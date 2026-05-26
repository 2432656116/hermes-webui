"""
Hermes WebUI — SQLite-backed session store.

Drop-in replacement for the JSON file-backed session persistence.
Enabled via HERMES_WEBUI_SQLITE=1.

Benefits over JSON files:
- Atomic writes (no truncation on crash)
- Indexed lookups (O(log N) vs O(N) for session list)
- Full-text search (FTS5) for session content
- Better for 100+ sessions

When disabled, falls back to existing JSON file persistence with zero overhead.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
SQLITE_ENABLED = os.environ.get("HERMES_WEBUI_SQLITE", "").strip() in ("1", "true", "yes")
SQLITE_DB_PATH = os.environ.get(
    "HERMES_WEBUI_SQLITE_DB",
    os.path.join(os.path.expanduser("~"), ".hermes-webui", "sessions.db"),
)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    title          TEXT DEFAULT 'Untitled',
    model          TEXT DEFAULT '',
    model_provider TEXT DEFAULT '',
    workspace      TEXT DEFAULT '',
    messages_json  TEXT DEFAULT '[]',
    message_count  INTEGER DEFAULT 0,
    pinned         INTEGER DEFAULT 0,
    archived       INTEGER DEFAULT 0,
    project_id     TEXT DEFAULT '',
    profile        TEXT DEFAULT 'default',
    created_at     REAL DEFAULT 0,
    updated_at     REAL DEFAULT 0,
    last_message_at REAL DEFAULT 0,
    source         TEXT DEFAULT 'webui',
    is_streaming   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id,
    title,
    messages_text,
    content='sessions',
    content_rowid='rowid'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, session_id, title, messages_text)
    VALUES (new.rowid, new.session_id, new.title,
            (SELECT group_concat(json_extract(value, '$.content'), ' ')
             FROM json_each(new.messages_json)
             WHERE json_extract(value, '$.content') IS NOT NULL));
END;

CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, session_id, title, messages_text)
    VALUES ('delete', old.rowid, old.session_id, old.title, '');
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, session_id, title, messages_text)
    VALUES ('delete', old.rowid, old.session_id, old.title, '');
    INSERT INTO sessions_fts(rowid, session_id, title, messages_text)
    VALUES (new.rowid, new.session_id, new.title,
            (SELECT group_concat(json_extract(value, '$.content'), ' ')
             FROM json_each(new.messages_json)
             WHERE json_extract(value, '$.content') IS NOT NULL));
END;

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
"""


def _get_conn() -> sqlite3.Connection:
    """Get or create the SQLite connection (thread-safe)."""
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        db_dir = os.path.dirname(SQLITE_DB_PATH)
        os.makedirs(db_dir, exist_ok=True)
        _conn = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
        logger.info("SQLite session store opened at %s", SQLITE_DB_PATH)
        return _conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    if d.get("messages_json"):
        try:
            d["messages"] = json.loads(d.pop("messages_json"))
        except json.JSONDecodeError:
            d["messages"] = []
    else:
        d.pop("messages_json", None)
        d["messages"] = []
    return d


# ── Public API ─────────────────────────────────────────────────────────────

def load_session(session_id: str) -> dict | None:
    """Load a session by ID. Returns None if not found."""
    if not SQLITE_ENABLED:
        return None  # Caller should fall back to JSON
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row:
            return _row_to_dict(row)
        return None
    except Exception:
        logger.debug("SQLite load failed for %s", session_id, exc_info=True)
        return None


def save_session(session_data: dict) -> bool:
    """
    Save a session. session_data should contain:
      session_id, title, model, model_provider, workspace,
      messages (list), pinned, archived, project_id, profile,
      created_at, updated_at, last_message_at, source, is_streaming
    """
    if not SQLITE_ENABLED:
        return False
    try:
        conn = _get_conn()
        messages = session_data.get("messages", [])
        messages_json = json.dumps(messages, ensure_ascii=False)
        message_count = len(messages)

        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (session_id, title, model, model_provider, workspace,
                 messages_json, message_count, pinned, archived,
                 project_id, profile, created_at, updated_at,
                 last_message_at, source, is_streaming)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_data.get("session_id", ""),
                session_data.get("title", "Untitled"),
                session_data.get("model", ""),
                session_data.get("model_provider", ""),
                session_data.get("workspace", ""),
                messages_json,
                message_count,
                int(session_data.get("pinned", False)),
                int(session_data.get("archived", False)),
                session_data.get("project_id", ""),
                session_data.get("profile", "default"),
                session_data.get("created_at", time.time()),
                session_data.get("updated_at", time.time()),
                session_data.get("last_message_at", time.time()),
                session_data.get("source", "webui"),
                int(session_data.get("is_streaming", False)),
            ),
        )
        conn.commit()
        return True
    except Exception:
        logger.debug("SQLite save failed for %s", session_data.get("session_id"), exc_info=True)
        return False


def list_sessions(
    limit: int = 100,
    offset: int = 0,
    profile: str | None = None,
    project_id: str | None = None,
    include_archived: bool = False,
) -> list[dict]:
    """List sessions, newest first."""
    if not SQLITE_ENABLED:
        return []
    try:
        conn = _get_conn()
        where = []
        params = []

        if profile:
            where.append("profile = ?")
            params.append(profile)

        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)

        if not include_archived:
            where.append("archived = 0")

        sql = "SELECT * FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception:
        logger.debug("SQLite list failed", exc_info=True)
        return []


def search_sessions(query: str, limit: int = 50) -> list[str]:
    """Full-text search sessions. Returns list of session_ids."""
    if not SQLITE_ENABLED:
        return []
    try:
        conn = _get_conn()
        # FTS5 query — escape special chars
        safe_query = query.replace('"', '""').replace("'", "''")
        rows = conn.execute(
            """
            SELECT session_id FROM sessions_fts
            WHERE sessions_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, limit),
        ).fetchall()
        return [r["session_id"] for r in rows]
    except Exception:
        logger.debug("SQLite FTS failed", exc_info=True)
        return []


def delete_session(session_id: str) -> bool:
    """Delete a session from SQLite."""
    if not SQLITE_ENABLED:
        return False
    try:
        conn = _get_conn()
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return True
    except Exception:
        logger.debug("SQLite delete failed for %s", session_id, exc_info=True)
        return False


def get_stats() -> dict:
    """Return store statistics."""
    if not SQLITE_ENABLED:
        return {"enabled": False}
    try:
        conn = _get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        archived = conn.execute(
            "SELECT COUNT(*) as c FROM sessions WHERE archived = 1"
        ).fetchone()["c"]
        total_messages = conn.execute(
            "SELECT SUM(message_count) as c FROM sessions"
        ).fetchone()["c"] or 0
        db_size = os.path.getsize(SQLITE_DB_PATH) if os.path.exists(SQLITE_DB_PATH) else 0
        return {
            "enabled": True,
            "total_sessions": total,
            "archived_sessions": archived,
            "total_messages": total_messages,
            "db_size_bytes": db_size,
        }
    except Exception:
        return {"enabled": True, "error": True}


def migrate_from_json(session_dir: str | Path) -> dict:
    """One-time migration from JSON files to SQLite."""
    if not SQLITE_ENABLED:
        return {"migrated": 0, "error": "sqlite not enabled"}

    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        return {"migrated": 0, "error": "session_dir not found"}

    migrated = 0
    errors = 0

    for json_file in session_dir.glob("*.json"):
        # Skip .bak and index files
        if json_file.name.endswith(".bak") or json_file.name == "_index.json":
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            session_id = data.get("session_id") or json_file.stem
            if "session_id" not in data or not data["session_id"]:
                data["session_id"] = session_id
            save_session(data)
            migrated += 1
        except Exception:
            errors += 1
            logger.debug("Migration failed for %s", json_file.name, exc_info=True)

    logger.info("SQLite migration: %d migrated, %d errors", migrated, errors)
    return {"migrated": migrated, "errors": errors}


# ── Initialization ─────────────────────────────────────────────────────────
def init_store():
    """Initialize the SQLite store on startup if enabled."""
    if not SQLITE_ENABLED:
        return
    try:
        conn = _get_conn()
        logger.info(
            "SQLite session store ready (%d sessions)",
            conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"],
        )
    except Exception:
        logger.warning("SQLite store init failed", exc_info=True)
