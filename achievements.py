from datetime import date

from fastapi import APIRouter, HTTPException

from database import get_connection


router = APIRouter(prefix="/users", tags=["achievements"])


ACHIEVEMENT_TYPES = (
    "overall_top",
    "fashion_top",
    "hair_style_top",
    "food_top",
    "spot_top",
    "early_member",
)


def grant_achievement(
    cur,
    user_id: int,
    achievement_type: str,
    achieved_date: date | None,
) -> bool:
    """Grant one achievement once for the specified date when applicable."""
    if achievement_type not in ACHIEVEMENT_TYPES:
        raise ValueError("invalid achievement type")

    if achievement_type == "early_member":
        # This achievement is intentionally date-independent and is granted
        # only by the registration flow while early-member mode is enabled.
        cur.execute(
            """
            INSERT INTO user_achievements (
                user_id,
                achievement_id,
                ranking_date
            )
            SELECT
                %s,
                a.id,
                NULL
            FROM achievements a
            WHERE a.achievement_type = %s
              AND a.is_active = TRUE
            ON CONFLICT (user_id, achievement_id, ranking_date)
            DO NOTHING
            RETURNING id
            """,
            (user_id, achievement_type),
        )
    else:
        cur.execute(
            """
            INSERT INTO user_achievements (
                user_id,
                achievement_id,
                ranking_date
            )
            SELECT
                %s,
                a.id,
                %s
            FROM achievements a
            WHERE a.achievement_type = %s
              AND a.is_active = TRUE
            ON CONFLICT (user_id, achievement_id, ranking_date)
            DO NOTHING
            RETURNING id
            """,
            (user_id, achieved_date, achievement_type),
        )

    return cur.fetchone() is not None


def grant_daily_top_achievements(cur, ranking_date: date) -> int:
    """Grant TOP achievements from an already-finalized daily ranking."""
    granted = 0

    for category, achievement_type in (
        ("all", "overall_top"),
        ("fashion", "fashion_top"),
        ("hair_style", "hair_style_top"),
        ("food", "food_top"),
        ("spot", "spot_top"),
    ):
        cur.execute(
            """
            SELECT p.user_id
            FROM daily_rankings dr
            JOIN posts p ON p.id = dr.post_id
            WHERE dr.ranking_date = %s
              AND dr.category = %s
              AND dr.rank = 1
            LIMIT 1
            """,
            (ranking_date, category),
        )

        row = cur.fetchone()
        if row is not None:
            if grant_achievement(
                cur,
                row[0],
                achievement_type,
                ranking_date,
            ):
                granted += 1

    return granted


@router.get("/{user_id}/achievements")
def get_user_achievements(user_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.achievement_type,
                    a.name,
                    a.description,
                    a.icon,
                    ua.ranking_date,
                    ua.created_at
                FROM user_achievements ua
                JOIN achievements a
                    ON a.id = ua.achievement_id
                WHERE ua.user_id = %s
                  AND a.is_active = TRUE
                ORDER BY
                    ua.created_at DESC,
                    ua.id DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    return [
        {
            "achievement_type": row[0],
            "name": row[1],
            "description": row[2],
            "icon": row[3],
            "ranking_date": (
                row[4].isoformat()
                if row[4]
                else None
            ),
            "created_at": (
                row[5].isoformat()
                if row[5]
                else None
            ),
        }
        for row in rows
    ]
