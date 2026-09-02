from database import get_connection


CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    display_name TEXT NOT NULL,
    profile_image TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE users
ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT '';

ALTER TABLE users
ADD COLUMN IF NOT EXISTS profile_image_key TEXT;

ALTER TABLE users
ADD COLUMN IF NOT EXISTS background_image_key TEXT;

CREATE TABLE IF NOT EXISTS auth_accounts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(provider, provider_user_id)
);

CREATE TABLE IF NOT EXISTS posts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE posts
ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'fashion';

ALTER TABLE posts
ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

UPDATE posts
SET expires_at = created_at + INTERVAL '24 hours'
WHERE expires_at IS NULL;

CREATE TABLE IF NOT EXISTS post_likes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(user_id, post_id)
);

CREATE TABLE IF NOT EXISTS post_dislikes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(user_id, post_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS follows (
    id BIGSERIAL PRIMARY KEY,
    follower_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    following_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(follower_id, following_id),
    CHECK(follower_id <> following_id)
);

ALTER TABLE posts
ADD COLUMN IF NOT EXISTS like_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE posts
ADD COLUMN IF NOT EXISTS image_key TEXT;

-- Every post has a 24-hour photo lifetime. Existing NULL values are backfilled,
-- then the default keeps newly-created posts on the same rule.
ALTER TABLE posts
ALTER COLUMN expires_at SET DEFAULT (CURRENT_TIMESTAMP + INTERVAL '24 hours');

UPDATE posts
SET expires_at = created_at + INTERVAL '24 hours'
WHERE expires_at IS NULL;

CREATE TABLE IF NOT EXISTS daily_ranking_snapshots (
    ranking_date DATE PRIMARY KEY,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_rankings (
    id BIGSERIAL PRIMARY KEY,
    ranking_date DATE NOT NULL REFERENCES daily_ranking_snapshots(ranking_date) ON DELETE CASCADE,
    category TEXT NOT NULL,
    rank INTEGER NOT NULL,
    post_id BIGINT NOT NULL REFERENCES posts(id),
    like_count INTEGER NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(ranking_date, category, rank),
    UNIQUE(ranking_date, category, post_id),
    CHECK(rank > 0),
    CHECK(like_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_daily_rankings_date_category
ON daily_rankings(ranking_date, category, rank);

CREATE TABLE IF NOT EXISTS achievements (
    id BIGSERIAL PRIMARY KEY,
    achievement_type TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    icon TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blocks (
    id BIGSERIAL PRIMARY KEY,
    blocker_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(blocker_id, blocked_id),
    CHECK(blocker_id <> blocked_id)
);

CREATE INDEX IF NOT EXISTS idx_blocks_blocker
ON blocks(blocker_id);

CREATE INDEX IF NOT EXISTS idx_blocks_blocked
ON blocks(blocked_id);

CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    reporter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_id BIGINT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(reporter_id, target_type, target_id),
    CHECK(target_type IN ('post', 'user')),
    CHECK(reason IN ('sensitive', 'offensive', 'non_realtime', 'other'))
);

CREATE INDEX IF NOT EXISTS idx_reports_created_at
ON reports(created_at DESC);

CREATE TABLE IF NOT EXISTS user_achievements (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    achievement_id BIGINT NOT NULL REFERENCES achievements(id) ON DELETE CASCADE,
    ranking_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, achievement_id, ranking_date)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_achievement_once
ON user_achievements(user_id, achievement_id)
WHERE ranking_date IS NULL;

INSERT INTO achievements (
    achievement_type,
    name,
    description,
    icon,
    is_active
)
VALUES
    ('overall_top', '総合TOP', 'その日の総合ランキング1位', '🏆', TRUE),
    ('fashion_top', 'Fashion TOP', 'その日のFashionランキング1位', '👗', TRUE),
    ('hair_style_top', 'Hair Style TOP', 'その日のHair Styleランキング1位', '💇', TRUE),
    ('food_top', 'Food TOP', 'その日のFoodランキング1位', '🍔', TRUE),
    ('spot_top', 'Spot TOP', 'その日のSpotランキング1位', '📍', TRUE),
    ('early_member', '始まりのユーザー', 'TOASTER初期メンバー限定の称号', '🌱', TRUE)
ON CONFLICT (achievement_type) DO NOTHING;

"""


def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLES)

    print("Database initialized successfully.")


if __name__ == "__main__":
    init_db()