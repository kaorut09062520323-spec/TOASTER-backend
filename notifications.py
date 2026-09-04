import os
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_connection


router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
)


class DeviceTokenRequest(BaseModel):
    user_id: int
    token: str


APNS_KEY_ID_ENV = "APNS_KEY_ID"
APNS_TEAM_ID_ENV = "APPLE_TEAM_ID"
APNS_BUNDLE_ID_ENV = "APNS_BUNDLE_ID"
APNS_ENVIRONMENT_ENV = "APNS_ENVIRONMENT"
APNS_KEY_PATH_ENV = "APNS_KEY_PATH"

DEFAULT_APNS_KEY_NAMES = (
    "AuthKey_TOASTER_APNS.p8",
    "AuthKey_TOASTER_APNs.p8",
)


class APNSError(Exception):
    pass


def ensure_notification_tables() -> None:
    """Ensure the notification token table exists on the deployed database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS device_tokens (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, token)
                );
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_tokens_user_id
                ON device_tokens(user_id);
                """
            )

            conn.commit()


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise APNSError(
            f"{name} is not set"
        )

    return value


def _get_apns_key_path() -> Path:
    configured = os.getenv(
        APNS_KEY_PATH_ENV,
        ""
    ).strip()

    candidates: list[Path] = []

    if configured:
        candidates.append(
            Path(configured)
        )

    for name in DEFAULT_APNS_KEY_NAMES:
        candidates.extend(
            [
                Path("/etc/secrets") / name,
                Path(__file__).resolve().parent / name,
            ]
        )

    # Render Secret Files are mounted under /etc/secrets/<filename>.
    secret_dir = Path("/etc/secrets")

    if secret_dir.is_dir():
        candidates.extend(
            sorted(
                secret_dir.glob("*.p8")
            )
        )

    # Local development convenience.
    project_dir = Path(__file__).resolve().parent

    candidates.extend(
        sorted(
            project_dir.glob("*.p8")
        )
    )

    seen: set[Path] = set()

    for path in candidates:

        if path in seen:
            continue

        seen.add(path)

        if path.is_file():
            return path

    raise APNSError(
        "APNs .p8 key file not found. "
        "Set APNS_KEY_PATH or place the key in /etc/secrets/."
    )


def _create_apns_jwt() -> str:

    key_id = _get_required_env(
        APNS_KEY_ID_ENV
    )

    team_id = _get_required_env(
        APNS_TEAM_ID_ENV
    )

    key_path = _get_apns_key_path()

    private_key = key_path.read_text(
        encoding="utf-8"
    )

    return jwt.encode(
        {
            "iss": team_id,
            "iat": __import__("time").time().__int__(),
        },
        private_key,
        algorithm="ES256",
        headers={
            "kid": key_id
        },
    )


def _get_apns_host() -> str:

    environment = os.getenv(
        APNS_ENVIRONMENT_ENV,
        "sandbox"
    ).strip().lower()

    if environment == "production":
        return "https://api.push.apple.com"

    if environment == "sandbox":
        return "https://api.sandbox.push.apple.com"

    raise APNSError(
        f"Invalid {APNS_ENVIRONMENT_ENV}: "
        f"{environment!r}. "
        "Use 'sandbox' or 'production'."
    )


def _delete_invalid_token(
    token: str
) -> None:

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM device_tokens
                WHERE token = %s
                """,
                (
                    token,
                ),
            )

            conn.commit()


def _send_to_apns(
    token: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> bool:

    bundle_id = _get_required_env(
        APNS_BUNDLE_ID_ENV
    )

    jwt_token = _create_apns_jwt()

    host = _get_apns_host()

    payload: dict[str, Any] = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": "default",
            "badge": 1,
        }
    }

    if data:
        payload.update(data)

    headers = {
        "authorization": f"bearer {jwt_token}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }

    url = f"{host}/3/device/{token}"

    with httpx.Client(
        http2=True,
        timeout=10.0
    ) as client:

        response = client.post(
            url,
            headers=headers,
            json=payload,
        )

    if response.status_code == 200:
        return True

    reason = ""

    try:
        reason = response.json().get(
            "reason",
            ""
        )

    except ValueError:
        pass

    # APNs tells us that these tokens are no longer usable.
    if (
        response.status_code == 410
        or reason in {
            "BadDeviceToken",
            "DeviceTokenNotForTopic",
            "Unregistered",
        }
    ):
        _delete_invalid_token(
            token
        )

    raise APNSError(
        f"APNs request failed: "
        f"status={response.status_code}, "
        f"reason={reason or 'unknown'}"
    )


def send_push_notification(
    user_id: int,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Send a push notification to every registered device for a user.

    Returns the number of devices that accepted the notification.

    Individual device failures are logged and do not break the
    originating like/follow/comment request.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT token
                FROM device_tokens
                WHERE user_id = %s
                """,
                (
                    user_id,
                ),
            )

            tokens = [
                row[0]
                for row in cur.fetchall()
            ]

    if not tokens:
        return 0

    sent_count = 0

    for token in tokens:

        try:

            if _send_to_apns(
                token=token,
                title=title,
                body=body,
                data=data,
            ):
                sent_count += 1

        except Exception as error:

            # Notification delivery must never make
            # a like/follow/comment fail.
            print(
                "APNs Notification Error:",
                {
                    "user_id": user_id,
                    "error": str(error),
                },
            )

    return sent_count


@router.post("/device-token")
def register_device_token(
    data: DeviceTokenRequest,
):

    token = data.token.strip()

    if not token:
        raise HTTPException(
            status_code=400,
            detail="device token is empty",
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM users
                WHERE id = %s
                """,
                (
                    data.user_id,
                ),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="user not found",
                )

            cur.execute(
                """
                INSERT INTO device_tokens (
                    user_id,
                    token
                )
                VALUES (%s, %s)
                ON CONFLICT (
                    user_id,
                    token
                )
                DO NOTHING
                """,
                (
                    data.user_id,
                    token,
                ),
            )

            conn.commit()

    return {
        "registered": True,
    }


@router.delete("/device-token")
def delete_device_token(
    data: DeviceTokenRequest,
):

    token = data.token.strip()

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM device_tokens
                WHERE user_id = %s
                AND token = %s
                """,
                (
                    data.user_id,
                    token,
                ),
            )

            conn.commit()

    return {
        "deleted": True,
    }