from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from urllib.request import Request as URLRequest, urlopen

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("CARGOSYNC_DATA_DIR", str(ROOT / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "documents"
DB_PATH = DATA_DIR / "cargosync.sqlite3"
INDEX_PATH = ROOT / "index.html"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SESSION_COOKIE = "cargosync_session"
SESSION_DAYS = int(os.getenv("CARGOSYNC_SESSION_DAYS", "7"))
COOKIE_SECURE = os.getenv("CARGOSYNC_SECURE_COOKIES", "0").lower() in {"1", "true", "yes"}
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
ACCESSORIAL_DOC_TYPES = {"detention", "tonu", "layover", "pallet_exchange", "washout", "lumper"}
ALLOWED_DOCUMENTS = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
}
STATE_KEYS = {
    "cargosync_schedule_v2": "team",
    "cargosync_drivers_v1": "team",
    "reefer_dispatch_stage1_loads_v1": "user",
}
ROLES = {"admin", "dispatcher", "viewer"}
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

app = FastAPI(title="CargoSync Dispatch API", version="1.0.0", docs_url=None, redoc_url=None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))


class PostgresConnection:
    """Small compatibility wrapper so the tested SQLite SQL can run on PostgreSQL."""
    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def _sql(query: str) -> str:
        query = query.replace("username TEXT NOT NULL UNIQUE COLLATE NOCASE", "username TEXT NOT NULL UNIQUE")
        query = query.replace("username=? COLLATE NOCASE", "username=?")
        query = query.replace("name COLLATE NOCASE", "LOWER(name)")
        query = query.replace("?", "%s")
        return query

    def execute(self, query, params=()):
        return self.conn.execute(self._sql(query), params)

    def executescript(self, script: str):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()
        return False


def connect():
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row
        return PostgresConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10))
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level="DEFERRED")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 15000")
    return c


@contextmanager
def db():
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with db() as c:
        if not USE_POSTGRES:
            c.execute("PRAGMA journal_mode=WAL")
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','dispatcher','viewer')),
            active INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            created_by TEXT,
            password_changed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS login_attempts (
            ip TEXT NOT NULL,
            username TEXT NOT NULL,
            fail_count INTEGER NOT NULL,
            first_failed_at TEXT NOT NULL,
            locked_until TEXT,
            PRIMARY KEY(ip, username)
        );
        CREATE TABLE IF NOT EXISTS workspace_state (
            state_key TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            value_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            updated_by TEXT,
            PRIMARY KEY(state_key, user_id)
        );
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            load_id TEXT NOT NULL,
            doc_type TEXT NOT NULL CHECK(doc_type IN ('rate','pod')),
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            file_bytes BLOB,
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            UNIQUE(load_id, doc_type)
        );
        CREATE INDEX IF NOT EXISTS idx_documents_load ON documents(load_id);
        CREATE TABLE IF NOT EXISTS accessorial_documents (
            id TEXT PRIMARY KEY,
            load_id TEXT NOT NULL,
            doc_type TEXT NOT NULL CHECK(doc_type IN ('detention','tonu','layover','pallet_exchange','washout','lumper')),
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            file_bytes BLOB,
            uploaded_by TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            UNIQUE(load_id, doc_type)
        );
        CREATE INDEX IF NOT EXISTS idx_accessorial_documents_load ON accessorial_documents(load_id);
        CREATE TABLE IF NOT EXISTS finance_commissions (
            dispatcher_id TEXT PRIMARY KEY,
            commission_percent REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            user_id TEXT,
            username TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            detail_json TEXT
        );
        """
        if USE_POSTGRES:
            schema = schema.replace("username TEXT NOT NULL UNIQUE COLLATE NOCASE", "username TEXT NOT NULL UNIQUE")
            schema = schema.replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGSERIAL PRIMARY KEY")
            schema = schema.replace("file_bytes BLOB", "file_bytes BYTEA")
        c.executescript(schema)
        # Keep existing databases compatible: Pro Viewer is stored as a viewer
        # plus a capability flag so old SQLite CHECK constraints remain valid.
        if USE_POSTGRES:
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_viewer INTEGER NOT NULL DEFAULT 0")
        else:
            user_columns = {row["name"] for row in c.execute("PRAGMA table_info(users)").fetchall()}
            if "pro_viewer" not in user_columns:
                c.execute("ALTER TABLE users ADD COLUMN pro_viewer INTEGER NOT NULL DEFAULT 0")


init_db()


def audit(c: sqlite3.Connection, user: sqlite3.Row | dict | None, action: str,
          target_type: str | None = None, target_id: str | None = None,
          detail: dict | None = None) -> None:
    c.execute(
        "INSERT INTO audit_log(at,user_id,username,action,target_type,target_id,detail_json) VALUES(?,?,?,?,?,?,?)",
        (iso(), (user["id"] if user else None), (user["username"] if user else None), action,
         target_type, target_id, json.dumps(detail or {}, ensure_ascii=False)),
    )


def public_user(row: sqlite3.Row | dict) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "username": row["username"],
        "role": "pro_viewer" if bool(row["pro_viewer"]) else row["role"], "active": bool(row["active"]),
        "mustChangePassword": bool(row["must_change_password"]),
        "createdAt": row["created_at"], "createdBy": row["created_by"] or "",
    }


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_current_user(request: Request) -> sqlite3.Row:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in required")
    with db() as c:
        row = c.execute(
            "SELECT u.*,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (token_digest(token),),
        ).fetchone()
        if not row or not row["active"] or row["expires_at"] <= iso():
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))
            raise HTTPException(status_code=401, detail="Session expired; sign in again")
        if row["must_change_password"] and not (request.url.path == "/api/auth/password" and request.method == "POST"):
            raise HTTPException(status_code=403, detail={"code": "PASSWORD_CHANGE_REQUIRED", "message": "Change your temporary password before using CargoSync."})
        c.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (iso(), token_digest(token)))
        return row


def effective_role(user: sqlite3.Row | dict) -> str:
    """Return the public role while preserving compatibility with existing DB constraints."""
    return "pro_viewer" if bool(user["pro_viewer"]) else str(user["role"])


def admin_user(user: sqlite3.Row = Depends(get_current_user)) -> sqlite3.Row:
    if effective_role(user) != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def can_edit_load(user: sqlite3.Row, load: dict, drivers: list[dict]) -> bool:
    role = effective_role(user)
    if role in {"admin", "pro_viewer"}:
        return True
    if role != "dispatcher":
        return False
    # Never trust the dispatcherId supplied by the browser alone. The driver's
    # current roster assignment is authoritative, including after reassignment.
    name, truck = str(load.get("driver", "")), str(load.get("truck", ""))
    driver = next((d for d in drivers if d.get("name") == name and str(d.get("truck", "")) == truck), None)
    if not driver or driver.get("dispatcherId") != user["id"]:
        return False
    return not load.get("dispatcherId") or load.get("dispatcherId") == user["id"]


def read_state(c: sqlite3.Connection, key: str, user_id: str | None = None) -> tuple[Any, int]:
    scope = STATE_KEYS[key]
    owner = user_id if scope == "user" else ""
    row = c.execute("SELECT value_json,version FROM workspace_state WHERE state_key=? AND user_id=?", (key, owner or "")).fetchone()
    if row:
        try:
            return json.loads(row["value_json"]), int(row["version"])
        except json.JSONDecodeError:
            return [], int(row["version"])
    return [], 0


def safe_bootstrap(c: sqlite3.Connection, user: sqlite3.Row | None) -> dict:
    if not user:
        return {"authenticated": False, "currentUser": None, "users": [], "states": {}, "versions": {}}
    if bool(user["must_change_password"]):
        # Do not place workspace data in the page before a temporary password is replaced.
        return {"authenticated": True, "currentUser": public_user(user), "users": [public_user(user)],
                "states": {key: [] for key in STATE_KEYS}, "versions": {key: 0 for key in STATE_KEYS}}
    users = [public_user(row) for row in c.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE").fetchall()]
    states, versions = {}, {}
    for key, scope in STATE_KEYS.items():
        value, version = read_state(c, key, user["id"])
        states[key] = value
        versions[key] = version
    return {"authenticated": True, "currentUser": public_user(user), "users": users, "states": states, "versions": versions}


def encode_bootstrap(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("origin"):
        origin = request.headers["origin"]
        host = request.headers.get("host", "")
        # Same-origin API only; deployments behind a reverse proxy should preserve the public Host.
        if origin.split("://", 1)[-1].rstrip("/").lower() != host.lower():
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") or request.url.path == "/" else "no-cache"
    return response


@app.get("/health")
def health():
    return {"ok": True, "service": "cargosync", "time": iso()}


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def home(request: Request):
    if not INDEX_PATH.exists():
        raise HTTPException(status_code=500, detail="CargoSync index.html is missing")
    html = INDEX_PATH.read_text(encoding="utf-8")
    token = request.cookies.get(SESSION_COOKIE, "")
    user = None
    if token:
        with db() as c:
            row = c.execute("SELECT u.*,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_digest(token),)).fetchone()
            if row and row["active"] and row["expires_at"] > iso():
                user = row
    with db() as c:
        bootstrap = safe_bootstrap(c, user)
    boot_json = encode_bootstrap(bootstrap)
    inject = ("<script id=\"cargoServerBootstrap\">window.__CARGOSYNC_BOOTSTRAP__=" + boot_json + ";"
              + "(function(){var b=window.__CARGOSYNC_BOOTSTRAP__;var ks=['cargosync_schedule_v2','cargosync_drivers_v1','reefer_dispatch_stage1_loads_v1'];"
              + "try{if(b&&b.authenticated){localStorage.setItem('cargosync_local_users_v1',JSON.stringify(b.users||[]));ks.forEach(function(k){localStorage.setItem(k,JSON.stringify((b.states||{})[k]||[]))})}else{localStorage.removeItem('cargosync_local_users_v1');ks.forEach(function(k){localStorage.removeItem(k)})}}catch(e){} })();</script>")
    html = html.replace("<!-- CARGOSYNC_BOOTSTRAP -->", inject)
    return HTMLResponse(html)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["dispatcher", "viewer", "pro_viewer", "admin"] = "dispatcher"

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        value = value.strip().lower()
        import re
        if not re.fullmatch(r"[a-z0-9._]{3,32}", value):
            raise ValueError("Login must be 3–32 characters: letters, numbers, dot, underscore")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value


class PasswordBody(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class DriverBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    truck: str = Field(min_length=1, max_length=50)
    status: str = Field(default="Available", max_length=40)
    dispatcherId: str = Field(min_length=1, max_length=100)
    phone: str = Field(default="", max_length=40)
    trailerNumber: str = Field(default="", max_length=50)
    notes: str = Field(default="", max_length=2000)


class DriverPatch(BaseModel):
    dispatcherId: str | None = None
    status: str | None = Field(default=None, max_length=40)
    truck: str | None = Field(default=None, min_length=1, max_length=50)
    phone: str | None = Field(default=None, max_length=40)
    trailerNumber: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)


class StateBody(BaseModel):
    value: Any
    base_version: int | None = None


class CommissionBody(BaseModel):
    percent: float = Field(ge=0, le=100)


class ImportBody(BaseModel):
    format: str = "CargoSync-local-backup-v1"
    users: list[dict] = Field(default_factory=list)
    drivers: list[dict] = Field(default_factory=list)
    scheduleLoads: list[dict] = Field(default_factory=list)


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip().lower()
    ip = request.client.host if request.client else "unknown"
    now = utcnow()
    now_iso = iso(now)
    locked = False
    ok = False
    token = ""
    user = None
    with db() as c:
        attempt = c.execute(
            "SELECT fail_count,first_failed_at,locked_until FROM login_attempts WHERE ip=? AND username=?",
            (ip, username),
        ).fetchone()
        if attempt and attempt["locked_until"] and attempt["locked_until"] > now_iso:
            locked = True
        else:
            user = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
            if user and user["active"]:
                try:
                    ok = password_hasher.verify(user["password_hash"], body.password)
                except (VerifyMismatchError, VerificationError, ValueError):
                    ok = False
            if not ok:
                # Unknown usernames follow the same password-hash path to reduce timing differences.
                if not user:
                    password_hasher.hash(body.password[:256] or "invalid-placeholder-password")
                in_window = False
                if attempt:
                    try:
                        in_window = datetime.fromisoformat(attempt["first_failed_at"]) > now - timedelta(minutes=15)
                    except ValueError:
                        in_window = False
                fail_count = int(attempt["fail_count"]) + 1 if attempt and in_window else 1
                first_failed_at = attempt["first_failed_at"] if attempt and in_window else now_iso
                locked_until = iso(now + timedelta(minutes=15)) if fail_count >= 6 else None
                c.execute(
                    "INSERT INTO login_attempts(ip,username,fail_count,first_failed_at,locked_until) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(ip,username) DO UPDATE SET fail_count=excluded.fail_count,first_failed_at=excluded.first_failed_at,locked_until=excluded.locked_until",
                    (ip, username, fail_count, first_failed_at, locked_until),
                )
            else:
                c.execute("DELETE FROM login_attempts WHERE ip=? AND username=?", (ip, username))
                token = secrets.token_urlsafe(48)
                expires = now + timedelta(days=SESSION_DAYS)
                c.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?)",
                          (token_digest(token), user["id"], iso(expires), now_iso, now_iso))
                audit(c, user, "login", "user", user["id"], {"ip": ip})
    if locked:
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again in 15 minutes.")
    if not ok:
        raise HTTPException(status_code=401, detail="Login or password is incorrect")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE or request.url.scheme == "https",
                        samesite="lax", max_age=SESSION_DAYS * 86400, path="/")
    return {"ok": True, "user": public_user(user)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE, "")
    with db() as c:
        if token:
            user = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_digest(token),)).fetchone()
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))
            if user:
                audit(c, user, "logout", "user", user["id"])
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/auth/me")
def whoami(user: sqlite3.Row = Depends(get_current_user)):
    return {"user": public_user(user)}


@app.post("/api/auth/password")
def change_password(body: ChangePasswordBody, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    try:
        if not password_hasher.verify(user["password_hash"], body.current_password):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
    except (VerifyMismatchError, VerificationError, ValueError):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="Choose a different new password")
    with db() as c:
        c.execute("UPDATE users SET password_hash=?,must_change_password=0,password_changed_at=? WHERE id=?",
                  (password_hasher.hash(body.new_password), iso(), user["id"]))
        token = request.cookies.get(SESSION_COOKIE, "")
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], token_digest(token)))
        audit(c, user, "password_changed", "user", user["id"])
    return {"ok": True}



@app.get("/api/users")
def list_users(user: sqlite3.Row = Depends(admin_user)):
    with db() as c:
        return {"users": [public_user(row) for row in c.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE").fetchall()]}


@app.post("/api/users")
def create_user(body: UserCreate, user: sqlite3.Row = Depends(admin_user)):
    new_id = "user-" + uuid.uuid4().hex
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (body.username,)).fetchone():
            raise HTTPException(status_code=409, detail="That login already exists")
        now = iso()
        stored_role = "viewer" if body.role == "pro_viewer" else body.role
        pro_viewer = 1 if body.role == "pro_viewer" else 0
        c.execute("INSERT INTO users(id,name,username,password_hash,role,active,must_change_password,created_at,created_by,pro_viewer) VALUES(?,?,?,?,?,1,1,?,?,?)",
                  (new_id, body.name, body.username, password_hasher.hash(body.password), stored_role, now, user["username"], pro_viewer))
        audit(c, user, "user_created", "user", new_id, {"username": body.username, "role": body.role})
        row = c.execute("SELECT * FROM users WHERE id=?", (new_id,)).fetchone()
        return {"user": public_user(row)}


@app.post("/api/users/{user_id}/password")
def reset_password(user_id: str, body: PasswordBody, user: sqlite3.Row = Depends(admin_user)):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Use the Change password control to change your own password")
    with db() as c:
        target = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        c.execute("UPDATE users SET password_hash=?,must_change_password=1,password_changed_at=? WHERE id=?",
                  (password_hasher.hash(body.password), iso(), user_id))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        audit(c, user, "password_reset", "user", user_id)
    return {"ok": True, "mustChangePassword": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, user: sqlite3.Row = Depends(admin_user)):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    with db() as c:
        target = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "admin" and c.execute("SELECT COUNT(*) n FROM users WHERE role='admin' AND active=1").fetchone()["n"] <= 1:
            raise HTTPException(status_code=400, detail="The workspace must retain at least one active administrator")
        if target["role"] == "dispatcher":
            drivers, version = read_state(c, "cargosync_drivers_v1")
            drivers = [({**d, "dispatcherId": None} if d.get("dispatcherId") == user_id else d) for d in drivers]
            c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES(?,'',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                      ("cargosync_drivers_v1", json.dumps(drivers, ensure_ascii=False), max(version, 0) + 1, iso(), user["username"]))
            loads, lver = read_state(c, "cargosync_schedule_v2")
            for load in loads:
                if load.get("dispatcherId") == user_id:
                    load["dispatcherId"] = None
            c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES(?,'',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                      ("cargosync_schedule_v2", json.dumps(loads, ensure_ascii=False), max(lver, 0) + 1, iso(), user["username"]))
        audit(c, user, "user_deleted", "user", user_id, {"username": target["username"]})
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}



# Map services are proxied through CargoSync so browser CORS policies and tracking
# protection do not silently break geocoding or driving-route requests.
_MAP_CACHE: dict[str, tuple[float, Any]] = {}
_MAP_CACHE_TTL = 300.0


def _map_fetch_json(url: str, timeout: float = 8.0) -> Any:
    request = URLRequest(url, headers={
        "Accept": "application/json",
        "User-Agent": "CargoSyncDispatch/1.0 (route planner)",
    })
    with urlopen(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise ValueError(f"Map provider returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _map_cache_get(key: str) -> Any | None:
    entry = _MAP_CACHE.get(key)
    if not entry:
        return None
    expires, value = entry
    if time.monotonic() >= expires:
        _MAP_CACHE.pop(key, None)
        return None
    return value


def _map_cache_set(key: str, value: Any) -> None:
    _MAP_CACHE[key] = (time.monotonic() + _MAP_CACHE_TTL, value)


_MAP_ASSET_URLS = {
    "leaflet.js": "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js",
    "leaflet.css": "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css",
}
_MAP_IMAGE_ASSETS = {"layers.png", "layers-2x.png", "marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"}


def _load_map_asset(name: str) -> bytes:
    key = "map-asset:" + name
    cached = _map_cache_get(key)
    if cached is not None:
        return cached
    if name in _MAP_ASSET_URLS:
        url = _MAP_ASSET_URLS[name]
    elif name.startswith("image:") and name[6:] in _MAP_IMAGE_ASSETS:
        url = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/" + name[6:]
    else:
        raise HTTPException(status_code=404, detail="Map asset not found")
    try:
        request = URLRequest(url, headers={
            "Accept": "text/css, application/javascript, image/png, image/*;q=0.8",
            "User-Agent": "CargoSyncDispatch/1.0 (map assets)",
        })
        with urlopen(request, timeout=12.0) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"Map asset returned HTTP {response.status}")
            content = response.read(2_000_001)
            if not content or len(content) > 2_000_000:
                raise ValueError("Map asset size is invalid")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not load interactive map assets") from exc
    _map_cache_set(key, content)
    return content


@app.get("/api/maps/leaflet.js")
def map_leaflet_script(user: sqlite3.Row = Depends(get_current_user)):
    return Response(content=_load_map_asset("leaflet.js"), media_type="application/javascript",
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/api/maps/leaflet.css")
def map_leaflet_styles(user: sqlite3.Row = Depends(get_current_user)):
    return Response(content=_load_map_asset("leaflet.css"), media_type="text/css",
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@app.get("/api/maps/images/{asset_name}")
def map_leaflet_image(asset_name: str, user: sqlite3.Row = Depends(get_current_user)):
    media_type = "image/png"
    if asset_name == "marker-shadow.png":
        media_type = "image/png"
    return Response(content=_load_map_asset("image:" + asset_name), media_type=media_type,
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


def _normal_place(value: Any) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in str(value or "")).split())


def _photon_places(query: str, limit: int = 8) -> list[dict[str, Any]]:
    url = "https://photon.komoot.io/api/?q=" + quote(query) + f"&limit={limit}&lang=en"
    data = _map_fetch_json(url)
    features = data.get("features", []) if isinstance(data, dict) else []
    if not isinstance(features, list):
        return []
    us = [item for item in features if str(item.get("properties", {}).get("country", "")).lower() in {"united states", "usa"}]
    candidates = us or features
    result: list[dict[str, Any]] = []
    for feature in candidates:
        properties = feature.get("properties") or {}
        coordinates = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coordinates) < 2:
            continue
        try:
            lon, lat = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        city = properties.get("city") or properties.get("locality") or properties.get("name") or properties.get("county") or ""
        state = properties.get("state") or ""
        postcode = properties.get("postcode") or ""
        label = ", ".join(dict.fromkeys(str(v).strip() for v in (city, state, postcode) if str(v or "").strip()))
        if not label:
            label = str(properties.get("name") or query)
        if result and any(
            _normal_place(previous["label"]) == _normal_place(label)
            or (abs(previous["lat"] - lat) < 0.0001 and abs(previous["lon"] - lon) < 0.0001)
            for previous in result
        ):
            continue
        result.append({"label": label, "lat": lat, "lon": lon, "name": label})
    return result


def _nominatim_places(query: str, limit: int = 8) -> list[dict[str, Any]]:
    url = ("https://nominatim.openstreetmap.org/search?format=jsonv2&addressdetails=1"
           f"&limit={limit}&countrycodes=us&q={quote(query)}")
    data = _map_fetch_json(url)
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for item in data:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        address = item.get("address") or {}
        city = (address.get("city") or address.get("town") or address.get("village")
                or address.get("municipality") or address.get("hamlet")
                or address.get("county") or item.get("name") or "")
        state, postcode = address.get("state") or "", address.get("postcode") or ""
        label = ", ".join(dict.fromkeys(str(v).strip() for v in (city, state, postcode) if str(v or "").strip()))
        if not label:
            label = ", ".join(str(item.get("display_name") or query).split(",")[:3]).strip()
        result.append({"label": label, "lat": lat, "lon": lon, "name": item.get("display_name") or label})
    return result


def _resolve_place(query: str) -> dict[str, Any]:
    cache_key = "geo:" + _normal_place(query)
    cached = _map_cache_get(cache_key)
    if cached:
        return cached
    errors: list[str] = []
    for provider in (_photon_places, _nominatim_places):
        try:
            places = provider(query, 5)
            if places:
                nq = _normal_place(query)
                best = next((p for p in places if nq and nq in _normal_place(p["label"])), places[0])
                _map_cache_set(cache_key, best)
                return best
        except Exception as exc:
            errors.append(str(exc))
    raise HTTPException(status_code=502, detail="Could not find that location. Try a US city with state or a ZIP code.")


@app.get("/api/maps/suggest")
def map_suggestions(q: str, user: sqlite3.Row = Depends(get_current_user)):
    query = " ".join(str(q or "").split())[:160]
    if len(query) < 2:
        return {"results": []}
    cache_key = "suggest:" + _normal_place(query)
    cached = _map_cache_get(cache_key)
    if cached is not None:
        return {"results": cached}
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    for provider in (_photon_places, _nominatim_places):
        try:
            results = provider(query, 8)
            if results:
                break
        except Exception as exc:
            errors.append(str(exc))
    if results:
        _map_cache_set(cache_key, results)
    return {"results": results}


@app.get("/api/maps/geocode")
def map_geocode(q: str, user: sqlite3.Row = Depends(get_current_user)):
    query = " ".join(str(q or "").split())[:200]
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Enter a city, state or ZIP code")
    try:
        place = _resolve_place(query)
        return {"lat": place["lat"], "lon": place["lon"], "name": place.get("name") or place["label"], "label": place["label"]}
    except HTTPException:
        raise


@app.get("/api/maps/route")
def map_route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
    user: sqlite3.Row = Depends(get_current_user),
):
    values = (from_lat, from_lon, to_lat, to_lon)
    if not all(math.isfinite(value) for value in values):
        raise HTTPException(status_code=400, detail="Invalid map coordinates")
    if not (-90 <= from_lat <= 90 and -90 <= to_lat <= 90 and -180 <= from_lon <= 180 and -180 <= to_lon <= 180):
        raise HTTPException(status_code=400, detail="Map coordinates are out of range")
    cache_key = "route:" + ",".join(f"{value:.5f}" for value in values)
    cached = _map_cache_get(cache_key)
    if cached is not None:
        return cached
    coordinates = f"{from_lon},{from_lat};{to_lon},{to_lat}"
    endpoints = (
        "https://router.project-osrm.org/route/v1/driving/" + coordinates + "?overview=full&geometries=geojson&steps=false",
        "https://routing.openstreetmap.de/routed-car/route/v1/driving/" + coordinates + "?overview=full&geometries=geojson&steps=false",
    )
    for endpoint in endpoints:
        try:
            data = _map_fetch_json(endpoint, timeout=12.0)
            routes = data.get("routes") if isinstance(data, dict) else None
            if data.get("code") != "Ok" or not routes:
                continue
            route = routes[0]
            geometry = route.get("geometry") or {}
            if not geometry.get("coordinates"):
                continue
            distance, duration = float(route["distance"]), float(route["duration"])
            result = {"miles": distance / 1609.344, "minutes": duration / 60.0, "geometry": geometry}
            _map_cache_set(cache_key, result)
            return result
        except Exception:
            continue
    raise HTTPException(status_code=502, detail="Both driving-route services are unavailable. The map pins can still be shown; retry route calculation shortly.")


@app.get("/api/workspace/state")
def get_workspace_state(user: sqlite3.Row = Depends(get_current_user)):
    with db() as c:
        return safe_bootstrap(c, user)


@app.put("/api/workspace/state/{key}")
def put_workspace_state(key: str, body: StateBody, user: sqlite3.Row = Depends(get_current_user)):
    if key not in STATE_KEYS:
        raise HTTPException(status_code=400, detail="Unknown workspace key")
    if key == "cargosync_local_users_v1":
        raise HTTPException(status_code=403, detail="Accounts must be managed through the Admin Panel API")
    if not isinstance(body.value, list):
        raise HTTPException(status_code=400, detail="Workspace state must be a JSON array")
    if len(body.value) > 50000:
        raise HTTPException(status_code=413, detail="Too many records")
    if key == "cargosync_drivers_v1" and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can manage the driver roster")
    if key == "cargosync_schedule_v2" and effective_role(user) == "viewer":
        raise HTTPException(status_code=403, detail="Viewer accounts cannot edit Schedule")
    if key == "reefer_dispatch_stage1_loads_v1" and effective_role(user) == "viewer":
        raise HTTPException(status_code=403, detail="Viewer accounts cannot save Route Planner loads")

    with db() as c:
        scope = STATE_KEYS[key]
        owner = user["id"] if scope == "user" else ""
        current, version = read_state(c, key, user["id"])
        expected = body.base_version
        if expected is not None and expected != version:
            raise HTTPException(status_code=409, detail={"message": "Workspace state changed on another device", "current_version": version})
        if key == "cargosync_drivers_v1":
            seen_ids, seen_trucks, seen_names = set(), set(), set()
            users = c.execute("SELECT id,role FROM users").fetchall()
            dispatcher_ids = {u["id"] for u in users if u["role"] == "dispatcher"}
            for d in body.value:
                if not isinstance(d, dict) or not str(d.get("name", "")).strip() or not str(d.get("truck", "")).strip():
                    raise HTTPException(status_code=400, detail="Every driver needs a name and truck number")
                did = str(d.get("id") or uuid.uuid4().hex)
                d["id"] = did
                truck = str(d.get("truck", "")).strip().lower()
                name = str(d.get("name", "")).strip().lower()
                owner_id = d.get("dispatcherId")
                if owner_id and owner_id not in dispatcher_ids:
                    raise HTTPException(status_code=400, detail="Driver is assigned to an invalid dispatcher")
                if did in seen_ids or truck in seen_trucks or name in seen_names:
                    raise HTTPException(status_code=409, detail="Duplicate driver ID, truck number, or driver name")
                seen_ids.add(did); seen_trucks.add(truck); seen_names.add(name)
        if key == "cargosync_schedule_v2":
            drivers, _ = read_state(c, "cargosync_drivers_v1")
            old_by_id = {str(l.get("id")): l for l in current if isinstance(l, dict)}
            new_by_id = {str(l.get("id")): l for l in body.value if isinstance(l, dict)}
            if len(new_by_id) != len(body.value) or any(not l.get("id") for l in body.value if isinstance(l, dict)):
                raise HTTPException(status_code=400, detail="Each load must have a unique ID")
            if effective_role(user) == "pro_viewer" and set(old_by_id) - set(new_by_id):
                raise HTTPException(status_code=403, detail="Pro Viewer accounts can add and edit loads but cannot delete them")
            if user["role"] != "admin":
                # Dispatcher changes are limited to loads assigned to their own drivers; other loads must be preserved unchanged.
                for old_id, old in old_by_id.items():
                    if can_edit_load(user, old, drivers):
                        continue
                    if old_id not in new_by_id or new_by_id[old_id] != old:
                        raise HTTPException(status_code=403, detail="You cannot modify or delete a load assigned to another dispatcher")
                for new_id, new in new_by_id.items():
                    old = old_by_id.get(new_id)
                    if old is None or new != old:
                        if not can_edit_load(user, new, drivers):
                            raise HTTPException(status_code=403, detail="You can only create or edit loads for your assigned drivers")
            for l in body.value:
                if not isinstance(l, dict):
                    raise HTTPException(status_code=400, detail="Invalid load record")
                if len(str(l.get("number", ""))) > 80 or len(str(l.get("pu", ""))) > 300 or len(str(l.get("del", ""))) > 300:
                    raise HTTPException(status_code=400, detail="A load field is too long")
                if l.get("puDate") and l.get("delDate") and str(l["delDate"]) < str(l["puDate"]):
                    raise HTTPException(status_code=400, detail="Delivery cannot be before pickup")
        next_version = version + 1
        now = iso()
        serialized = json.dumps(body.value, ensure_ascii=False, separators=(",", ":"))
        c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES(?,?,?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=excluded.version,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                  (key, owner or "", serialized, next_version, now, user["username"]))
        audit(c, user, "workspace_state_updated", "state", key, {"version": next_version, "items": len(body.value)})
        # Remove documents for loads deliberately removed by an authorized editor.
        if key == "cargosync_schedule_v2":
            for removed_id in set(old_by_id) - set(new_by_id):
                if user["role"] == "admin" or can_edit_load(user, old_by_id[removed_id], drivers):
                    docs = c.execute("SELECT stored_name FROM documents WHERE load_id=?", (removed_id,)).fetchall()
                    for doc in docs:
                        try: (UPLOAD_DIR / doc["stored_name"]).unlink(missing_ok=True)
                        except OSError: pass
                    c.execute("DELETE FROM documents WHERE load_id=?", (removed_id,))
        return {"ok": True, "key": key, "version": next_version, "updatedAt": now}


@app.get("/api/finance/summary")
def finance_summary(user: sqlite3.Row = Depends(get_current_user)):
    role = effective_role(user)
    if role not in {"admin", "dispatcher"}:
        raise HTTPException(status_code=403, detail="Finance data is available only to administrators and dispatchers")

    def money_num(value: Any) -> float:
        try:
            number = float(value or 0)
            return number if math.isfinite(number) and number >= 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def summarize(load_list: list[dict], percent: float = 0.0) -> dict[str, Any]:
        total_rate = tonu_total = detention_total = layover_total = 0.0
        rpm_rate = rpm_miles = 0.0
        missing_miles = load_count = 0
        for load in load_list:
            if not isinstance(load, dict):
                continue
            load_count += 1
            rate = money_num(load.get("rate"))
            total_rate += rate
            amounts = load.get("accessorialAmounts") if isinstance(load.get("accessorialAmounts"), dict) else {}
            tonu = money_num(amounts.get("tonu", load.get("tonuAmount", 0)))
            detention = money_num(amounts.get("detention", load.get("detentionAmount", 0)))
            layover = money_num(amounts.get("layover", load.get("layoverAmount", 0)))
            tonu_total += tonu
            detention_total += detention
            layover_total += layover

            accessorials = {str(value).strip().lower() for value in (load.get("accessorials") or [])}
            is_tonu = "tonu" in accessorials or bool(load.get("tonu"))
            if not is_tonu:
                miles = money_num(load.get("loadedMiles") or load.get("routeMiles") or load.get("miles"))
                if miles > 0:
                    rpm_rate += rate
                    rpm_miles += miles
                elif rate > 0:
                    missing_miles += 1

        accessorial_total = tonu_total + detention_total + layover_total
        gross = total_rate + accessorial_total
        commission = min(100.0, max(0.0, float(percent or 0)))
        return {
            "loadCount": load_count,
            "freightRevenue": round(total_rate, 2),
            "tonuRevenue": round(tonu_total, 2),
            "detentionRevenue": round(detention_total, 2),
            "layoverRevenue": round(layover_total, 2),
            "accessorialRevenue": round(accessorial_total, 2),
            "totalRevenue": round(gross, 2),
            "commissionPercent": commission,
            "earnings": round(gross * commission / 100.0, 2),
            "rpmRevenue": round(rpm_rate, 2),
            "rpmMiles": round(rpm_miles, 1),
            "rpm": round(rpm_rate / rpm_miles, 2) if rpm_miles > 0 else None,
            "loadsWithoutMiles": missing_miles,
        }

    with db() as c:
        loads, _ = read_state(c, "cargosync_schedule_v2")
        drivers, _ = read_state(c, "cargosync_drivers_v1")
        commissions = {
            row["dispatcher_id"]: float(row["commission_percent"] or 0)
            for row in c.execute("SELECT dispatcher_id, commission_percent FROM finance_commissions").fetchall()
        }
        if role == "dispatcher":
            assignments = {
                (str(driver.get("name", "")), str(driver.get("truck", ""))): driver.get("dispatcherId")
                for driver in drivers if isinstance(driver, dict)
            }
            own_loads = []
            for load in loads:
                if not isinstance(load, dict):
                    continue
                owner_id = assignments.get((str(load.get("driver", "")), str(load.get("truck", "")))) or load.get("dispatcherId")
                if owner_id == user["id"]:
                    own_loads.append(load)
            own_commission = commissions.get(user["id"], 0.0)
            return {
                "scope": "dispatcher",
                "dispatcher": {
                    "id": user["id"],
                    "name": user["name"],
                    **summarize(own_loads, own_commission),
                },
            }

        users = c.execute(
            "SELECT id, name, username FROM users WHERE role='dispatcher' AND active=1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
        assignment_map = {
            (str(driver.get("name", "")), str(driver.get("truck", ""))): driver.get("dispatcherId")
            for driver in drivers if isinstance(driver, dict)
        }
        by_dispatcher = {row["id"]: [] for row in users}
        for load in loads:
            if not isinstance(load, dict):
                continue
            dispatcher_id = assignment_map.get((str(load.get("driver", "")), str(load.get("truck", "")))) or load.get("dispatcherId")
            if dispatcher_id in by_dispatcher:
                by_dispatcher[dispatcher_id].append(load)
        dispatchers = []
        for row in users:
            percent = commissions.get(row["id"], 0.0)
            dispatchers.append({
                "id": row["id"],
                "name": row["name"],
                "username": row["username"],
                **summarize(by_dispatcher[row["id"]], percent),
            })
        company = summarize([load for load in loads if isinstance(load, dict)], 0.0)
        company["commissionPayouts"] = round(sum(item["earnings"] for item in dispatchers), 2)
        company["unassignedLoads"] = sum(
            1 for load in loads if isinstance(load, dict)
            and (assignment_map.get((str(load.get("driver", "")), str(load.get("truck", "")))) or load.get("dispatcherId")) not in by_dispatcher
        )
        return {"scope": "company", "company": company, "dispatchers": dispatchers}


@app.put("/api/finance/commissions/{dispatcher_id}")
def update_dispatcher_commission(dispatcher_id: str, body: CommissionBody, user: sqlite3.Row = Depends(admin_user)):
    with db() as c:
        target = c.execute(
            "SELECT id FROM users WHERE id=? AND role='dispatcher' AND active=1", (dispatcher_id,)
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Active dispatcher account not found")
        now = iso()
        c.execute(
            "INSERT INTO finance_commissions(dispatcher_id,commission_percent,updated_at,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(dispatcher_id) DO UPDATE SET commission_percent=excluded.commission_percent,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (dispatcher_id, float(body.percent), now, user["username"]),
        )
        audit(c, user, "finance_commission_updated", "dispatcher", dispatcher_id, {"percent": float(body.percent)})
    return {"ok": True, "dispatcherId": dispatcher_id, "percent": float(body.percent), "updatedAt": now}


@app.get("/api/drivers")
def api_drivers(user: sqlite3.Row = Depends(get_current_user)):
    with db() as c:
        drivers, _ = read_state(c, "cargosync_drivers_v1")
        return {"drivers": drivers}


@app.post("/api/drivers")
def create_driver(body: DriverBody, user: sqlite3.Row = Depends(admin_user)):
    with db() as c:
        drivers, ver = read_state(c, "cargosync_drivers_v1")
        users = c.execute("SELECT id,role FROM users").fetchall()
        if not any(u["id"] == body.dispatcherId and u["role"] == "dispatcher" for u in users):
            raise HTTPException(status_code=400, detail="Choose a valid dispatcher account")
        if any(str(d.get("truck", "")).lower() == body.truck.strip().lower() for d in drivers):
            raise HTTPException(status_code=409, detail="That truck number already exists")
        if any(str(d.get("name", "")).lower() == body.name.strip().lower() for d in drivers):
            raise HTTPException(status_code=409, detail="A driver with that name already exists")
        driver = {"id": "driver-" + uuid.uuid4().hex, "name": body.name.strip(), "truck": body.truck.strip(), "status": body.status.strip() or "Available", "dispatcherId": body.dispatcherId, "phone": body.phone.strip(), "trailerNumber": body.trailerNumber.strip(), "notes": body.notes.strip(), "createdAt": iso()}
        drivers.append(driver)
        now = iso()
        c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES('cargosync_drivers_v1','',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                  (json.dumps(drivers, ensure_ascii=False), ver + 1, now, user["username"]))
        audit(c, user, "driver_created", "driver", driver["id"], {"truck": driver["truck"], "dispatcherId": driver["dispatcherId"]})
        return {"driver": driver, "version": ver + 1}


@app.patch("/api/drivers/{driver_id}")
def patch_driver(driver_id: str, body: DriverPatch, user: sqlite3.Row = Depends(admin_user)):
    with db() as c:
        drivers, ver = read_state(c, "cargosync_drivers_v1")
        target = next((d for d in drivers if d.get("id") == driver_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Driver not found")
        old_name = str(target.get("name", ""))
        old_truck = str(target.get("truck", ""))
        assigned_changed = "dispatcherId" in body.model_fields_set
        truck_changed = body.truck is not None and body.truck.strip() != old_truck
        if assigned_changed:
            if body.dispatcherId and not c.execute("SELECT 1 FROM users WHERE id=? AND role='dispatcher' AND active=1", (body.dispatcherId,)).fetchone():
                raise HTTPException(status_code=400, detail="Choose a valid active dispatcher account")
            target["dispatcherId"] = body.dispatcherId or None
        if body.truck is not None:
            new_truck = body.truck.strip()
            if not new_truck:
                raise HTTPException(status_code=400, detail="Truck number cannot be empty")
            if any(d.get("id") != driver_id and str(d.get("truck", "")).strip().lower() == new_truck.lower() for d in drivers):
                raise HTTPException(status_code=409, detail="That truck number already exists")
            target["truck"] = new_truck
        if body.phone is not None:
            target["phone"] = body.phone.strip()
        if body.trailerNumber is not None:
            target["trailerNumber"] = body.trailerNumber.strip()
        if body.notes is not None:
            target["notes"] = body.notes.strip()
        if body.status is not None:
            target["status"] = body.status.strip() or "Available"
        now = iso()
        c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES('cargosync_drivers_v1','',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                  (json.dumps(drivers, ensure_ascii=False), ver + 1, now, user["username"]))
        if assigned_changed or truck_changed:
            loads, lver = read_state(c, "cargosync_schedule_v2")
            changed_loads = False
            for load in loads:
                if str(load.get("driver", "")) == old_name and str(load.get("truck", "")) == old_truck:
                    if truck_changed and load.get("truck") != target.get("truck"):
                        load["truck"] = target.get("truck")
                        changed_loads = True
                    if assigned_changed and load.get("dispatcherId") != target.get("dispatcherId"):
                        load["dispatcherId"] = target.get("dispatcherId")
                        changed_loads = True
                    if changed_loads:
                        load["updatedBy"] = user["username"]
                        load["updatedAt"] = now
            if changed_loads:
                c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES('cargosync_schedule_v2','',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                          (json.dumps(loads, ensure_ascii=False), lver + 1, now, user["username"]))
        audit(c, user, "driver_updated", "driver", driver_id, {"dispatcherId": target.get("dispatcherId"), "assignmentChanged": assigned_changed, "truckChanged": truck_changed, "phoneUpdated": body.phone is not None, "trailerNumberUpdated": body.trailerNumber is not None, "notesUpdated": body.notes is not None})
        return {"driver": target, "version": ver + 1}


@app.delete("/api/drivers/{driver_id}")
def delete_driver(driver_id: str, user: sqlite3.Row = Depends(admin_user)):
    with db() as c:
        drivers, ver = read_state(c, "cargosync_drivers_v1")
        target = next((d for d in drivers if d.get("id") == driver_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Driver not found")
        loads, _ = read_state(c, "cargosync_schedule_v2")
        if any(l.get("driver") == target.get("name") and str(l.get("truck", "")) == str(target.get("truck", "")) for l in loads):
            raise HTTPException(status_code=409, detail="This driver has scheduled loads. Reassign or remove those loads first.")
        drivers = [d for d in drivers if d.get("id") != driver_id]
        c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES('cargosync_drivers_v1','',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                  (json.dumps(drivers, ensure_ascii=False), ver + 1, iso(), user["username"]))
        audit(c, user, "driver_deleted", "driver", driver_id)
    return {"ok": True}


def find_load_and_authorize(c: sqlite3.Connection, user: sqlite3.Row, load_id: str, require_edit: bool = False) -> dict:
    loads, _ = read_state(c, "cargosync_schedule_v2")
    load = next((l for l in loads if str(l.get("id")) == load_id), None)
    if not load:
        raise HTTPException(status_code=404, detail="Load not found")
    drivers, _ = read_state(c, "cargosync_drivers_v1")
    if require_edit and not can_edit_load(user, load, drivers):
        raise HTTPException(status_code=403, detail="You do not have permission to edit this load")
    return load


def detect_document(ext: str, content: bytes, declared_type: str) -> str:
    if ext not in ALLOWED_DOCUMENTS:
        raise HTTPException(status_code=415, detail="Only PDF, PNG, JPG and WEBP documents are allowed")
    detected = None
    if content.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        detected = "image/webp"
    if not detected or detected not in ALLOWED_DOCUMENTS[ext]:
        raise HTTPException(status_code=415, detail="File type does not match its extension or is not a valid PDF/image")
    return detected


@app.put("/api/loads/{load_id}/documents/{doc_type}")
async def upload_document(load_id: str, doc_type: Literal["rate", "pod", "detention", "tonu", "layover", "pallet_exchange", "washout", "lumper"], request: Request,
                          file: UploadFile = File(...), user: sqlite3.Row = Depends(get_current_user)):
    # Rate confirmation and POD remain in the original table for compatibility.
    # Accessorial receipts use their own table so existing database constraints stay untouched.
    table = "accessorial_documents" if doc_type in ACCESSORIAL_DOC_TYPES else "documents"
    with db() as c:
        find_load_and_authorize(c, user, load_id, require_edit=True)
        ext = Path(file.filename or "document").suffix.lower()
        content = await file.read(MAX_DOCUMENT_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(content) > MAX_DOCUMENT_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 15 MB limit")
        content_type = detect_document(ext, content, file.content_type or "")
        stored = uuid.uuid4().hex + ext
        c.execute(f"INSERT INTO {table}(id,load_id,doc_type,original_name,stored_name,content_type,size_bytes,file_bytes,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(load_id,doc_type) DO UPDATE SET id=excluded.id,original_name=excluded.original_name,stored_name=excluded.stored_name,content_type=excluded.content_type,size_bytes=excluded.size_bytes,file_bytes=excluded.file_bytes,uploaded_by=excluded.uploaded_by,uploaded_at=excluded.uploaded_at",
                  (uuid.uuid4().hex, load_id, doc_type, Path(file.filename or f"{doc_type}{ext}").name[:255], stored, content_type, len(content), content, user["username"], iso()))
        audit(c, user, "document_uploaded", "load", load_id, {"type": doc_type, "size": len(content)})
    return {"ok": True, "type": doc_type, "name": Path(file.filename or "document").name, "size": len(content)}


@app.get("/api/loads/{load_id}/documents/{doc_type}")
def download_document(load_id: str, doc_type: Literal["rate", "pod", "detention", "tonu", "layover", "pallet_exchange", "washout", "lumper"], user: sqlite3.Row = Depends(get_current_user)):
    table = "accessorial_documents" if doc_type in ACCESSORIAL_DOC_TYPES else "documents"
    with db() as c:
        find_load_and_authorize(c, user, load_id)
        row = c.execute(f"SELECT * FROM {table} WHERE load_id=? AND doc_type=?", (load_id, doc_type)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        filename = row["original_name"]
        content = row["file_bytes"] if "file_bytes" in row.keys() else None
        # Compatibility with pre-DB document records from an older local deployment.
        if content is None:
            path = UPLOAD_DIR / row["stored_name"]
            if not path.exists():
                raise HTTPException(status_code=404, detail="Document file is missing")
            content = path.read_bytes()
        disposition = "inline; filename*=UTF-8''" + quote(filename)
        return Response(content=content, media_type=row["content_type"], headers={"Cache-Control": "private, no-store", "Content-Disposition": disposition, "X-File-Name": quote(filename)})


@app.delete("/api/loads/{load_id}/documents/{doc_type}")
def delete_document(load_id: str, doc_type: Literal["rate", "pod", "detention", "tonu", "layover", "pallet_exchange", "washout", "lumper"], user: sqlite3.Row = Depends(get_current_user)):
    table = "accessorial_documents" if doc_type in ACCESSORIAL_DOC_TYPES else "documents"
    with db() as c:
        find_load_and_authorize(c, user, load_id, require_edit=True)
        row = c.execute(f"SELECT * FROM {table} WHERE load_id=? AND doc_type=?", (load_id, doc_type)).fetchone()
        if row:
            try: (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
            except OSError: pass
            c.execute(f"DELETE FROM {table} WHERE load_id=? AND doc_type=?", (load_id, doc_type))
            audit(c, user, "document_deleted", "load", load_id, {"type": doc_type})
    return {"ok": True}


@app.post("/api/admin/import")
def import_legacy(body: ImportBody, user: sqlite3.Row = Depends(admin_user)):
    if body.format != "CargoSync-local-backup-v1":
        raise HTTPException(status_code=400, detail="Unsupported backup format")
    if len(body.drivers) > 5000 or len(body.scheduleLoads) > 50000:
        raise HTTPException(status_code=413, detail="Backup is too large")
    with db() as c:
        existing_users = [dict(r) for r in c.execute("SELECT id,name,username,role FROM users").fetchall()]
        by_username = {u["username"].lower(): u for u in existing_users}
        old_user_to_new = {}
        for old in body.users:
            if old.get("username") and old["username"].lower() in by_username:
                old_user_to_new[str(old.get("id"))] = by_username[old["username"].lower()]["id"]
        drivers = []
        trucks, names = set(), set()
        for d in body.drivers:
            truck, name = str(d.get("truck", "")).strip(), str(d.get("name", "")).strip()
            if not name or not truck: continue
            owner = old_user_to_new.get(str(d.get("dispatcherId") or ""))
            if owner and not any(u["id"] == owner and u["role"] == "dispatcher" for u in existing_users): owner = None
            if truck.lower() in trucks or name.lower() in names: continue
            trucks.add(truck.lower()); names.add(name.lower())
            drivers.append({**d, "id": "driver-" + uuid.uuid4().hex, "name": name, "truck": truck, "dispatcherId": owner})
        name_to_driver = {(d["name"], str(d["truck"])): d for d in drivers}
        loads = []
        for l in body.scheduleLoads:
            if not isinstance(l, dict) or not l.get("id"): continue
            new = dict(l)
            match = name_to_driver.get((str(l.get("driver", "")), str(l.get("truck", ""))))
            new["dispatcherId"] = match["dispatcherId"] if match else None
            new["createdBy"] = new.get("createdBy") or user["username"]
            new["updatedBy"] = user["username"]
            loads.append(new)
        now = iso()
        for key, value in (("cargosync_drivers_v1", drivers), ("cargosync_schedule_v2", loads)):
            _, oldver = read_state(c, key)
            c.execute("INSERT INTO workspace_state(state_key,user_id,value_json,version,updated_at,updated_by) VALUES(?,'',?,?,?,?) ON CONFLICT(state_key,user_id) DO UPDATE SET value_json=excluded.value_json,version=workspace_state.version+1,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                      (key, json.dumps(value, ensure_ascii=False), oldver + 1, now, user["username"]))
        audit(c, user, "legacy_import", "workspace", "team", {"drivers": len(drivers), "loads": len(loads)})
    return {"ok": True, "driversImported": len(drivers), "loadsImported": len(loads)}


@app.get("/api/admin/audit")
def get_audit(limit: int = 100, user: sqlite3.Row = Depends(admin_user)):
    limit = max(1, min(limit, 500))
    with db() as c:
        rows = c.execute("SELECT at,username,action,target_type,target_id,detail_json FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return {"events": [{**dict(r), "detail": json.loads(r["detail_json"] or "{}")} for r in rows]}


def create_admin_user(name: str, username: str, password: str) -> str:
    username = username.strip().lower()
    if len(password) < 12:
        raise ValueError("Admin password must be at least 12 characters")
    import re
    if not re.fullmatch(r"[a-z0-9._]{3,32}", username):
        raise ValueError("Username must be 3–32 letters, numbers, dots or underscores")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
            raise ValueError("That username already exists")
        user_id = "user-" + uuid.uuid4().hex
        c.execute("INSERT INTO users(id,name,username,password_hash,role,active,must_change_password,created_at,created_by,password_changed_at) VALUES(?,?,?,?, 'admin',1,0,?,'setup',?)",
                  (user_id, name.strip() or "Workspace Admin", username, password_hasher.hash(password), iso(), iso()))
        return user_id


def bootstrap_admin_from_env() -> None:
    """Create the first admin from secret environment variables, once only."""
    name = os.getenv("CARGOSYNC_BOOTSTRAP_ADMIN_NAME", "Workspace Admin").strip() or "Workspace Admin"
    username = os.getenv("CARGOSYNC_BOOTSTRAP_ADMIN_USERNAME", "").strip().lower()
    password = os.getenv("CARGOSYNC_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        return
    if len(password) < 16:
        raise RuntimeError("CARGOSYNC_BOOTSTRAP_ADMIN_PASSWORD must be at least 16 characters")
    import re
    if not re.fullmatch(r"[a-z0-9._]{3,32}", username):
        raise RuntimeError("CARGOSYNC_BOOTSTRAP_ADMIN_USERNAME must be 3–32 letters, numbers, dots or underscores")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
            return
        user_id = "user-" + uuid.uuid4().hex
        now = iso()
        c.execute("INSERT INTO users(id,name,username,password_hash,role,active,must_change_password,created_at,created_by,password_changed_at) VALUES(?,?,?,?, 'admin',1,1,?,'bootstrap',?)",
                  (user_id, name, username, password_hasher.hash(password), now, now))
        audit(c, None, "bootstrap_admin_created", "user", user_id, {"username": username})


bootstrap_admin_from_env()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)