from fastapi import APIRouter, HTTPException, Header, Depends
from google.oauth2 import id_token
from google.auth.transport import requests
from pydantic import BaseModel

from database import get_connection
import os
import hashlib
import secrets
import re
import hmac
import base64
import jwt
from jwt import PyJWKClient

from achievements import grant_achievement


router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# Sign in with Apple uses the app's Bundle ID as the audience.
APPLE_BUNDLE_ID = os.getenv(
    "APPLE_BUNDLE_ID",
    "com.toaster.kaoru.toaster",
)
APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = f"{APPLE_ISSUER}/auth/keys"
apple_jwk_client = PyJWKClient(APPLE_KEYS_URL)

JWT_ALGORITHM = "HS256"
JWT_EXPIRES_DAYS = int(os.getenv("TOASTER_JWT_EXPIRES_DAYS", "30"))


def _jwt_secret() -> str:
    secret = os.getenv("TOASTER_JWT_SECRET")
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="TOASTER_JWT_SECRET is not configured",
        )
    return secret


def create_access_token(user_id: int) -> str:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=JWT_EXPIRES_DAYS),
        "type": "access",
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_access_token(token: str) -> int:
    try:
        claims = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp", "type"]},
        )
        if claims.get("type") != "access":
            raise ValueError("invalid token type")
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, HTTPException):
        raise HTTPException(status_code=401, detail="Invalid or expired access token")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=401, detail="User no longer exists")

    return user_id


def get_current_user_id(authorization: str | None = Header(default=None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization required")
    return verify_access_token(authorization[7:].strip())


# During the initial launch period, new registrations receive the one-time
# "始まりのユーザー" achievement.
EARLY_MEMBER_ENABLED = os.getenv(
    "TOASTER_EARLY_MEMBER_ENABLED",
    "true",
).lower() == "true"


class GoogleLoginRequest(BaseModel):
    id_token: str


class AppleLoginRequest(BaseModel):
    identity_token: str
    nonce: str
    display_name: str | None = None


class OriginalLoginRequest(BaseModel):
    login_id: str
    password: str


class CompleteGoogleSignupRequest(BaseModel):
    id_token: str
    login_id: str
    display_name: str
    password: str
    accepted_terms: bool
    accepted_privacy: bool


class CompleteAppleSignupRequest(BaseModel):
    identity_token: str
    nonce: str
    display_name: str
    accepted_terms: bool
    accepted_privacy: bool


LOGIN_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,19}$")
PBKDF2_ITERATIONS = 600_000


def _normalize_login_id(value: str) -> str:
    return value.strip().lower()


def _validate_signup_fields(login_id: str, display_name: str, password: str):
    display_name = display_name.strip()

    if not display_name or len(display_name) > 50:
        raise HTTPException(status_code=400, detail="Display name must be 1-50 characters.")

    # Apple authentication is the account authentication mechanism.
    # Generate an internal login_id so existing DB constraints remain intact,
    # but do not ask the Apple user to choose or enter one.
    login_id = None
    if len(password) < 8 or len(password) > 128:
        raise HTTPException(status_code=400, detail="Password must be 8-128 characters.")
    return login_id, display_name


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode()}$"
        f"{base64.urlsafe_b64encode(digest).decode()}"
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _provider_lookup(cur, provider: str, provider_user_id: str):
    cur.execute(
        """
        SELECT user_id
        FROM auth_accounts
        WHERE provider = %s AND provider_user_id = %s
        """,
        (provider, provider_user_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _create_provider_account(
    cur,
    provider: str,
    provider_user_id: str,
    login_id: str,
    display_name: str,
    password: str,
    profile_image: str | None = None,
):
    cur.execute(
        """
        INSERT INTO users (display_name, login_id, password_hash, profile_image)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (display_name, login_id, _hash_password(password), profile_image),
    )
    user_id = cur.fetchone()[0]

    if EARLY_MEMBER_ENABLED:
        grant_achievement(cur, user_id, "early_member", None)

    cur.execute(
        """
        INSERT INTO auth_accounts (user_id, provider, provider_user_id)
        VALUES (%s, %s, %s)
        """,
        (user_id, provider, provider_user_id),
    )
    return user_id


def _complete_provider_signup(
    provider: str,
    provider_user_id: str,
    login_id: str,
    display_name: str,
    password: str,
    accepted_terms: bool,
    accepted_privacy: bool,
    profile_image: str | None = None,
):
    if not accepted_terms or not accepted_privacy:
        raise HTTPException(
            status_code=400,
            detail="Terms and Privacy Policy acceptance are required.",
        )

    login_id, display_name = _validate_signup_fields(
        login_id, display_name, password
    )

    # Import here so the rest of the backend keeps its existing dependencies.
    from psycopg.errors import UniqueViolation

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if _provider_lookup(cur, provider, provider_user_id) is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"This {provider.capitalize()} account is already registered.",
                    )

                cur.execute(
                    "SELECT id FROM users WHERE login_id = %s",
                    (login_id,),
                )
                if cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="This Login ID is already in use.",
                    )

                user_id = _create_provider_account(
                    cur,
                    provider,
                    provider_user_id,
                    login_id,
                    display_name,
                    password,
                    profile_image,
                )
    except UniqueViolation as exc:
        # The DB unique constraints are the final authority for races.
        constraint = getattr(exc.diag, "constraint_name", "") or ""
        if "login_id" in constraint:
            raise HTTPException(status_code=409, detail="This Login ID is already in use.")
        if "auth_accounts" in constraint or "provider" in constraint:
            raise HTTPException(
                status_code=409,
                detail=f"This {provider.capitalize()} account is already registered.",
            )
        raise

    return {
        "user_id": user_id,
        "is_new_user": False,
        "access_token": create_access_token(user_id),
        "token_type": "Bearer",
    }


@router.post("/google")
def google_login(data: GoogleLoginRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured")

    try:
        google_user = id_token.verify_oauth2_token(
            data.id_token,
            requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google ID token")

    google_user_id = google_user["sub"]

    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _provider_lookup(cur, "google", google_user_id)

    if user_id is None:
        # Verified provider identity, but no TOASTER account exists yet.
        # The frontend may now show the registration flow.
        return {
            "user_id": 0,
            "is_new_user": True,
            "access_token": "",
            "token_type": "Bearer",
        }

    return {
        "user_id": user_id,
        "is_new_user": False,
        "access_token": create_access_token(user_id),
        "token_type": "Bearer",
    }


@router.post("/apple")
def apple_login(data: AppleLoginRequest):
    if not data.identity_token or not data.nonce:
        raise HTTPException(status_code=400, detail="Apple identity token and nonce are required")

    expected_nonce = hashlib.sha256(data.nonce.encode("utf-8")).hexdigest()
    try:
        signing_key = apple_jwk_client.get_signing_key_from_jwt(data.identity_token)
        claims = jwt.decode(
            data.identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_BUNDLE_ID,
            issuer=APPLE_ISSUER,
            options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Apple identity token")

    if claims.get("nonce") != expected_nonce:
        raise HTTPException(status_code=401, detail="Invalid Apple nonce")

    apple_user_id = claims["sub"]

    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _provider_lookup(cur, "apple", apple_user_id)

    if user_id is None:
        return {
            "user_id": 0,
            "is_new_user": True,
            "access_token": "",
            "token_type": "Bearer",
        }

    return {
        "user_id": user_id,
        "is_new_user": False,
        "access_token": create_access_token(user_id),
        "token_type": "Bearer",
    }


def _complete_apple_signup(
    provider_user_id: str,
    display_name: str,
    accepted_terms: bool,
    accepted_privacy: bool,
):
    if not accepted_terms or not accepted_privacy:
        raise HTTPException(
            status_code=400,
            detail="Terms and Privacy Policy acceptance are required.",
        )

    display_name = display_name.strip()

    if not display_name or len(display_name) > 50:
        raise HTTPException(status_code=400, detail="Display name must be 1-50 characters.")

    # Apple authentication is the account authentication mechanism.
    # Generate an internal login_id so existing DB constraints remain intact,
    # but do not ask the Apple user to choose or enter one.
    login_id = None

    from psycopg.errors import UniqueViolation

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if _provider_lookup(cur, "apple", provider_user_id) is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="This Apple account is already registered.",
                    )

                # Keep generating until a unique internal login_id is found.
                # The value is not shown to the user and is not used as the
                # Apple authentication credential.
                for _ in range(5):
                    login_id = "apple" + secrets.token_hex(7)
                    cur.execute("SELECT id FROM users WHERE login_id = %s", (login_id,))
                    if cur.fetchone() is None:
                        break
                else:
                    raise HTTPException(status_code=500, detail="Could not allocate an internal Login ID.")

                # Sign in with Apple is already the authentication mechanism.
                # Do not create or request a separate TOASTER password.
                cur.execute(
                    """
                    INSERT INTO users (display_name, login_id, password_hash, profile_image)
                    VALUES (%s, %s, NULL, NULL)
                    RETURNING id
                    """,
                    (display_name, login_id),
                )
                user_id = cur.fetchone()[0]

                if EARLY_MEMBER_ENABLED:
                    grant_achievement(cur, user_id, "early_member", None)

                cur.execute(
                    """
                    INSERT INTO auth_accounts (user_id, provider, provider_user_id)
                    VALUES (%s, %s, %s)
                    """,
                    (user_id, "apple", provider_user_id),
                )
    except UniqueViolation as exc:
        constraint = getattr(exc.diag, "constraint_name", "") or ""
        if "login_id" in constraint:
            raise HTTPException(status_code=409, detail="This Login ID is already in use.")
        if "auth_accounts" in constraint or "provider" in constraint:
            raise HTTPException(status_code=409, detail="This Apple account is already registered.")
        raise

    return {
        "user_id": user_id,
        "is_new_user": False,
        "access_token": create_access_token(user_id),
        "token_type": "Bearer",
    }


@router.post("/google/signup")
def complete_google_signup(data: CompleteGoogleSignupRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured")

    try:
        google_user = id_token.verify_oauth2_token(
            data.id_token,
            requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google ID token")

    return _complete_provider_signup(
        "google",
        google_user["sub"],
        data.login_id,
        data.display_name,
        data.password,
        data.accepted_terms,
        data.accepted_privacy,
        google_user.get("picture"),
    )


@router.post("/apple/signup")
def complete_apple_signup(data: CompleteAppleSignupRequest):
    if not data.identity_token or not data.nonce:
        raise HTTPException(status_code=400, detail="Apple identity token and nonce are required")

    expected_nonce = hashlib.sha256(data.nonce.encode("utf-8")).hexdigest()
    try:
        signing_key = apple_jwk_client.get_signing_key_from_jwt(data.identity_token)
        claims = jwt.decode(
            data.identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_BUNDLE_ID,
            issuer=APPLE_ISSUER,
            options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Apple identity token")

    if claims.get("nonce") != expected_nonce:
        raise HTTPException(status_code=401, detail="Invalid Apple nonce")

    return _complete_apple_signup(
        claims["sub"],
        data.display_name,
        data.accepted_terms,
        data.accepted_privacy,
    )


@router.post("/login")
def original_login(data: OriginalLoginRequest):
    login_id = _normalize_login_id(data.login_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, display_name, password_hash FROM users WHERE login_id = %s",
                (login_id,),
            )
            row = cur.fetchone()

    if row is None or not row[2] or not _verify_password(data.password, row[2]):
        raise HTTPException(status_code=401, detail="Invalid Login ID or password")

    return {
        "user_id": row[0],
        "is_new_user": False,
        "access_token": create_access_token(row[0]),
        "token_type": "Bearer",
    }


@router.get("/me")
def get_me(current_user_id: int = Depends(get_current_user_id)):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, display_name, login_id FROM users WHERE id = %s", (current_user_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=401, detail="User no longer exists")
    return {"user_id": row[0], "display_name": row[1], "login_id": row[2]}
