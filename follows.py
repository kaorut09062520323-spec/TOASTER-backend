from fastapi import APIRouter, HTTPException

from database import get_connection
from notifications import send_push_notification


router = APIRouter(
    prefix="/users",
    tags=["follows"],
)


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
                SELECT id, display_name
                FROM users
                WHERE id = %s
                """,
                (target_user_id,),
            )

            target_user = cur.fetchone()

            if target_user is None:
                raise HTTPException(
                    status_code=404,
                    detail="target user not found",
                )

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

            if cur.fetchone() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="already following",
                )

            cur.execute(
                """
                INSERT INTO follows (
                    follower_id,
                    following_id
                )
                VALUES (%s, %s)
                """,
                (
                    user_id,
                    target_user_id,
                ),
            )

    send_push_notification(
        target_user_id,
        "フォロー",
        "あなたをフォローしました。",
        data={
            "user_id": user_id,
            "type": "follow",
        },
    )

    return {
        "user_id": user_id,
        "target_user_id": target_user_id,
        "following": True,
    }


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
                RETURNING id
                """,
                (
                    user_id,
                    target_user_id,
                ),
            )

            deleted = cur.fetchone()

            if deleted is None:
                raise HTTPException(
                    status_code=404,
                    detail="follow not found",
                )

    return {
        "user_id": user_id,
        "target_user_id": target_user_id,
        "following": False,
    }


@router.get("/{user_id}/followers")
def get_followers(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    u.id
                FROM follows f
                JOIN users u
                    ON u.id = f.follower_id
                WHERE f.following_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )

            rows = cur.fetchall()

    return {
        "user_id": user_id,
        "followers": [
            row[0]
            for row in rows
        ],
    }


@router.get("/{user_id}/following")
def get_following(user_id: int):

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    u.id
                FROM follows f
                JOIN users u
                    ON u.id = f.following_id
                WHERE f.follower_id = %s
                ORDER BY f.created_at DESC
                """,
                (user_id,),
            )

            rows = cur.fetchall()

    return {
        "user_id": user_id,
        "following": [
            row[0]
            for row in rows
        ],
    }