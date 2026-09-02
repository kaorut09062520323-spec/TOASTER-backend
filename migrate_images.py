"""One-time migration for existing post images.

Existing R2 objects may still be original 3000px/4000px photos.
This script downloads each post image, normalizes it to the same 800px
maximum-side JPEG used for new posts, and uploads it back to the same key.
"""

from image_utils import resize_image_to_800
from database import get_connection
from r2 import download_image, upload_image


def migrate():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, image_key
                FROM posts
                WHERE image_key IS NOT NULL
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall()

    print(f"Found {len(rows)} post images.")

    for post_id, image_key in rows:
        try:
            obj = download_image(image_key)
            original = obj["Body"].read()
            resized = resize_image_to_800(original)

            upload_image(
                resized,
                image_key,
                "image/jpeg",
            )

            print(
                f"Post {post_id}: "
                f"{len(original)} -> {len(resized)} bytes"
            )

        except Exception as e:
            print(
                f"Post {post_id}: FAILED - {e}"
            )


if __name__ == "__main__":
    migrate()
