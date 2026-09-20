# TOASTER Backend — Render deployment

This directory is the deployable FastAPI backend.

## Render

- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/`

Set the environment variables listed in `render.yaml` in Render.
Do not commit `.env` or secret values.

After deployment, verify:

- `/`
- `/privacy`
- `/terms`
- `/support`
- `/db-test`

`/db-test` requires `DATABASE_URL` and is useful for confirming PostgreSQL connectivity.

## Authentication environment variables

The API now issues TOASTER access JWTs after Google/Apple login and requires a
Bearer token for protected API requests.

Set these Render environment variables on the web service:

- `TOASTER_JWT_SECRET`: long random secret; keep this private and stable.
- `TOASTER_JWT_EXPIRES_DAYS`: optional, defaults to `30`.
