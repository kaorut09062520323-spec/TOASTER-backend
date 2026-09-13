from fastapi import APIRouter, HTTPException, Header, Depends
from google.oauth2 import id_token
from google.auth.transport import requests
from pydantic import BaseModel

from database import get_connection
import os
import hashlib
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
    create_account: bool = True


class AppleLoginRequest(BaseModel):
    identity_token: str
    nonce: str
    display_name: str | None = None
    create_account: bool = True


def _create_user_and_auth_account(
    cur,
    provider: str,
    provider_user_id: str,
    display_name: str,
    profile_image: str | None = None,
):
    cur.execute(
        """
        INSERT INTO users (
            display_name,
            profile_image
        )
        VALUES (%s, %s)
        RETURNING id
        """,
        (display_name, profile_image),
    )

    user_id = cur.fetchone()[0]

    if EARLY_MEMBER_ENABLED:
        grant_achievement(
            cur,
            user_id,
            "early_member",
            None,
        )

    cur.execute(
        """
        INSERT INTO auth_accounts (
            user_id,
            provider,
            provider_user_id
        )
        VALUES (%s, %s, %s)
        """,
        (user_id, provider, provider_user_id),
    )

    return user_id


@router.post("/google")
def google_login(data: GoogleLoginRequest):

    print("=== /auth/google START ===")

    if not GOOGLE_CLIENT_ID:
        print("GOOGLE_CLIENT_ID missing")
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_CLIENT_ID is not configured",
        )

    print("GOOGLE_CLIENT_ID exists")
    print("ID Token received:", bool(data.id_token))

    try:
        print("Starting Google token verification...")

        google_user = id_token.verify_oauth2_token(
            data.id_token,
            requests.Request(),
            GOOGLE_CLIENT_ID,
        )

        print("Google token verification SUCCESS")

    except ValueError:
        print("Google token verification FAILED")
        raise HTTPException(
            status_code=401,
            detail="Invalid Google ID token",
        )

    google_user_id = google_user["sub"]
    display_name = google_user.get("name")
    profile_image = google_user.get("picture")

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT user_id
                FROM auth_accounts
                WHERE provider = %s
                  AND provider_user_id = %s
                """,
                ("google", google_user_id),
            )

            account = cur.fetchone()

            if account:
                user_id = account[0]

                return {
                    "user_id": user_id,
                    "is_new_user": False,
                    "access_token": create_access_token(user_id),
                    "token_type": "Bearer",
                }

            if not data.create_account:
                # Identity is verified, but account creation waits for
                # explicit acceptance of the Terms and Privacy Policy.
                return {
                    "user_id": 0,
                    "is_new_user": True,
                    "access_token": "",
                    "token_type": "Bearer",
                }

            user_id = _create_user_and_auth_account(
                cur=cur,
                provider="google",
                provider_user_id=google_user_id,
                display_name=display_name or "ユーザー",
                profile_image=profile_image,
            )

    return {
        "user_id": user_id,
        "is_new_user": True,
        "access_token": create_access_token(user_id),
        "token_type": "Bearer",
    }


@router.post("/apple")
def apple_login(data: AppleLoginRequest):

    print("=== /auth/apple START ===")
    print("Identity Token received:", bool(data.identity_token))
    print("Nonce received:", bool(data.nonce))

    if not data.identity_token:
        raise HTTPException(
            status_code=400,
            detail="Apple identity token is required",
        )

    if not data.nonce:
        raise HTTPException(
            status_code=400,
            detail="Apple nonce is required",
        )

    # The client sends the original random nonce. Apple receives SHA-256(nonce)
    # and returns that hashed value in the identity token's "nonce" claim.
    expected_nonce = hashlib.sha256(
        data.nonce.encode("utf-8")
    ).hexdigest()

    try:
        signing_key = apple_jwk_client.get_signing_key_from_jwt(
            data.identity_token
        )

        claims = jwt.decode(
            data.identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_BUNDLE_ID,
            issuer=APPLE_ISSUER,
            options={
                "require": ["iss", "aud", "exp", "iat", "sub", "nonce"],
            },
        )

    except Exception as e:
        print("Apple token verification FAILED:", repr(e))
        raise HTTPException(
            status_code=401,
            detail="Invalid Apple identity token",
        )

    token_nonce = claims.get("nonce")

    if token_nonce != expected_nonce:
        print("Apple nonce verification FAILED")
        raise HTTPException(
            status_code=401,
            detail="Invalid Apple nonce",
        )

    apple_user_id = claims["sub"]

    # Apple may provide a private relay email. It is useful for diagnostics,
    # but the stable account key is Apple's "sub".
    apple_email = claims.get("email")

    print("Apple token verification SUCCESS")
    print("Apple user:", apple_user_id)
    print("Apple email present:", bool(apple_email))

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT user_id
                FROM auth_accounts
                WHERE provider = %s
                  AND provider_user_id = %s
                """,
                ("apple", apple_user_id),
            )

            account = cur.fetchone()

            if account:
                user_id = account[0]

                return {
                    "user_id": user_id,
                    "is_new_user": False,
                    "access_token": create_access_token(user_id),
                    "token_type": "Bearer",
                }

            # Apple supplies the user's name only during the first
            # authorization. The iOS client therefore sends the name obtained
            # from ASAuthorizationAppleIDCredential.fullName on first signup.
            display_name = (
                (data.display_name or "").strip()
                or "Apple User"
            )

            if not data.create_account:
                # Identity is verified, but account creation waits for
                # explicit acceptance of the Terms and Privacy Policy.
                return {
                    "user_id": 0,
                    "is_new_user": True,
                    "access_token": "",
                    "token_type": "Bearer",
                }

            user_id = _create_user_and_auth_account(
                cur=cur,
                provider="apple",
                provider_user_id=apple_user_id,
                display_name=display_name,
                profile_image=None,
            )

    return {
        "user_id": user_id,
        "is_new_user": True,
        "access_token": create_access_token(user_id),
        "token_type": "Bearer",
    }


@router.get("/me")
def get_me(current_user_id: int = Depends(get_current_user_id)):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, display_name FROM users WHERE id = %s", (current_user_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=401, detail="User no longer exists")
    return {"user_id": row[0], "display_name": row[1]}
