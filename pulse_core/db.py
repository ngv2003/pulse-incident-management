import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = str(Path(__file__).parent.parent / "pulse.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'investigating',
            severity TEXT DEFAULT 'critical',
            source TEXT DEFAULT 'watcher',
            triggered_at TEXT NOT NULL,
            resolved_at TEXT,
            rca_what TEXT,
            rca_timeline TEXT,
            rca_root_cause TEXT,
            rca_confidence INTEGER,
            rca_action TEXT,
            steps TEXT DEFAULT '[]',
            recommended_actions TEXT DEFAULT '[]',
            auto_resolved INTEGER DEFAULT 0,
            rulebook_id TEXT,
            bob_executed_fix TEXT
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS actions_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL,
            service TEXT NOT NULL,
            action TEXT NOT NULL,
            command TEXT,
            result TEXT,
            executed_by TEXT DEFAULT 'bob',
            executed_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def upsert_incident(incident_id: str, **fields):
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM incidents WHERE id = ?", (incident_id,)
    ).fetchone()
    if existing:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [incident_id]
        conn.execute(f"UPDATE incidents SET {set_clause} WHERE id = ?", vals)
    else:
        fields["id"] = incident_id
        if "triggered_at" not in fields:
            fields["triggered_at"] = datetime.utcnow().isoformat()
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        conn.execute(
            f"INSERT INTO incidents ({cols}) VALUES ({placeholders})",
            list(fields.values())
        )
    conn.commit()
    conn.close()


def get_incident(incident_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_incidents():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY triggered_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_active_incident(service: str) -> bool:
    """
    Returns True if there is any open incident for this service.
    Blocks on: investigating, needs_action, executing — anything not yet resolved.
    This prevents the watcher from creating duplicate incidents while one is
    awaiting user approval or while BOB is executing a fix.
    """
    conn = get_conn()
    row = conn.execute(
        """SELECT id FROM incidents
           WHERE service = ?
           AND status NOT IN ('resolved', 'auto_resolved')
           LIMIT 1""",
        (service,)
    ).fetchone()
    conn.close()
    return row is not None


def append_step(incident_id: str, step: str):
    conn = get_conn()
    row = conn.execute("SELECT steps FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row:
        steps = json.loads(row[0] or "[]")
        steps.append({"text": step, "ts": datetime.utcnow().isoformat()})
        conn.execute("UPDATE incidents SET steps = ? WHERE id = ?",
                     (json.dumps(steps), incident_id))
        conn.commit()
    conn.close()


def add_chat_message(incident_id: str, role: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_messages (incident_id, role, content, created_at) VALUES (?,?,?,?)",
        (incident_id, role, content, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_chat_messages(incident_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE incident_id = ? ORDER BY id",
        (incident_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_action(incident_id: str, service: str, action: str, command: str, result: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO actions_log (incident_id, service, action, command, result, executed_at) VALUES (?,?,?,?,?,?)",
        (incident_id, service, action, command, result, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def get_service_action_history(service: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM actions_log WHERE service = ? ORDER BY executed_at DESC LIMIT 10",
        (service,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"[DB] Initialised {DB_PATH}")