from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from database import get_connection

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
JST = ZoneInfo("Asia/Tokyo")


def ensure_campaign_tables():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    start_date DATE,
                    end_date DATE NOT NULL,
                    prize_total TEXT NOT NULL DEFAULT '',
                    rules TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    payout_method TEXT NOT NULL DEFAULT 'PayPay',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS campaign_prizes (
                    id BIGSERIAL PRIMARY KEY,
                    campaign_id BIGINT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    ranking_type TEXT NOT NULL,
                    rank_range TEXT NOT NULL,
                    amount TEXT NOT NULL DEFAULT '',
                    UNIQUE(campaign_id, ranking_type, rank_range)
                );
            """)

            # Migrate the first campaign schema if it already exists.
            cur.execute("ALTER TABLE campaigns ALTER COLUMN start_date DROP NOT NULL")
            cur.execute("ALTER TABLE campaigns ALTER COLUMN prize_total TYPE TEXT USING prize_total::TEXT")
            cur.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS payout_method TEXT NOT NULL DEFAULT 'PayPay'")
            cur.execute("ALTER TABLE campaign_prizes ALTER COLUMN rank TYPE TEXT USING rank::TEXT")
            cur.execute("ALTER TABLE campaign_prizes ALTER COLUMN amount TYPE TEXT USING amount::TEXT")
            cur.execute("ALTER TABLE campaign_prizes ADD COLUMN IF NOT EXISTS ranking_type TEXT")
            cur.execute("ALTER TABLE campaign_prizes ADD COLUMN IF NOT EXISTS rank_range TEXT")
            cur.execute("UPDATE campaign_prizes SET ranking_type = category WHERE ranking_type IS NULL OR ranking_type = ''")
            cur.execute("UPDATE campaign_prizes SET rank_range = rank WHERE rank_range IS NULL OR rank_range = ''")
            cur.execute("ALTER TABLE campaign_prizes ALTER COLUMN ranking_type SET NOT NULL")
            cur.execute("ALTER TABLE campaign_prizes ALTER COLUMN rank_range SET NOT NULL")
            cur.execute("ALTER TABLE campaign_prizes DROP CONSTRAINT IF EXISTS campaign_prizes_campaign_id_category_rank_key")
            cur.execute("ALTER TABLE campaign_prizes DROP CONSTRAINT IF EXISTS campaign_prizes_rank_check")
            cur.execute("ALTER TABLE campaign_prizes DROP CONSTRAINT IF EXISTS campaign_prizes_amount_check")
            cur.execute("ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS campaigns_end_date_start_date_check")

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_campaigns_dates ON campaigns(start_date, end_date);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_campaign_prizes_campaign ON campaign_prizes(campaign_id);
            """)
            conn.commit()


def _status(start_date, end_date) -> str:
    today = datetime.now(JST).date()
    if start_date is None:
        return "start_date_unknown"
    if today < start_date:
        return "upcoming"
    if today > end_date:
        return "ended"
    return "active"


def _serialize_campaign(row, prizes):
    start_date = row[3]
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": row[4].isoformat(),
        "prize_total": row[5],
        "rules": row[6],
        "notes": row[7],
        "payout_method": row[8] or "PayPay",
        "status": _status(start_date, row[4]),
        "prizes": [
            {"ranking_type": p[0], "rank_range": p[1], "amount": p[2]}
            for p in prizes
        ],
    }


@router.get("")
def list_campaigns():
    today = datetime.now(JST).date()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, description, start_date, end_date,
                       prize_total, rules, notes, payout_method
                FROM campaigns
                WHERE end_date >= %s
                ORDER BY
                    CASE WHEN start_date IS NOT NULL AND start_date <= %s AND end_date >= %s THEN 0 ELSE 1 END,
                    CASE WHEN start_date IS NULL THEN 1 ELSE 0 END,
                    start_date ASC NULLS LAST,
                    id DESC
            """, (today, today, today))
            rows = cur.fetchall()
            result = []
            for row in rows:
                cur.execute("""
                    SELECT ranking_type, rank_range, amount
                    FROM campaign_prizes
                    WHERE campaign_id = %s
                    ORDER BY id ASC
                """, (row[0],))
                result.append(_serialize_campaign(row, cur.fetchall()))
            return result


@router.get("/{campaign_id}")
def get_campaign(campaign_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, description, start_date, end_date,
                       prize_total, rules, notes, payout_method
                FROM campaigns WHERE id = %s
            """, (campaign_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="campaign not found")
            cur.execute("""
                SELECT ranking_type, rank_range, amount
                FROM campaign_prizes WHERE campaign_id = %s ORDER BY id ASC
            """, (campaign_id,))
            return _serialize_campaign(row, cur.fetchall())
