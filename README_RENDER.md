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
