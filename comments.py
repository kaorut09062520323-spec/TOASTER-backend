from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_connection

router = APIRouter(prefix="/posts", tags=["comments"])


class CreateCommentRequest(BaseModel):
    user_id: int
    content: str


@router.get("/{post_id}/comments")
def get_comments(post_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.user_id, u.display_name, c.content, c.created_at
                FROM comments c
                JOIN users u ON u.id = c.user_id
                WHERE c.post_id = %s
                ORDER BY c.created_at ASC
                """,
                (post_id,),
            )
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "user_id": row[1],
            "display_name": row[2],
            "content": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


@router.post("/{post_id}/comments")
def create_comment(post_id: int, data: CreateCommentRequest):
    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="comment is empty")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM posts WHERE id = %s", (post_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="post not found")

            cur.execute("SELECT id, display_name FROM users WHERE id = %s", (data.user_id,))
            user = cur.fetchone()
            if user is None:
                raise HTTPException(status_code=404, detail="user not found")

            cur.execute(
                """
                INSERT INTO comments (user_id, post_id, content)
                VALUES (%s, %s, %s)
                RETURNING id, created_at
                """,
                (data.user_id, post_id, content),
            )
            row = cur.fetchone()

    return {
        "id": row[0],
        "user_id": data.user_id,
        "display_name": user[1],
        "content": content,
        "created_at": row[1],
    }
