from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from database import get_connection


router = APIRouter(
    prefix="/users",
    tags=["users"]
)


class UserCreate(BaseModel):
    display_name: str
    profile_image: str | None = None


# MARK: - Create User

@router.post("")
def create_user(user: UserCreate):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO users (
                    display_name,
                    profile_image
                )
                VALUES (%s, %s)
                RETURNING
                    id,
                    display_name,
                    profile_image,
                    created_at
                """,
                (
                    user.display_name,
                    user.profile_image
                ),
            )

            row = cur.fetchone()

    return {
        "id": row[0],
        "display_name": row[1],
        "profile_image": row[2],
        "created_at": row[3],
    }


# MARK: - Delete Account

@router.delete("/{user_id}")
def delete_account(user_id: int):
    """
    Permanently delete a user and all database records owned by that user.
    Foreign-key cascades remove posts, reactions, comments, follows,
    authentication links, blocks and achievements.
    R2 objects are removed separately before the DB row is deleted.
    """
    from r2 import delete_image

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT profile_image_key, background_image_key
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )
            user = cur.fetchone()

            if user is None:
                raise HTTPException(status_code=404, detail="user not found")

            cur.execute(
                """
                SELECT image_key
                FROM posts
                WHERE user_id = %s
                  AND image_key IS NOT NULL
                """,
                (user_id,),
            )
            post_images = [row[0] for row in cur.fetchall()]

            image_keys = [
                key for key in [user[0], user[1]] if key
            ] + post_images

            # daily_rankings keeps a reference to posts without an ON DELETE
            # CASCADE rule, so remove those historical ranking rows first.
            cur.execute(
                """
                DELETE FROM daily_rankings
                WHERE post_id IN (
                    SELECT id
                    FROM posts
                    WHERE user_id = %s
                )
                """,
                (user_id,),
            )

            # Delete DB row. The remaining user-owned rows are removed by
            # the existing foreign-key CASCADE rules.
            cur.execute(
                "DELETE FROM users WHERE id = %s RETURNING id",
                (user_id,),
            )

    # R2 cleanup is intentionally best-effort. Account deletion has already
    # succeeded in the database, and a failed object cleanup must not leave
    # the user account alive.
    for key in image_keys:
        try:
            delete_image(key)
        except Exception as e:
            print("Account Image Cleanup Error:", key, e)

    return {
        "deleted": True,
        "user_id": user_id,
    }


# MARK: - Get User

# MARK: - Search

@router.get("/search")
def search_users(q: str):
    query = q.strip()
    if not query:
        return []

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, display_name, profile_image
                FROM users
                WHERE display_name ILIKE %s
                ORDER BY display_name ASC
                LIMIT 50
                """,
                (f"%{query}%",),
            )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "display_name": row[1],
            "profile_image": row[2],
        }
        for row in rows
    ]

@router.get("/{user_id}")
def get_user(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    display_name,
                    profile_image,
                    created_at
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            row = cur.fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="user not found",
        )

    return {
        "id": row[0],
        "display_name": row[1],
        "profile_image": row[2],
        "created_at": row[3],
    }


# MARK: - Follow

@router.post("/{user_id}/follow/{target_user_id}")
def follow_user(
    user_id: int,
    target_user_id: int,
):

    if user_id == target_user_id:
        raise HTTPException(
            status_code=400,
            detail="cannot follow yourself",
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="user not found",
                )

            cur.execute(
                """
                SELECT id
                FROM users
                WHERE id = %s
                """,
                (target_user_id,),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="target user not found",
                )

            cur.execute(
                """
                INSERT INTO follows (
                    follower_id,
                    following_id
                )
                VALUES (%s, %s)
                ON CONFLICT (
                    follower_id,
                    following_id
                )
                DO NOTHING
                """,
                (
                    user_id,
                    target_user_id,
                ),
            )

    return {
        "following": True
    }


# MARK: - Unfollow

@router.delete("/{user_id}/follow/{target_user_id}")
def unfollow_user(
    user_id: int,
    target_user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM follows
                WHERE follower_id = %s
                AND following_id = %s
                """,
                (
                    user_id,
                    target_user_id,
                ),
            )

    return {
        "following": False
    }


# MARK: - Follow Status

@router.get(
    "/{user_id}/following/{target_user_id}"
)
def get_follow_status(
    user_id: int,
    target_user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM follows
                WHERE follower_id = %s
                AND following_id = %s
                """,
                (
                    user_id,
                    target_user_id,
                ),
            )

            row = cur.fetchone()

    return {
        "following": row is not None
    }


# MARK: - Followers

@router.get("/{user_id}/followers")
def get_followers(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    u.id,
                    u.display_name,
                    u.profile_image
                FROM follows f
                JOIN users u
                    ON u.id = f.follower_id
                WHERE f.following_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )

            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "display_name": row[1],
            "profile_image": row[2],
        }
        for row in rows
    ]


# MARK: - Following

@router.get("/{user_id}/following")
def get_following(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    u.id,
                    u.display_name,
                    u.profile_image
                FROM follows f
                JOIN users u
                    ON u.id = f.following_id
                WHERE f.follower_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )

            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "display_name": row[1],
            "profile_image": row[2],
        }
        for row in rows
    ]


# MARK: - Follow Counts

@router.get("/{user_id}/follow-counts")
def get_follow_counts(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM follows
                        WHERE following_id = %s
                    ) AS followers_count,

                    (
                        SELECT COUNT(*)
                        FROM follows
                        WHERE follower_id = %s
                    ) AS following_count
                """,
                (
                    user_id,
                    user_id,
                ),
            )

            row = cur.fetchone()

    return {
        "followers_count": row[0],
        "following_count": row[1],
    }

# MARK: - Profile

@router.get("/{user_id}/profile")
def get_user_profile(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    u.id,
                    u.display_name,
                    u.profile_image,
                    u.profile_image_key,
                    u.background_image_key,
                    u.bio,

                    (
                        SELECT COUNT(*)
                        FROM follows f
                        WHERE f.following_id = u.id
                    ) AS followers_count,

                    (
                        SELECT COUNT(*)
                        FROM follows f
                        WHERE f.follower_id = u.id
                    ) AS following_count,

                    (
                        SELECT COUNT(*)
                        FROM posts p
                        WHERE p.user_id = u.id
                    ) AS post_count

                FROM users u
                WHERE u.id = %s
                """,
                (user_id,),
            )

            row = cur.fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="user not found",
        )

    return {
        "id": row[0],
        "display_name": row[1],
        "profile_image": row[2],
        "profile_image_key": row[3],
        "background_image_key": row[4],
        "bio": row[5],
        "followers_count": row[6],
        "following_count": row[7],
        "post_count": row[8],
    }



# MARK: - User Posts

@router.get("/{user_id}/posts")
def get_user_posts(user_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.user_id,
                    u.display_name,
                    p.category,
                    p.content,
                    p.image_key,
                    p.like_count,
                    (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id),
                    p.created_at
                FROM posts p
                JOIN users u ON u.id = p.user_id
                WHERE p.user_id = %s
                ORDER BY p.created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "user_id": row[1],
            "display_name": row[2],
            "category": row[3],
            "content": row[4],
            "image_key": row[5],
            "like_count": row[6],
            "comment_count": row[7],
            "created_at": row[8],
        }
        for row in rows
    ]

# MARK: - Profile Update

@router.put("/{user_id}/profile")
async def update_user_profile(
    user_id: int,
    display_name: str = Form(...),
    bio: str = Form(""),
    profile_image: UploadFile | None = File(None),
    background_image: UploadFile | None = File(None),
):
    from image_utils import resize_image_to_800
    from r2 import upload_image
    from fastapi.responses import StreamingResponse
    import uuid

    display_name = display_name.strip()
    bio = bio.strip()

    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE id = %s",
                (user_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="user not found")

            profile_image_key = None
            background_image_key = None

            for upload, prefix in (
                (profile_image, "profiles"),
                (background_image, "profile_backgrounds"),
            ):
                if upload is None:
                    continue

                if not upload.content_type or not upload.content_type.startswith("image/"):
                    raise HTTPException(status_code=400, detail="file must be an image")

                image_data = await upload.read()
                if not image_data:
                    raise HTTPException(status_code=400, detail="empty image")

                try:
                    image_data = resize_image_to_800(image_data)
                except Exception as e:
                    print("Profile Image Processing Error:", e)
                    raise HTTPException(status_code=400, detail="invalid image")

                object_key = f"{prefix}/{uuid.uuid4()}.jpg"

                try:
                    upload_image(image_data, object_key, "image/jpeg")
                except Exception as e:
                    print("Profile Image R2 Error:", e)
                    raise HTTPException(status_code=500, detail="failed to upload image")

                if prefix == "profiles":
                    profile_image_key = object_key
                else:
                    background_image_key = object_key

            if profile_image_key is None and background_image_key is None:
                cur.execute(
                    """
                    UPDATE users
                    SET display_name = %s,
                        bio = %s
                    WHERE id = %s
                    """,
                    (display_name, bio, user_id),
                )
            elif profile_image_key is None:
                cur.execute(
                    """
                    UPDATE users
                    SET display_name = %s,
                        bio = %s,
                        background_image_key = %s
                    WHERE id = %s
                    """,
                    (display_name, bio, background_image_key, user_id),
                )
            elif background_image_key is None:
                cur.execute(
                    """
                    UPDATE users
                    SET display_name = %s,
                        bio = %s,
                        profile_image_key = %s
                    WHERE id = %s
                    """,
                    (display_name, bio, profile_image_key, user_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE users
                    SET display_name = %s,
                        bio = %s,
                        profile_image_key = %s,
                        background_image_key = %s
                    WHERE id = %s
                    """,
                    (
                        display_name,
                        bio,
                        profile_image_key,
                        background_image_key,
                        user_id,
                    ),
                )

    return get_user_profile(user_id)


# MARK: - Custom Profile Image

@router.get("/{user_id}/profile-image")
def get_profile_image(user_id: int):
    from fastapi.responses import StreamingResponse
    from r2 import download_image

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile_image_key FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()

    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail="image not found")

    try:
        obj = download_image(row[0])
    except Exception:
        raise HTTPException(status_code=404, detail="image not found")

    return StreamingResponse(
        obj["Body"],
        media_type=obj.get("ContentType") or "image/jpeg",
    )


# MARK: - Background Image

@router.get("/{user_id}/background-image")
def get_background_image(user_id: int):
    from fastapi.responses import StreamingResponse
    from r2 import download_image

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT background_image_key FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()

    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail="image not found")

    try:
        obj = download_image(row[0])
    except Exception:
        raise HTTPException(status_code=404, detail="image not found")

    return StreamingResponse(
        obj["Body"],
        media_type=obj.get("ContentType") or "image/jpeg",
    )
