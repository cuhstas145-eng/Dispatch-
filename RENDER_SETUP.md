# CargoSync — Render deployment notes

The `cargosync-fullstack-deploy` branch is used for the server-backed CargoSync application. The existing GitHub Pages `main` site is kept separate until the Render app is healthy.

## Render resources

- Web service: `cargosync-dispatch` (Python / FastAPI).
- PostgreSQL: existing Render database `cargosync-db` (Frankfurt).
- The app stores shared drivers, Schedule state, user records, sessions, audit events, and Rate Confirmation/POD content in PostgreSQL. User passwords are Argon2 hashes.
- The initial admin is provisioned via secret Render environment variables, not committed into Git.

Required environment variables on the web service:

- `DATABASE_URL`: Render PostgreSQL private connection string (Blueprint reference in `render.yaml`).
- `CARGOSYNC_SECURE_COOKIES=1`
- `CARGOSYNC_DATA_DIR=/tmp/cargosync` (only for transient local compatibility; persistent state is in PostgreSQL).
- `CARGOSYNC_BOOTSTRAP_ADMIN_NAME`
- `CARGOSYNC_BOOTSTRAP_ADMIN_USERNAME`
- `CARGOSYNC_BOOTSTRAP_ADMIN_PASSWORD` (at least 16 characters; keep secret). When created, the first admin must change this temporary password.

## Telegram bot

Set `TELEGRAM_BOT_TOKEN` and `CARGOSYNC_WEBAPP_URL=https://<your-render-service>.onrender.com/?view=schedule` in the environment of the process that runs `bot.py`. The top keyboard gains a `🗓 CargoSync Schedule` Web App button. This is separate from the web service; do not store the Telegram token in GitHub.

## Free-plan warning

Free Render web services can sleep and have ephemeral filesystems. The free PostgreSQL database created for initial testing expires on **2026-11-18**. Upgrade/replace it before that date to retain data for continued use. Do not treat this setup as permanent production storage until billing, backups, and ownership are intentionally confirmed.