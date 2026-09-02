from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_connection

router = APIRouter(prefix="/support", tags=["support"])


class SendMessageRequest(BaseModel):
    user_id: int
    body: str


class ReadMessagesRequest(BaseModel):
    user_id: int


def ensure_conversation(cur, user_id: int):
    cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
    if cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="user not found")

    cur.execute(
        """
        INSERT INTO support_conversations(user_id)
        VALUES (%s)
        ON CONFLICT (user_id) DO NOTHING
        RETURNING id
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "SELECT id FROM support_conversations WHERE user_id = %s",
        (user_id,),
    )
    return cur.fetchone()[0]


@router.get("/conversation")
def get_conversation(user_id: int):
    with get_connection() as conn, conn.cursor() as cur:
        conversation_id = ensure_conversation(cur, user_id)
        cur.execute(
            """
            SELECT id, user_id, created_at, updated_at
            FROM support_conversations
            WHERE id = %s
            """,
            (conversation_id,),
        )
        row = cur.fetchone()

    return {
        "id": row[0],
        "user_id": row[1],
        "created_at": row[2],
        "updated_at": row[3],
    }


@router.get("/messages")
def get_messages(user_id: int):
    with get_connection() as conn, conn.cursor() as cur:
        conversation_id = ensure_conversation(cur, user_id)

        # Opening the chat marks messages from the admin as read.
        cur.execute(
            """
            UPDATE support_messages
            SET read_at = CURRENT_TIMESTAMP
            WHERE conversation_id = %s
              AND sender_type = 'admin'
              AND read_at IS NULL
            """,
            (conversation_id,),
        )

        cur.execute(
            """
            SELECT id, sender_type, body, created_at, read_at
            FROM support_messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (conversation_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "sender_type": row[1],
            "body": row[2],
            "created_at": row[3],
            "read_at": row[4],
        }
        for row in rows
    ]


@router.post("/messages")
def send_message(request: SendMessageRequest):
    body = request.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="message body required")
    if len(body) > 5000:
        raise HTTPException(status_code=400, detail="message too long")

    with get_connection() as conn, conn.cursor() as cur:
        conversation_id = ensure_conversation(cur, request.user_id)
        cur.execute(
            """
            INSERT INTO support_messages(
                conversation_id,
                sender_type,
                body
            )
            VALUES (%s, 'user', %s)
            RETURNING id, sender_type, body, created_at, read_at
            """,
            (conversation_id, body),
        )
        row = cur.fetchone()

        cur.execute(
            """
            UPDATE support_conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (conversation_id,),
        )
        conn.commit()

    return {
        "id": row[0],
        "sender_type": row[1],
        "body": row[2],
        "created_at": row[3],
        "read_at": row[4],
    }


@router.get("/unread")
def unread_count(user_id: int):
    with get_connection() as conn, conn.cursor() as cur:
        conversation_id = ensure_conversation(cur, user_id)
        cur.execute(
            """
            SELECT COUNT(*)
            FROM support_messages
            WHERE conversation_id = %s
              AND sender_type = 'admin'
              AND read_at IS NULL
            """,
            (conversation_id,),
        )
        count = cur.fetchone()[0]

    return {"unread_count": count}
