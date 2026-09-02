from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from database import get_connection
from achievements import grant_daily_top_achievements


router = APIRouter(prefix="/ranking", tags=["ranking"])

JST = ZoneInfo("Asia/Tokyo")

CATEGORIES = ("all", "fashion", "hair_style", "food", "spot")


# MARK: - Today / Provisional Ranking


def _today_range_jst():
    """Return today's JST calendar-day range."""
    today = datetime.now(JST).date()
    return today, *_day_range_jst(today)


@router.get("/today")
def get_today_ranking(category: str = "all"):
    """
    Return today's provisional ranking.

    Today is intentionally NOT read from daily_rankings. It is calculated
    directly from posts using the current like_count.

    A post participates when its 24-hour lifetime overlaps any part
    of today's JST calendar day.

    Ranking order:
        1. More likes first
        2. If likes are equal, newer post first
        3. If created_at is also equal, larger post_id first
    """
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail="invalid category"
        )

    target_date, day_start, day_end = _today_range_jst()

    category_clause = ""
    params: list = [day_end, day_start]

    if category != "all":
        category_clause = "AND p.category = %s"
        params.append(category)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    p.id,
                    p.user_id,
                    u.display_name,
                    p.category,
                    p.image_key,
                    p.like_count,
                    p.created_at,
                    p.expires_at
                FROM posts p
                JOIN users u
                    ON u.id = p.user_id
                WHERE p.image_key IS NOT NULL
                  AND p.created_at < %s
                  AND p.expires_at > %s
                  {category_clause}
                ORDER BY
                    p.like_count DESC,
                    p.created_at DESC,
                    p.id DESC
                LIMIT 100
                """,
                params,
            )

            rows = cur.fetchall()

    return [
        {
            "post_id": row[0],
            "rank": rank,
            "user_id": row[1],
            "display_name": row[2],
            "post_category": row[3],
            "image_key": row[4],
            "like_count": row[5],
            "created_at": (
                row[6].isoformat()
                if row[6]
                else None
            ),
            "expires_at": (
                row[7].isoformat()
                if row[7]
                else None
            ),
            "ranking_date": target_date.isoformat(),
        }
        for rank, row in enumerate(rows, start=1)
    ]


# MARK: - Daily Ranking Helpers


def _day_range_jst(target_date: date):
    start = datetime.combine(
        target_date,
        time.min,
        tzinfo=JST,
    )

    end = start + timedelta(days=1)

    return start, end


def snapshot_daily_ranking(
    target_date: date | None = None
) -> dict:
    """
    Freeze the ranking for a calendar day at 00:00 JST.

    A post belongs to the day's comparison set when its lifetime overlaps
    any part of that calendar day:

        created_at < day_end
        AND
        expires_at > day_start

    Ranking order:
        1. More likes first
        2. If likes are equal, newer post first
        3. If created_at is also equal, larger post_id first
    """
    now_jst = datetime.now(JST)

    if target_date is None:
        target_date = (
            now_jst.date()
            - timedelta(days=1)
        )

    if target_date >= now_jst.date():
        raise ValueError(
            "daily ranking can only be finalized for a past date"
        )

    day_start, day_end = _day_range_jst(
        target_date
    )

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Prevent two snapshot jobs for the same date
            # from running simultaneously.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (
                    f"toaster-daily-ranking:"
                    f"{target_date.isoformat()}",
                ),
            )

            # Check whether this date has already been finalized.
            cur.execute(
                """
                SELECT snapshot_at
                FROM daily_ranking_snapshots
                WHERE ranking_date = %s
                """,
                (target_date,),
            )

            existing_snapshot = cur.fetchone()

            if existing_snapshot is not None:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM daily_rankings
                    WHERE ranking_date = %s
                    """,
                    (target_date,),
                )

                count = cur.fetchone()[0]

                snapshot_at = existing_snapshot[0]

                # The snapshot may have been created before achievement
                # support was deployed. Re-running is therefore allowed to
                # backfill the five TOP achievements idempotently.
                granted_achievements = grant_daily_top_achievements(
                    cur,
                    target_date,
                )

                return {
                    "ranking_date":
                        target_date.isoformat(),
                    "snapshot_at": (
                        snapshot_at.isoformat()
                        if snapshot_at
                        else None
                    ),
                    "inserted_count": count,
                    "granted_achievements": granted_achievements,
                    "already_exists": True,
                }

            # Create the snapshot record first.
            cur.execute(
                """
                INSERT INTO daily_ranking_snapshots (
                    ranking_date
                )
                VALUES (%s)
                RETURNING snapshot_at
                """,
                (target_date,),
            )

            snapshot_at = cur.fetchone()[0]

            inserted_count = 0

            # Create rankings for ALL categories:
            #
            # all
            # fashion
            # hair_style
            # food
            # view
            #
            for category in CATEGORIES:

                category_clause = ""
                params: list = [
                    day_end,
                    day_start,
                ]

                if category != "all":
                    category_clause = (
                        "AND p.category = %s"
                    )
                    params.append(category)

                cur.execute(
                    f"""
                    SELECT
                        p.id,
                        p.like_count,
                        p.created_at
                    FROM posts p
                    WHERE p.image_key IS NOT NULL
                      AND p.created_at < %s
                      AND p.expires_at > %s
                      {category_clause}
                    ORDER BY
                        p.like_count DESC,
                        p.created_at DESC,
                        p.id DESC
                    LIMIT 100
                    """,
                    params,
                )

                rows = cur.fetchall()

                for rank, row in enumerate(
                    rows,
                    start=1,
                ):
                    cur.execute(
                        """
                        INSERT INTO daily_rankings (
                            ranking_date,
                            category,
                            rank,
                            post_id,
                            like_count,
                            snapshot_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s
                        )
                        """,
                        (
                            target_date,
                            category,
                            rank,
                            row[0],
                            row[1],
                            snapshot_at,
                        ),
                    )

                    inserted_count += 1

            granted_achievements = grant_daily_top_achievements(
                cur,
                target_date,
            )

    return {
        "ranking_date":
            target_date.isoformat(),
        "snapshot_at": (
            snapshot_at.isoformat()
            if snapshot_at
            else None
        ),
        "inserted_count": inserted_count,
        "granted_achievements": granted_achievements,
        "already_exists": False,
    }


# MARK: - Snapshot Job


@router.post("/daily/snapshot")
def create_daily_ranking_snapshot(
    target_date: date | None = None
):
    """
    Finalize a past day.

    Without a date, finalize yesterday in JST.
    """
    try:
        return snapshot_daily_ranking(
            target_date
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )


# MARK: - Daily Ranking Read


def _serialize_daily_row(row):
    return {
        "ranking_date": row[0].isoformat(),
        "category": row[1],
        "rank": row[2],
        "post_id": row[3],
        "user_id": row[4],
        "display_name": row[5],
        "post_category": row[6],
        "image_key": row[7],
        "like_count": row[8],
        "created_at": (
            row[9].isoformat()
            if row[9]
            else None
        ),
        "expires_at": (
            row[10].isoformat()
            if row[10]
            else None
        ),
        "snapshot_at": (
            row[11].isoformat()
            if row[11]
            else None
        ),
    }


def _read_daily_rows(
    cur,
    target_date: date,
    category: str,
):
    cur.execute(
        """
        SELECT
            dr.ranking_date,
            dr.category,
            dr.rank,
            dr.post_id,
            u.id,
            u.display_name,
            p.category,
            p.image_key,
            dr.like_count,
            p.created_at,
            p.expires_at,
            dr.snapshot_at
        FROM daily_rankings dr
        JOIN posts p
            ON p.id = dr.post_id
        JOIN users u
            ON u.id = p.user_id
        WHERE dr.ranking_date = %s
          AND dr.category = %s
        ORDER BY dr.rank ASC
        """,
        (
            target_date,
            category,
        ),
    )

    return cur.fetchall()


@router.get("/daily")
def get_daily_ranking(
    target_date: date,
    category: str = "all",
):
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail="invalid category",
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            rows = _read_daily_rows(
                cur,
                target_date,
                category,
            )

            if rows:
                return [
                    _serialize_daily_row(row)
                    for row in rows
                ]

            cur.execute(
                """
                SELECT 1
                FROM daily_ranking_snapshots
                WHERE ranking_date = %s
                """,
                (target_date,),
            )

            snapshot_exists = (
                cur.fetchone() is not None
            )

    if not snapshot_exists:
        raise HTTPException(
            status_code=404,
            detail="daily ranking not found",
        )

    return []


@router.get("/daily/latest")
def get_latest_daily_ranking(
    category: str = "all",
):
    """
    Return the newest finalized day
    and its ranking.
    """
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail="invalid category",
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT MAX(ranking_date)
                FROM daily_ranking_snapshots
                """
            )

            row = cur.fetchone()

            if row is None or row[0] is None:
                raise HTTPException(
                    status_code=404,
                    detail="daily ranking not found",
                )

            target_date = row[0]

            rows = _read_daily_rows(
                cur,
                target_date,
                category,
            )

    return {
        "ranking_date":
            target_date.isoformat(),
        "items": [
            _serialize_daily_row(row)
            for row in rows
        ],
    }


@router.get("/daily/dates")
def get_daily_ranking_dates(
    before: date | None = Query(
        default=None
    ),
    limit: int = Query(
        default=30,
        ge=1,
        le=365,
    ),
):
    """
    Return finalized dates for
    date-picker navigation.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:

            if before is None:
                cur.execute(
                    """
                    SELECT ranking_date
                    FROM daily_ranking_snapshots
                    ORDER BY ranking_date DESC
                    LIMIT %s
                    """,
                    (limit,),
                )

            else:
                cur.execute(
                    """
                    SELECT ranking_date
                    FROM daily_ranking_snapshots
                    WHERE ranking_date <= %s
                    ORDER BY ranking_date DESC
                    LIMIT %s
                    """,
                    (
                        before,
                        limit,
                    ),
                )

            rows = cur.fetchall()

    return [
        row[0].isoformat()
        for row in rows
    ]


# MARK: - Backward Compatibility


# Backward-compatible alias:
# /ranking is today's provisional ranking.
@router.get("")
def get_ranking(
    category: str = "all"
):
    return get_today_ranking(
        category
    )