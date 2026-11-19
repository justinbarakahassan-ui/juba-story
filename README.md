# Juba Story

Juba Story is a Flask web app for recording and sharing short voice stories. It includes a SQLite data layer, live updates through Server-Sent Events, and installable PWA metadata.

## Run locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. The health endpoint is `GET /healthz`.

## Production deployment

Use a Linux host with persistent storage and run the `Procfile` command. Set `JUBA_STORY_DB` to a persistent path and set `FOUNDER_PASSWORD_HASH` as a secret before the first start. The included free Render blueprint is suitable for a temporary public demo, but its local SQLite data is ephemeral. Use a paid Render disk or external database before treating it as production data. SQLite is suitable for a small single-instance deployment; for multiple app instances, move the database to a shared database and replace the in-process SSE subscriber set with a shared broker.

Put TLS in front of Gunicorn, restrict database/file permissions, configure backups, and set upload limits at the reverse proxy as well as in Flask.

## Verification

The project should pass Python compilation, Flask health checks, API validation checks, and a browser smoke test covering sign-up, recording, publishing, liking, commenting, live refresh, and PWA installation. Store credentials and deployment provider settings outside the repository.
