from fastapi import APIRouter, HTTPException
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
                }

            # Apple supplies the user's name only during the first
            # authorization. The iOS client therefore sends the name obtained
            # from ASAuthorizationAppleIDCredential.fullName on first signup.
            display_name = (
                (data.display_name or "").strip()
                or "Apple User"
            )

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
    }
