from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from urllib.parse import parse_qs
import json
import re

from auth import verify_access_token

from database import get_connection
from users import router as users_router
from auth import router as auth_router
from posts import router as posts_router
from likes import router as likes_router
from follows import router as follows_router
from comments import router as comments_router
from ranking import router as ranking_router
from achievements import router as achievements_router
from moderation import router as moderation_router
from messages import router as messages_router
from campaigns import ensure_campaign_tables, router as campaigns_router
from notifications import (
    ensure_notification_tables,
    router as notifications_router,
)


app = FastAPI()



PUBLIC_API_PATHS = (
    "/privacy",
    "/terms",
    "/support",
    "/campaigns",
    "/",
    "/docs",
    "/openapi.json",
)


def _is_public_request(request):
    path = request.url.path
    if path in PUBLIC_API_PATHS or path.startswith("/auth/"):
        return True
    if request.method == "GET":
        public_prefixes = (
            "/posts/", "/ranking", "/users/search", "/campaigns",
        )
        if path == "/posts" or path.startswith(public_prefixes):
            # User-specific like status and achievements/support are protected.
            if "/likes" in path and "user_id=" in str(request.url):
                return False
            if path.startswith("/users/") and path.endswith("/achievements"):
                return False
            return True
        if re.fullmatch(r"/users/\d+", path):
            return True
        if re.fullmatch(r"/users/\d+/(profile|posts|followers|following|follow-counts)", path):
            return True
        if re.fullmatch(r"/users/\d+/(profile-image|background-image)", path):
            return True
        if re.fullmatch(r"/users/\d+/follow/\d+", path):
            return False
    return False


def _extract_identity_ids(request, body: bytes):
    ids = []
    query = parse_qs(request.url.query)
    for key in ("user_id", "reporter_id", "blocker_id"):
        for value in query.get(key, []):
            try:
                ids.append(int(value))
            except ValueError:
                pass

    path = request.url.path
    if path.startswith("/users/"):
        parts = path.split("/")
        if len(parts) > 2 and parts[2].isdigit():
            # First user path component is always the acting user for mutating
            # /users/{user_id}/... endpoints.
            if request.method != "GET" or "/follow/" in path:
                ids.append(int(parts[2]))
    if path.startswith("/moderation/blocks/"):
        parts = path.split("/")
        if len(parts) > 3 and parts[3].isdigit():
            ids.append(int(parts[3]))

    if body:
        text = body.decode("utf-8", errors="ignore")
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    for key in ("user_id", "reporter_id", "blocker_id"):
                        value = payload.get(key)
                        if value is not None:
                            ids.append(int(value))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            for key in ("user_id", "reporter_id", "blocker_id"):
                pattern = rb'name="' + key.encode() + rb'"[^\r\n]*\r\n(?:[^\r\n]*\r\n)*\r\n(\d+)'
                match = re.search(pattern, body)
                if match:
                    ids.append(int(match.group(1)))
            if "application/x-www-form-urlencoded" in content_type:
                form = parse_qs(text)
                for key in ("user_id", "reporter_id", "blocker_id"):
                    for value in form.get(key, []):
                        try:
                            ids.append(int(value))
                        except ValueError:
                            pass
    return ids


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if _is_public_request(request):
            return await call_next(request)

        authorization = request.headers.get("authorization")
        if not authorization or not authorization.startswith("Bearer "):
            return JSONResponse({"detail": "Authorization required"}, status_code=401)

        try:
            current_user_id = verify_access_token(authorization[7:].strip())
        except Exception as exc:
            if isinstance(exc, HTTPException):
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return JSONResponse({"detail": "Invalid or expired access token"}, status_code=401)

        body = await request.body()
        identity_ids = _extract_identity_ids(request, body)
        if any(user_id != current_user_id for user_id in identity_ids):
            return JSONResponse({"detail": "Authenticated user does not match request user"}, status_code=403)

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive
        request.state.user_id = current_user_id
        return await call_next(request)



app.add_middleware(AuthenticationMiddleware)

def ensure_support_tables():

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS support_conversations (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE
                        REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS support_messages (
                    id SERIAL PRIMARY KEY,
                    conversation_id INTEGER NOT NULL
                        REFERENCES support_conversations(id)
                        ON DELETE CASCADE,
                    sender_type TEXT NOT NULL
                        CHECK (sender_type IN ('user', 'admin')),
                    body TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    read_at TIMESTAMP
                );
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_support_messages_conversation_created
                ON support_messages(
                    conversation_id,
                    created_at,
                    id
                );
                """
            )

            conn.commit()


@app.on_event("startup")
def startup():

    ensure_support_tables()
    ensure_campaign_tables()

    ensure_notification_tables()


app.include_router(users_router)
app.include_router(auth_router)
app.include_router(posts_router)
app.include_router(likes_router)
app.include_router(follows_router)
app.include_router(comments_router)
app.include_router(ranking_router)
app.include_router(achievements_router)
app.include_router(moderation_router)
app.include_router(messages_router)
app.include_router(notifications_router)
app.include_router(campaigns_router)


# MARK: - Public Pages

PUBLIC_PAGE_STYLE = """
<style>
    * { box-sizing: border-box; }

    html {
        background: #000;
    }

    body {
        margin: 0;
        background: #000;
        color: #fff;
        font-family:
            -apple-system,
            BlinkMacSystemFont,
            "Helvetica Neue",
            Arial,
            sans-serif;
        line-height: 1.8;
    }

    .container {
        max-width: 800px;
        margin: 0 auto;
        padding: 48px 24px 80px;
    }

    .brand {
        font-size: 14px;
        font-weight: 700;
        letter-spacing: .08em;
        color: #aaa;
        margin-bottom: 10px;
    }

    h1 {
        font-size: 32px;
        line-height: 1.3;
        margin: 0 0 8px;
    }

    h2 {
        font-size: 20px;
        line-height: 1.4;
        margin: 36px 0 10px;
    }

    p,
    li {
        color: #ddd;
    }

    .date {
        color: #777;
        font-size: 13px;
        margin-bottom: 36px;
    }

    .card {
        margin: 18px 0;
        padding: 18px 20px;
        background: #111;
        border-radius: 14px;
    }
</style>
"""


def public_page(
    title: str,
    body: str
) -> str:

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >
    <title>{title} | TOASTER</title>
    {PUBLIC_PAGE_STYLE}
</head>

<body>

    <main class="container">

        <div class="brand">
            TOASTER
        </div>

        <h1>
            {title}
        </h1>

        {body}

    </main>

</body>
</html>
"""


@app.get(
    "/privacy",
    response_class=HTMLResponse
)
def privacy_policy():

    return public_page(
        "プライバシーポリシー",
        """
        <div class="date">
            最終更新日：2026年9月3日
        </div>

        <p>
            TOASTER（以下「本サービス」）は、ユーザーのプライバシーを尊重し、
            個人情報その他の利用者に関する情報を適切に取り扱います。
        </p>

        <h2>1. 取得する情報</h2>

        <p>
            本サービスでは、以下の情報を取得する場合があります。
        </p>

        <ul>
            <li>
                AppleまたはGoogleによるログインに関する情報
            </li>

            <li>
                表示名、プロフィール画像、自己紹介等のプロフィール情報
            </li>

            <li>
                ユーザーが作成した投稿、画像、コメント、いいね、
                フォロー等の情報
            </li>

            <li>
                通報、ブロックその他のサービス利用に関する情報
            </li>

            <li>
                サービスの提供・維持に必要な端末、通信その他の技術情報
            </li>
        </ul>

        <h2>2. 利用目的</h2>

        <ul>
            <li>
                本サービスの提供、認証およびアカウント管理
            </li>

            <li>
                投稿、コメント、いいね、フォロー等の機能の提供
            </li>

            <li>
                通報・ブロック等による安全なコミュニティの維持
            </li>

            <li>
                不正利用、規約違反その他の問題への対応
            </li>

            <li>
                本サービスの改善および品質向上
            </li>

            <li>
                お問い合わせへの対応
            </li>
        </ul>

        <h2>3. 外部サービス</h2>

        <p>
            本サービスでは、ログイン、画像保存、サーバー運用その他の機能のために、
            外部サービスを利用する場合があります。外部サービスにおける情報の
            取り扱いについては、各サービス提供者のポリシーも適用されます。
        </p>

        <h2>4. 第三者への提供</h2>

        <p>
            法令に基づく場合、生命・身体・財産の保護のために必要な場合、その他
            法令上認められる場合を除き、取得した情報を本人の同意なく第三者へ
            提供することは原則としてありません。
        </p>

        <h2>5. 安全管理</h2>

        <p>
            取得した情報について、漏えい、紛失、不正アクセス等を防止するため、
            適切な安全管理措置を講じるよう努めます。
        </p>

        <h2>6. アカウントの削除</h2>

        <p>
            ユーザーはアプリ内のアカウント削除機能からアカウントを削除できます。
            削除後は、サービス提供に不要となったアカウント関連情報を適切に削除します。
            ただし、法令上保存が必要な情報等については、必要な期間保存する場合があります。
        </p>

        <h2>7. ポリシーの変更</h2>

        <p>
            本ポリシーは、必要に応じて変更されることがあります。重要な変更については、
            本サービス上または適切な方法でお知らせします。
        </p>

        <h2>8. お問い合わせ</h2>

        <p>
            本サービスに関するお問い合わせは、アプリ内の「運営とのチャット」から
            ご連絡ください。
        </p>
        """
    )


@app.get(
    "/terms",
    response_class=HTMLResponse
)
def terms_of_service():

    return public_page(
        "利用規約",
        """
        <div class="date">
            最終更新日：2026年9月3日
        </div>

        <p>
            本利用規約（以下「本規約」）は、TOASTERが提供するサービス
            （以下「本サービス」）の利用条件を定めるものです。
            ユーザーは、本サービスを利用することで本規約に同意したものとします。
        </p>

        <h2>1. アカウント</h2>

        <p>
            ユーザーは、自身のアカウントを適切に管理する責任を負います。
            アカウントを第三者に不正利用させたり、
            第三者になりすましたりしてはなりません。
        </p>

        <h2>2. 投稿コンテンツ</h2>

        <p>
            ユーザーが本サービスへ投稿した文章、画像その他のコンテンツについては、
            ユーザー自身が責任を負うものとします。
            他者の権利を侵害するコンテンツを投稿してはなりません。
        </p>

        <h2>3. 禁止事項</h2>

        <p>
            ユーザーは、以下の行為を行ってはなりません。
        </p>

        <ul>
            <li>
                法令または公序良俗に反する行為
            </li>

            <li>
                他者への誹謗中傷、脅迫、嫌がらせその他の迷惑行為
            </li>

            <li>
                他人になりすます行為
            </li>

            <li>
                スパム、過度な宣伝その他サービスの正常な利用を妨げる行為
            </li>

            <li>
                他者の著作権、肖像権、プライバシーその他の権利を侵害する行為
            </li>

            <li>
                リアルタイムで撮っていない写真の投稿
            </li>

            <li>
                不正アクセス、サービスへの攻撃その他のセキュリティを害する行為
            </li>

            <li>
                その他、TOASTERが不適切と判断する行為
            </li>
        </ul>

        <h2>4. 通報・ブロック</h2>

        <p>
            ユーザーは、本サービス上の通報・ブロック機能を利用できます。
            違反行為が確認された場合、投稿の削除、アカウントの利用制限、
            アカウント停止等の措置を行う場合があります。
        </p>

        <h2>5. コンテンツの削除</h2>

        <p>
            本規約または法令に違反するコンテンツ、その他本サービスの運営上不適切と判断した
            コンテンツについて、事前の通知なく削除または非表示とする場合があります。
        </p>

        <h2>6. サービスの変更・停止</h2>

        <p>
            TOASTERは、必要に応じて本サービスの内容を変更し、
            または一時的に停止する場合があります。
        </p>

        <h2>7. アカウントの削除</h2>

        <p>
            ユーザーはアプリ内の機能から自身のアカウントを削除できます。
            また、本規約への違反その他の事情により、
            アカウントの利用を制限または停止する場合があります。
        </p>

        <h2>8. キャンペーン</h2>

        <p>
            TOASTERでは、本サービス上で期間限定のキャンペーンを実施する場合があります。
            キャンペーンの開催期間、対象となるランキング、賞金その他の条件については、
            各キャンペーンの詳細ページに表示する内容を基本とします。
        </p>

        <h3>8-1. 参加条件</h3>
        <p>
            キャンペーンへの参加には、各キャンペーンで定める条件を満たす必要があります。
            キャンペーン期間、対象カテゴリー、対象投稿その他の条件は、キャンペーンごとに異なる場合があります。
        </p>

        <h3>8-2. ランキングおよび賞金</h3>
        <p>
            キャンペーンにおける受賞順位および賞金額は、各キャンペーンの詳細ページに表示します。
            賞金の対象順位は、1位のみの場合に限らず、複数の順位または順位の範囲を対象とする場合があります。
            同一キャンペーン内でランキングごとに賞金額や対象順位が異なる場合があります。
        </p>

        <p>
            キャンペーンの対象となるランキングの順位は、TOASTERが定めるランキングの集計結果に基づいて決定します。
            不正な操作、複数アカウントの不正利用、その他キャンペーンの趣旨または本規約に反する行為が確認された場合、
            受賞資格を取り消し、賞金の支払いを行わない、または支払済みの賞金について返還を求める場合があります。
        </p>

        <h3>8-3. 賞金の支払方法</h3>
        <p>
            キャンペーンの賞金は、原則としてPayPayを利用して支払います。
            受賞者は、TOASTERが指定する方法により、賞金の受け取りに必要なPayPayアカウント等の情報を提供するものとします。
            PayPayの利用に必要なアカウント登録その他の条件については、受賞者自身がPayPayの定める利用規約その他の条件に従うものとします。
        </p>

        <p>
            受賞者が賞金の受け取りに必要な情報を指定された期限までに提供しない場合、
            またはPayPay側の利用条件その他の事情により賞金を支払うことができない場合、
            受賞資格を取り消すことがあります。
        </p>

        <h3>8-4. 受賞者への連絡</h3>
        <p>
            受賞者への連絡方法、賞金の受け取りに必要な手続および期限は、各キャンペーンで別途定める場合があります。
            受賞者が指定された期限までに必要な手続を行わない場合、受賞資格を失うことがあります。
        </p>

        <h3>8-5. キャンペーン内容の変更・中止</h3>
        <p>
            TOASTERは、必要と判断した場合、キャンペーンの内容、開催期間、対象ランキング、賞金その他の条件を変更し、
            またはキャンペーンを中止する場合があります。変更または中止を行う場合は、可能な限り本サービス上で告知します。
        </p>

        <h3>8-6. キャンペーンに関する禁止行為</h3>
        <p>
            キャンペーンへの参加に際して、以下の行為を禁止します。
        </p>
        <ul>
            <li>ランキングや反応数を不正に操作する行為</li>
            <li>キャンペーン参加を目的とした複数アカウントの不正利用</li>
            <li>自動化された手段その他不正な手段によって反応、投稿その他の数値を増加させる行為</li>
            <li>第三者に賞金の受け取りを不正に代行させる行為</li>
            <li>その他、キャンペーンの公平性を害するとTOASTERが判断する行為</li>
        </ul>

        <h3>8-7. その他</h3>
        <p>
            キャンペーンについて本規約と各キャンペーンの詳細ページの記載が異なる場合、
            当該キャンペーンについては各キャンペーンの詳細ページに記載された個別条件を優先する場合があります。
            キャンペーンへの参加者は、参加前に本規約および各キャンペーンの詳細を確認するものとします。
        </p>

        <h2>9. 免責事項</h2>

        <p>
            TOASTERは、本サービスについて、その完全性、正確性、継続性、
            特定目的への適合性等を保証するものではありません。
            ユーザー間で発生したトラブルについては、
            原則として当事者間で解決するものとします。
        </p>

        <h2>10. 規約の変更</h2>

        <p>
            TOASTERは、必要に応じて本規約を変更できます。
            変更後の規約は、本サービス上で公開した時点または
            別途定めた時点から適用されます。
        </p>

        <h2>11. お問い合わせ</h2>

        <p>
            本サービスに関するお問い合わせは、
            アプリ内の「運営とのチャット」からご連絡ください。
        </p>
        """
    )


@app.get(
    "/support",
    "/campaigns",
    response_class=HTMLResponse
)
def support_page():

    return public_page(
        "サポート",
        """
        <div class="date">
            TOASTER Support
        </div>

        <p>
            TOASTERをご利用いただきありがとうございます。
            アプリの利用中に問題が発生した場合は、以下をご確認ください。
        </p>

        <div class="card">

            <h2>
                ログインについて
            </h2>

            <p>
                AppleまたはGoogleのアカウントを利用してログインしてください。
                ログインできない場合は、通信環境をご確認のうえ再度お試しください。
            </p>

        </div>

        <div class="card">

            <h2>
                投稿について
            </h2>

            <p>
                不適切な投稿を発見した場合は、
                アプリ内の通報機能をご利用ください。
            </p>

        </div>

        <div class="card">

            <h2>
                ブロックについて
            </h2>

            <p>
                ユーザーをブロックすると、
                そのユーザーとの不要な接触を避けることができます。
                ブロックしたユーザーはブロックリストから確認・解除できます。
            </p>

        </div>

        <div class="card">

            <h2>
                アカウント削除について
            </h2>

            <p>
                アプリ内のプロフィール画面からアカウントを削除できます。
                削除すると、アカウントに関連する投稿、フォロー、いいね、
                コメント、アチーブメント等のデータが削除されます。
            </p>

        </div>

        <div class="card">

            <h2>
                お問い合わせ
            </h2>

            <p>
                問題が解決しない場合は、
                アプリ内の「運営とのチャット」からお問い合わせください。
            </p>

        </div>
        """
    )


@app.get("/")
def root():

    return {
        "message": "TOASTER API"
    }


@app.get("/db-test")
def db_test():

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT 1"
            )

            result = cur.fetchone()

    return {
        "database": result[0]
    }