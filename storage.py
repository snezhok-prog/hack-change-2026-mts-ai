"""SQLite persistence for a single-process demo; no contact transmission."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                  role TEXT NOT NULL, text TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
                CREATE TABLE IF NOT EXISTS leads (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
                  name TEXT NOT NULL, phone TEXT NOT NULL, company TEXT NOT NULL,
                  summary TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_session(self) -> str:
        identifier = uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?, ?)', (identifier, now()))
        return identifier

    def session(self, identifier: str) -> dict | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (identifier,)).fetchone()
            if row is None:
                return None
            messages = [dict(m) for m in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id', (identifier,))]
            for message in messages:
                message['metadata'] = json.loads(message['metadata'])
            lead = db.execute('SELECT * FROM leads WHERE session_id=?', (identifier,)).fetchone()
            return {**dict(row), 'messages': messages, 'lead': dict(lead) if lead else None}

    def message(self, session: str, role: str, text: str, metadata: dict | None = None) -> None:
        with self.connect() as db:
            db.execute('INSERT INTO messages(session_id, role, text, metadata, created_at) VALUES (?,?,?,?,?)',
                       (session, role, text, json.dumps(metadata or {}, ensure_ascii=False), now()))

    def lead(self, session: str, name: str, phone: str, company: str, summary: str) -> dict:
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?,?)',
                       (uuid4().hex, session, name, phone, company, summary, now()))
            return dict(db.execute('SELECT * FROM leads WHERE session_id=?', (session,)).fetchone())

    def recent(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT s.id, s.created_at,
                (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                EXISTS(SELECT 1 FROM leads l WHERE l.session_id=s.id) AS has_lead
                FROM sessions s ORDER BY created_at DESC LIMIT ?''', (limit,))]
