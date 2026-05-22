"""SQLite database layer for fleet manager."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Session:
    session_id: str
    tmux_session: str
    tmux_pane: str
    project_root: str | None
    session_type: str
    web_url: str | None
    web_port: int | None
    web_host: str | None
    process_pid: int | None
    state: str  # IDLE, WORKING, AWAITING_INPUT, ERROR
    summary: str | None
    detail: str | None
    last_seen: str | None
    created_at: str


@dataclass
class Question:
    question_id: str
    session_id: str
    items: str  # JSON
    context: str | None
    answered: bool
    answer: str | None  # JSON
    created_at: str
    answered_at: str | None


@dataclass
class InboxMessage:
    message_id: str
    session_id: str
    content: str
    from_client: str | None
    delivered: bool
    created_at: str
    delivered_at: str | None


@dataclass
class StatusLogEntry:
    id: int
    session_id: str
    state: str
    summary: str | None
    detail: str | None
    timestamp: str


_db: sqlite3.Connection | None = None


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def init_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    global _db
    if db_path is None:
        db_path = Path(__file__).parent.parent / "data" / "fleet.db"
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = sqlite3.connect(str(db_path), check_same_thread=False)
    _db.row_factory = _row_factory
    _db.execute("PRAGMA journal_mode = WAL")
    _db.execute("PRAGMA foreign_keys = ON")
    _create_schema()
    _migrate(_db)
    return _db


def _migrate(db: sqlite3.Connection) -> None:
    """Run schema migrations for existing databases."""
    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "claude_session_id" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN claude_session_id TEXT")
        db.commit()

    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "agent" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN agent TEXT DEFAULT 'opencode'")
        db.commit()

    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "session_type" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN session_type TEXT DEFAULT 'agent'")
        db.commit()

    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "web_url" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN web_url TEXT")
        db.commit()

    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "web_port" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN web_port INTEGER")
        db.commit()

    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "web_host" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN web_host TEXT DEFAULT '127.0.0.1'")
        db.commit()

    cursor = db.execute("PRAGMA table_info(sessions)")
    columns = {row["name"] for row in cursor.fetchall()}
    if "process_pid" not in columns:
        db.execute("ALTER TABLE sessions ADD COLUMN process_pid INTEGER")
        db.commit()

    cursor = db.execute("PRAGMA table_info(inbox)")
    inbox_columns = {row["name"] for row in cursor.fetchall()}
    if "raw" not in inbox_columns:
        db.execute("ALTER TABLE inbox ADD COLUMN raw BOOLEAN DEFAULT 0")
        db.commit()


def _create_schema() -> None:
    assert _db is not None
    _db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id    TEXT PRIMARY KEY,
            tmux_session  TEXT NOT NULL,
            tmux_pane     TEXT DEFAULT '0',
            project_root  TEXT,
            session_type  TEXT DEFAULT 'agent',
            web_url       TEXT,
            web_port      INTEGER,
            web_host      TEXT DEFAULT '127.0.0.1',
            process_pid   INTEGER,
            state         TEXT DEFAULT 'IDLE',
            summary       TEXT,
            detail        TEXT,
            agent         TEXT DEFAULT 'opencode',
            claude_session_id TEXT,
            last_seen     DATETIME,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS questions (
            question_id   TEXT PRIMARY KEY,
            session_id    TEXT NOT NULL,
            items         TEXT NOT NULL,
            context       TEXT,
            answered      BOOLEAN DEFAULT 0,
            answer        TEXT,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            answered_at   DATETIME,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS inbox (
            message_id    TEXT PRIMARY KEY,
            session_id    TEXT NOT NULL,
            content       TEXT NOT NULL,
            from_client   TEXT,
            raw           BOOLEAN DEFAULT 0,
            delivered     BOOLEAN DEFAULT 0,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            delivered_at  DATETIME,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS status_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL,
            state         TEXT NOT NULL,
            summary       TEXT,
            detail        TEXT,
            timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)


def get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


# ── Sessions ──

def create_session(
    session_id: str,
    tmux_session: str,
    tmux_pane: str = "0",
    project_root: str | None = None,
    agent: str = "opencode",
    session_type: str = "agent",
    web_url: str | None = None,
    web_port: int | None = None,
    web_host: str | None = None,
    process_pid: int | None = None,
) -> dict[str, Any]:
    db = get_db()
    db.execute(
        """INSERT INTO sessions (
               session_id, tmux_session, tmux_pane, project_root, agent,
               session_type, web_url, web_port, web_host, process_pid, state, last_seen
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'IDLE', datetime('now'))""",
        (session_id, tmux_session, tmux_pane, project_root, agent, session_type, web_url, web_port, web_host, process_pid),
    )
    db.commit()
    return get_session(session_id)  # type: ignore[return-value]


def update_web_session(
    session_id: str,
    *,
    web_url: str | None = None,
    web_port: int | None = None,
    web_host: str | None = None,
    process_pid: int | None = None,
) -> dict[str, Any]:
    db = get_db()
    db.execute(
        """UPDATE sessions
           SET web_url = COALESCE(?, web_url),
               web_port = COALESCE(?, web_port),
               web_host = COALESCE(?, web_host),
               process_pid = COALESCE(?, process_pid)
           WHERE session_id = ?""",
        (web_url, web_port, web_host, process_pid, session_id),
    )
    db.commit()
    return get_session(session_id)  # type: ignore[return-value]


def get_session(session_id: str) -> dict[str, Any] | None:
    return get_db().execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()


def get_all_sessions() -> list[dict[str, Any]]:
    return get_db().execute(
        "SELECT * FROM sessions ORDER BY created_at DESC"
    ).fetchall()


def update_status(
    session_id: str,
    state: str,
    summary: str,
    project_root: str | None = None,
    detail: str | None = None,
    tmux_prefix: str = "fleet-",
) -> dict[str, Any]:
    db = get_db()
    # Auto-register if session doesn't exist
    existing = get_session(session_id)
    if not existing:
        create_session(session_id, tmux_session=f"{tmux_prefix}{session_id}")

    db.execute(
        """UPDATE sessions
           SET state = ?, summary = ?, detail = ?,
               project_root = COALESCE(?, project_root),
               last_seen = datetime('now')
           WHERE session_id = ?""",
        (state, summary, detail, project_root, session_id),
    )
    db.commit()
    log_status(session_id, state, summary, detail)
    return get_session(session_id)  # type: ignore[return-value]


def update_claude_session_id(session_id: str, claude_session_id: str) -> None:
    db = get_db()
    db.execute(
        "UPDATE sessions SET claude_session_id = ? WHERE session_id = ?",
        (claude_session_id, session_id),
    )
    db.commit()




def delete_session(session_id: str) -> bool:
    db = get_db()
    # Clean up related records (status_log has no FK cascade)
    db.execute("DELETE FROM status_log WHERE session_id = ?", (session_id,))
    cursor = db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    db.commit()
    return cursor.rowcount > 0


# ── Status Log ──

def log_status(
    session_id: str, state: str, summary: str | None = None, detail: str | None = None
) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO status_log (session_id, state, summary, detail) VALUES (?, ?, ?, ?)",
        (session_id, state, summary, detail),
    )
    db.commit()


def get_status_log(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return get_db().execute(
        "SELECT * FROM status_log WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()


# ── Questions ──

def create_question(
    session_id: str, items: str, context: str | None = None
) -> dict[str, Any]:
    db = get_db()
    question_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO questions (question_id, session_id, items, context) VALUES (?, ?, ?, ?)",
        (question_id, session_id, items, context),
    )
    db.commit()
    return get_question(question_id)  # type: ignore[return-value]


def get_question(question_id: str) -> dict[str, Any] | None:
    return get_db().execute(
        "SELECT * FROM questions WHERE question_id = ?", (question_id,)
    ).fetchone()


def get_pending_questions(session_id: str | None = None) -> list[dict[str, Any]]:
    db = get_db()
    if session_id:
        return db.execute(
            "SELECT * FROM questions WHERE session_id = ? AND answered = 0 ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
    return db.execute(
        "SELECT * FROM questions WHERE answered = 0 ORDER BY created_at DESC"
    ).fetchall()


def answer_question(question_id: str, answer: str) -> dict[str, Any] | None:
    db = get_db()
    db.execute(
        "UPDATE questions SET answered = 1, answer = ?, answered_at = datetime('now') WHERE question_id = ?",
        (answer, question_id),
    )
    db.commit()
    return get_question(question_id)


# ── Inbox Messages ──

def create_inbox_message(
    session_id: str, content: str, from_client: str | None = None, raw: bool = False
) -> dict[str, Any]:
    db = get_db()
    message_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO inbox (message_id, session_id, content, from_client, raw) VALUES (?, ?, ?, ?, ?)",
        (message_id, session_id, content, from_client, int(raw)),
    )
    db.commit()
    return get_message(message_id)  # type: ignore[return-value]


def get_message(message_id: str) -> dict[str, Any] | None:
    return get_db().execute(
        "SELECT * FROM inbox WHERE message_id = ?", (message_id,)
    ).fetchone()


def get_queued_messages(session_id: str) -> list[dict[str, Any]]:
    return get_db().execute(
        "SELECT * FROM inbox WHERE session_id = ? AND delivered = 0 ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()


def mark_message_delivered(message_id: str) -> None:
    db = get_db()
    db.execute(
        "UPDATE inbox SET delivered = 1, delivered_at = datetime('now') WHERE message_id = ?",
        (message_id,),
    )
    db.commit()
