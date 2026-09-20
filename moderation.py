from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_connection

router = APIRouter(prefix="/moderation", tags=["moderation"])


class ReportCreate(BaseModel):
    reporter_id: int
    target_type: str
    target_id: int
    reason: str


class BlockRequest(BaseModel):
    blocker_id: int
    blocked_id: int


@router.post("/reports")
def create_report(data: ReportCreate):
    if data.target_type not in {"post", "user"}:
        raise HTTPException(status_code=400, detail="invalid target_type")

    if data.reason not in {"sensitive", "offensive", "non_realtime", "other"}:
        raise HTTPException(status_code=400, detail="invalid reason")

    if data.target_type == "user" and data.reporter_id == data.target_id:
        raise HTTPException(status_code=400, detail="cannot report yourself")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE id = %s",
                (data.reporter_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="reporter not found")

            if data.target_type == "post":
                cur.execute(
                    "SELECT id FROM posts WHERE id = %s",
                    (data.target_id,),
                )
            else:
                cur.execute(
                    "SELECT id FROM users WHERE id = %s",
                    (data.target_id,),
                )

            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="target not found")

            cur.execute(
                """
                INSERT INTO reports (
                    reporter_id, target_type, target_id, reason
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (reporter_id, target_type, target_id)
                DO NOTHING
                RETURNING id
                """,
                (
                    data.reporter_id,
                    data.target_type,
                    data.target_id,
                    data.reason,
                ),
            )
            row = cur.fetchone()

    return {
        "reported": True,
        "already_reported": row is None,
    }


@router.post("/blocks")
def block_user(data: BlockRequest):
    if data.blocker_id == data.blocked_id:
        raise HTTPException(status_code=400, detail="cannot block yourself")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE id = %s",
                (data.blocker_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="blocker not found")

            cur.execute(
                "SELECT id FROM users WHERE id = %s",
                (data.blocked_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="blocked user not found")

            # Blocking also removes the follow relationship in both directions.
            cur.execute(
                """
                DELETE FROM follows
                WHERE (follower_id = %s AND following_id = %s)
                   OR (follower_id = %s AND following_id = %s)
                """,
                (
                    data.blocker_id, data.blocked_id,
                    data.blocked_id, data.blocker_id,
                ),
            )

            cur.execute(
                """
                INSERT INTO blocks (blocker_id, blocked_id)
                VALUES (%s, %s)
                ON CONFLICT (blocker_id, blocked_id)
                DO NOTHING
                """,
                (data.blocker_id, data.blocked_id),
            )

    return {"blocked": True}


@router.get("/blocks/{blocker_id}")
def get_blocked_users(blocker_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.id,
                    u.display_name,
                    u.profile_image
                FROM blocks b
                JOIN users u
                    ON u.id = b.blocked_id
                WHERE b.blocker_id = %s
                ORDER BY b.created_at DESC, b.id DESC
                """,
                (blocker_id,),
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


@router.delete("/blocks/{blocker_id}/{blocked_id}")
def unblock_user(blocker_id: int, blocked_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM blocks
                WHERE blocker_id = %s
                  AND blocked_id = %s
                """,
                (blocker_id, blocked_id),
            )
    return {"blocked": False}


@router.get("/blocks/{blocker_id}/{blocked_id}")
def get_block_status(blocker_id: int, blocked_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM blocks
                WHERE blocker_id = %s
                  AND blocked_id = %s
                """,
                (blocker_id, blocked_id),
            )
            row = cur.fetchone()
    return {"blocked": row is not None}
