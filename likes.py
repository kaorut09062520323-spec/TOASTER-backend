from fastapi import APIRouter, HTTPException

from database import get_connection
from notifications import send_push_notification


router = APIRouter(prefix="/posts", tags=["likes"])


@router.post("/{post_id}/like")
def like_post(post_id: int, user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, user_id
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
                FROM post_likes
                WHERE user_id = %s
                  AND post_id = %s
                """,
                (user_id, post_id),
            )

            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="already liked",
                )

            cur.execute(
                """
                INSERT INTO post_likes (
                    user_id,
                    post_id
                )
                VALUES (%s, %s)
                """,
                (user_id, post_id),
            )

            cur.execute(
                """
                UPDATE posts
                SET like_count = like_count + 1
                WHERE id = %s
                RETURNING like_count
                """,
                (post_id,),
            )

            like_count = cur.fetchone()[0]
            post_owner_id = post[1]

    if post_owner_id != user_id:

        send_push_notification(
            post_owner_id,
            "いいね",
            "あなたの投稿にいいねがつきました。",
            data={
                "post_id": post_id,
                "type": "like",
            },
        )

    return {
        "post_id": post_id,
        "user_id": user_id,
        "liked": True,
        "like_count": like_count,
    }


@router.delete("/{post_id}/like")
def unlike_post(
    post_id: int,
    user_id: int,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM post_likes
                WHERE user_id = %s
                  AND post_id = %s
                RETURNING id
                """,
                (user_id, post_id),
            )

            deleted = cur.fetchone()

            if deleted is None:
                raise HTTPException(
                    status_code=404,
                    detail="like not found",
                )

            cur.execute(
                """
                UPDATE posts
                SET like_count = GREATEST(
                    like_count - 1,
                    0
                )
                WHERE id = %s
                RETURNING like_count
                """,
                (post_id,),
            )

            row = cur.fetchone()

            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail="post not found",
                )

            like_count = row[0]

    return {
        "post_id": post_id,
        "user_id": user_id,
        "liked": False,
        "like_count": like_count,
    }


@router.get("/{post_id}/likes")
def get_post_likes(
    post_id: int,
    user_id: int | None = None,
):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, like_count
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

            like_count = post[1]

            liked = False

            if user_id is not None:

                cur.execute(
                    """
                    SELECT id
                    FROM post_likes
                    WHERE user_id = %s
                      AND post_id = %s
                    """,
                    (user_id, post_id),
                )

                liked = cur.fetchone() is not None

    return {
        "post_id": post_id,
        "like_count": like_count,
        "liked": liked,
    }