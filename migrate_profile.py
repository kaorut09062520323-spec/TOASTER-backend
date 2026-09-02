"""Add profile fields required by the profile editor.

Safe to run multiple times.
"""
from database import get_connection


def migrate():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT"
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_image_key TEXT"
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS background_image_key TEXT"
            )
    print("Profile columns OK.")


if __name__ == "__main__":
    migrate()
