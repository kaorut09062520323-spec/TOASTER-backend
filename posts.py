from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from database import get_connection
from r2 import upload_image, delete_image
from image_utils import resize_image_to_800

import uuid


router = APIRouter(
    prefix="/posts",
    tags=["posts"]
)


# MARK: - Create Post

@router.post("")
async def create_post(
    user_id: int = Form(...),
    category: str = Form(...),
    content: str | None = Form(None),
    image: UploadFile = File(...),
):

    # MARK: - Validate Category

    allowed_categories = {
        "fashion",
        "hair_style",
        "food",
        "spot",
    }

    if category not in allowed_categories:
        raise HTTPException(
            status_code=400,
            detail="invalid category",
        )


    # MARK: - Validate Image

    if not image.content_type:
        raise HTTPException(
            status_code=400,
            detail="invalid image",
        )

    if not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="file must be an image",
        )


    # MARK: - User Check

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


    # MARK: - Read Image

    image_data = await image.read()

    if not image_data:
        raise HTTPException(
            status_code=400,
            detail="empty image",
        )


    # MARK: - Normalize Image

    # Every post image is stored at a maximum of 800px on its long side.
    # This keeps all app screens lightweight without requiring a separate
    # thumbnail URL.
    try:

        image_data = resize_image_to_800(
            image_data
        )

    except Exception as e:

        print(
            "Image Processing Error:",
            e,
        )

        raise HTTPException(
            status_code=400,
            detail="invalid image",
        )


    # MARK: - Image Key

    image_key = (
        f"posts/{uuid.uuid4()}.jpg"
    )


    # MARK: - Upload R2

    try:

        upload_image(
            image_data,
            image_key,
            "image/jpeg",
        )

    except Exception as e:

        print(
            "R2 Upload Error:",
            e,
        )

        raise HTTPException(
            status_code=500,
            detail="failed to upload image",
        )


    # MARK: - Create Post

    try:

        with get_connection() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO posts (
                        user_id,
                        category,
                        content,
                        image_key
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                    RETURNING
                        id,
                        user_id,
                        category,
                        content,
                        image_key,
                        like_count,
                        created_at
                    """,
                    (
                        user_id,
                        category,
                        content,
                        image_key,
                    ),
                )

                row = cur.fetchone()

    except Exception as e:

        print(
            "Create Post DB Error:",
            e,
        )

        raise HTTPException(
            status_code=500,
            detail="failed to create post",
        )


    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT display_name FROM users WHERE id = %s",
                (user_id,),
            )
            display_name = cur.fetchone()[0]

    return {
        "id": row[0],
        "user_id": row[1],
        "display_name": display_name,
        "category": row[2],
        "content": row[3],
        "image_key": row[4],
        "like_count": row[5],
        "comment_count": 0,
        "created_at": row[6],
    }


# MARK: - Get Posts

@router.get("")
def get_posts(
    user_id: int,
    following: bool = False,
    category: str | None = None,
):
    """
    Personalized MVP home feed.

    Rules:
    1. A post that the user has already reacted to (like, dislike, comment)
       is permanently excluded from the normal feed.
    2. Category can be filtered.
    3. following=true restricts candidates to followed users.
    4. Own posts are excluded.
    5. Freshness and popularity are lightly scored.
    6. A small deterministic-ish random component prevents a permanently
       identical ordering.
    """

    allowed_categories = {
        "fashion",
        "hair_style",
        "food",
        "spot",
    }

    if category is not None and category not in allowed_categories:
        raise HTTPException(
            status_code=400,
            detail="invalid category",
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Reaction history is the permanent exclusion set for the feed.
            # Commenting counts as a reaction because a comment is an explicit
            # interaction with the post.
            reaction_cte = """
                WITH reacted_posts AS (
                    SELECT post_id
                    FROM post_likes
                    WHERE user_id = %s

                    UNION

                    SELECT post_id
                    FROM post_dislikes
                    WHERE user_id = %s

                    UNION

                    SELECT post_id
                    FROM comments
                    WHERE user_id = %s
                )
            """

            params = [user_id, user_id, user_id]

            if following:
                follow_sql = """
                    INNER JOIN follows f
                        ON f.following_id = p.user_id
                        AND f.follower_id = %s
                """
                params.append(user_id)
            else:
                follow_sql = ""

            category_sql = ""
            if category is not None:
                category_sql = "AND p.category = %s"
                params.append(category)

            cur.execute(
                reaction_cte
                + f"""
                SELECT
                    p.id,
                    p.user_id,
                    u.display_name,
                    p.category,
                    p.content,
                    p.image_key,
                    p.like_count,
                    (
                        SELECT COUNT(*)
                        FROM comments c
                        WHERE c.post_id = p.id
                    ) AS comment_count,
                    p.created_at,

                    -- Freshness: recent posts receive a modest boost.
                    GREATEST(
                        0.0,
                        1.0 - (
                            EXTRACT(
                                EPOCH FROM (CURRENT_TIMESTAMP - p.created_at)
                            ) / 86400.0
                        ) / 7.0
                    ) AS freshness_score,

                    -- Popularity is deliberately damped so large accounts
                    -- do not dominate every user's feed.
                    LN(1.0 + GREATEST(p.like_count, 0)) AS popularity_score

                FROM posts p
                JOIN users u
                    ON u.id = p.user_id

                {follow_sql}

                WHERE p.user_id <> %s

                AND NOT EXISTS (
                    SELECT 1
                    FROM blocks b
                    WHERE b.blocker_id = %s
                      AND b.blocked_id = p.user_id
                )

                AND NOT EXISTS (
                    SELECT 1
                    FROM blocks b
                    WHERE b.blocker_id = p.user_id
                      AND b.blocked_id = %s
                )

                AND p.expires_at > CURRENT_TIMESTAMP

                AND NOT EXISTS (
                    SELECT 1
                    FROM reacted_posts rp
                    WHERE rp.post_id = p.id
                )

                {category_sql}

                ORDER BY
                    (
                        (
                            GREATEST(
                                0.0,
                                1.0 - (
                                    EXTRACT(
                                        EPOCH FROM (
                                            CURRENT_TIMESTAMP - p.created_at
                                        )
                                    ) / 86400.0
                                ) / 7.0
                            ) * 0.55
                        )
                        +
                        (
                            LEAST(
                                LN(1.0 + GREATEST(p.like_count, 0)) / 8.0,
                                1.0
                            ) * 0.20
                        )
                        +
                        (
                            RANDOM() * 0.25
                        )
                    ) DESC,
                    p.created_at DESC
                """,
                tuple(
                    params
                    + [user_id, user_id, user_id]
                ),
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


# MARK: - Get Post

@router.get("/{post_id}")
def get_post(
    post_id: int,
):

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
                    (
                        SELECT COUNT(*)
                        FROM comments c
                        WHERE c.post_id = p.id
                    ) AS comment_count,
                    p.created_at
                FROM posts p
                JOIN users u
                    ON u.id = p.user_id
                WHERE p.id = %s
                """,
                (post_id,),
            )

            row = cur.fetchone()


    if row is None:
        raise HTTPException(
            status_code=404,
            detail="post not found",
        )


    return {
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


# MARK: - Update Post

@router.put("/{post_id}")
async def update_post(
    post_id: int,
    user_id: int = Form(...),
    category: str = Form(...),
    content: str | None = Form(None),
    image: UploadFile | None = File(None),
):
    allowed_categories = {
        "fashion",
        "hair_style",
        "food",
        "spot",
    }

    if category not in allowed_categories:
        raise HTTPException(
            status_code=400,
            detail="invalid category",
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    user_id,
                    image_key
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )
            existing = cur.fetchone()

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="post not found",
        )

    if existing[1] != user_id:
        raise HTTPException(
            status_code=403,
            detail="not allowed",
        )

    old_image_key = existing[2]
    new_image_key = old_image_key
    uploaded_new_key = None

    if image is not None:
        if not image.content_type or not image.content_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail="file must be an image",
            )

        image_data = await image.read()

        if not image_data:
            raise HTTPException(
                status_code=400,
                detail="empty image",
            )

        try:
            image_data = resize_image_to_800(image_data)
        except Exception as e:
            print("Image Processing Error:", e)
            raise HTTPException(
                status_code=400,
                detail="invalid image",
            )

        uploaded_new_key = f"posts/{uuid.uuid4()}.jpg"

        try:
            upload_image(
                image_data,
                uploaded_new_key,
                "image/jpeg",
            )
        except Exception as e:
            print("R2 Upload Error:", e)
            raise HTTPException(
                status_code=500,
                detail="failed to upload image",
            )

        new_image_key = uploaded_new_key

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE posts
                    SET
                        category = %s,
                        content = %s,
                        image_key = %s
                    WHERE id = %s
                    AND user_id = %s
                    RETURNING
                        id,
                        user_id,
                        category,
                        content,
                        image_key,
                        like_count,
                        (
                            SELECT COUNT(*)
                            FROM comments c
                            WHERE c.post_id = posts.id
                        ) AS comment_count,
                        created_at
                    """,
                    (
                        category,
                        content if content and content.strip() else None,
                        new_image_key,
                        post_id,
                        user_id,
                    ),
                )
                row = cur.fetchone()

    except Exception as e:
        if uploaded_new_key:
            try:
                delete_image(uploaded_new_key)
            except Exception:
                pass

        print("Update Post DB Error:", e)
        raise HTTPException(
            status_code=500,
            detail="failed to update post",
        )

    if row is None:
        if uploaded_new_key:
            try:
                delete_image(uploaded_new_key)
            except Exception:
                pass

        raise HTTPException(
            status_code=404,
            detail="post not found",
        )

    if (
        uploaded_new_key
        and old_image_key
        and old_image_key != uploaded_new_key
    ):
        try:
            delete_image(old_image_key)
        except Exception as e:
            # DB update succeeded, so the post remains valid even if
            # cleanup of the old object fails.
            print("Old Image Cleanup Error:", e)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT display_name
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )
            display_name = cur.fetchone()[0]

    return {
        "id": row[0],
        "user_id": row[1],
        "display_name": display_name,
        "category": row[2],
        "content": row[3],
        "image_key": row[4],
        "like_count": row[5],
        "comment_count": row[6],
        "created_at": row[7],
    }


# MARK: - Delete Post

@router.delete("/{post_id}")
def delete_post(
    post_id: int,
    user_id: int,
):
    """
    Delete a post owned by user_id.

    Deletion is idempotent:
    - If the post is already gone, return success so stale ranking/detail
      state does not surface a misleading 404.
    - If the post exists but belongs to another user, reject it.
    """

    image_key = None

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, user_id, image_key
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            post = cur.fetchone()

            # Already deleted / stale client state.
            if post is None:
                return {
                    "post_id": post_id,
                    "deleted": True,
                    "already_deleted": True,
                }

            if post[1] != user_id:
                raise HTTPException(
                    status_code=403,
                    detail="not authorized to delete this post",
                )

            image_key = post[2]

            # Remove dependent records first.
            cur.execute(
                """
                DELETE FROM post_likes
                WHERE post_id = %s
                """,
                (post_id,),
            )

            cur.execute(
                """
                DELETE FROM post_dislikes
                WHERE post_id = %s
                """,
                (post_id,),
            )

            cur.execute(
                """
                DELETE FROM comments
                WHERE post_id = %s
                """,
                (post_id,),
            )

            # Remove ranking references when the table exists.
            try:
                cur.execute(
                    """
                    DELETE FROM daily_rankings
                    WHERE post_id = %s
                    """,
                    (post_id,),
                )
            except Exception:
                # Keep post deletion working even if this project version
                # does not have daily_rankings or uses a different schema.
                conn.rollback()

                cur.execute(
                    """
                    SELECT id, user_id, image_key
                    FROM posts
                    WHERE id = %s
                    """,
                    (post_id,),
                )

                post = cur.fetchone()

                if post is None:
                    return {
                        "post_id": post_id,
                        "deleted": True,
                        "already_deleted": True,
                    }

                if post[1] != user_id:
                    raise HTTPException(
                        status_code=403,
                        detail="not authorized to delete this post",
                    )

                image_key = post[2]

                cur.execute(
                    "DELETE FROM post_likes WHERE post_id = %s",
                    (post_id,),
                )
                cur.execute(
                    "DELETE FROM post_dislikes WHERE post_id = %s",
                    (post_id,),
                )
                cur.execute(
                    "DELETE FROM comments WHERE post_id = %s",
                    (post_id,),
                )

            cur.execute(
                """
                DELETE FROM posts
                WHERE id = %s
                AND user_id = %s
                RETURNING id
                """,
                (post_id, user_id),
            )

            deleted = cur.fetchone()

            if deleted is None:
                return {
                    "post_id": post_id,
                    "deleted": True,
                    "already_deleted": True,
                }

    # R2 cleanup is intentionally outside the DB transaction.
    # A missing R2 object should not make the DB deletion fail.
    if image_key:
        try:
            from r2 import delete_image
            delete_image(image_key)
        except Exception as e:
            print("R2 Delete Warning:", e)

    return {
        "post_id": post_id,
        "deleted": True,
        "already_deleted": False,
    }

@router.get("/{post_id}/image")
def get_post_image(post_id: int):
    from fastapi.responses import StreamingResponse
    from r2 import download_image

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT image_key
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )
            row = cur.fetchone()

    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail="image not found")

    try:
        obj = download_image(row[0])
    except Exception:
        raise HTTPException(status_code=404, detail="image not found")

    content_type = obj.get("ContentType") or "image/jpeg"
    return StreamingResponse(obj["Body"], media_type=content_type)


# MARK: - Like Status

@router.get("/{post_id}/likes")
def get_like_status(
    post_id: int,
    user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT like_count
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            post = cur.fetchone()

            if post is None:
                raise HTTPException(
                    status_code=404,
                    detail="post not found",
                )


            cur.execute(
                """
                SELECT id
                FROM post_likes
                WHERE post_id = %s
                AND user_id = %s
                """,
                (
                    post_id,
                    user_id,
                ),
            )

            like = cur.fetchone()


    return {
        "post_id": post_id,
        "like_count": post[0],
        "liked": like is not None,
    }


# MARK: - Like

@router.post("/{post_id}/likes")
def like_post(
    post_id: int,
    user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="post not found",
                )


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
                INSERT INTO post_likes (
                    user_id,
                    post_id
                )
                VALUES (%s, %s)

                ON CONFLICT (
                    user_id,
                    post_id
                )
                DO NOTHING

                RETURNING id
                """,
                (
                    user_id,
                    post_id,
                ),
            )

            inserted = cur.fetchone()


            if inserted is not None:

                cur.execute(
                    """
                    UPDATE posts
                    SET like_count =
                        like_count + 1
                    WHERE id = %s
                    """,
                    (post_id,),
                )


            cur.execute(
                """
                SELECT like_count
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            like_count = cur.fetchone()[0]


    return {
        "post_id": post_id,
        "like_count": like_count,
        "liked": True,
    }


# MARK: - Unlike

@router.delete("/{post_id}/likes")
def unlike_post(
    post_id: int,
    user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="post not found",
                )


            cur.execute(
                """
                DELETE FROM post_likes
                WHERE user_id = %s
                AND post_id = %s
                RETURNING id
                """,
                (
                    user_id,
                    post_id,
                ),
            )

            deleted = cur.fetchone()


            if deleted is not None:

                cur.execute(
                    """
                    UPDATE posts
                    SET like_count =
                        GREATEST(
                            like_count - 1,
                            0
                        )
                    WHERE id = %s
                    """,
                    (post_id,),
                )


            cur.execute(
                """
                SELECT like_count
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            like_count = cur.fetchone()[0]


    return {
        "post_id": post_id,
        "like_count": like_count,
        "liked": False,
    }


# MARK: - Dislike

@router.post("/{post_id}/dislikes")
def dislike_post(
    post_id: int,
    user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM posts
                WHERE id = %s
                """,
                (post_id,),
            )

            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="post not found",
                )


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
                INSERT INTO post_dislikes (
                    user_id,
                    post_id
                )
                VALUES (%s, %s)

                ON CONFLICT (
                    user_id,
                    post_id
                )
                DO NOTHING
                """,
                (
                    user_id,
                    post_id,
                ),
            )


    return {
        "post_id": post_id,
        "disliked": True,
    }