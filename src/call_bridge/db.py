"""SQLite persistence for call sessions and messages."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_lock = threading.RLock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CallStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with _lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    local_id TEXT NOT NULL,
                    local_label TEXT NOT NULL,
                    member_name TEXT NOT NULL,
                    purpose TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    hangup_by TEXT,
                    hangup_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
                CREATE INDEX IF NOT EXISTS idx_sessions_local ON sessions(local_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_member ON sessions(member_name);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    from_party TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_to_local INTEGER NOT NULL DEFAULT 0,
                    delivered_to_member INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(session_id, seq),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
                """
            )

    def open_session(
        self,
        local_id: str,
        local_label: str,
        member_name: str,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = _utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, local_id, local_label, member_name, purpose,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ringing', ?, ?)
                """,
                (session_id, local_id, local_label, member_name, purpose, now, now),
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def list_sessions(
        self,
        party: str | None = None,
        local_id: str | None = None,
        member_name: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if local_id:
            clauses.append("local_id = ?")
            params.append(local_id)
        if member_name:
            clauses.append("member_name = ?")
            params.append(member_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        else:
            # default: active (ringing/open)
            clauses.append("status IN ('ringing', 'open')")
        # party filter is soft metadata — local_id/member_name already cover identity
        _ = party
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions{where} ORDER BY updated_at DESC",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def send_message(
        self, session_id: str, from_party: str, message: str, reply_required: bool = True
    ) -> dict[str, Any]:
        if from_party not in ("local", "member"):
            raise ValueError("from_party must be 'local' or 'member'")
        with self._conn() as conn:
            sess = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if sess is None:
                raise KeyError(f"session not found: {session_id}")
            if sess["status"] == "hungup":
                raise RuntimeError("session already hung up")
            now = _utcnow()
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            if from_party == "local" and reply_required:
                message += (
                    "\n\nこの連絡には返信が必要です。"
                    f"session_id={session_id} の通話に、member として call_send で返事を送ってください。"
                )
            conn.execute(
                """
                INSERT INTO messages (
                    session_id, seq, from_party, body, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, seq, from_party, message, now),
            )
            new_status = "open" if sess["status"] == "ringing" else sess["status"]
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (new_status, now, session_id),
            )
            return {
                "session_id": session_id,
                "seq": seq,
                "from_party": from_party,
                "message": message,
                "created_at": now,
                "status": new_status,
            }

    def poll_messages(
        self,
        session_id: str,
        party: str,
        after_seq: int = 0,
        mark_delivered: bool = True,
    ) -> dict[str, Any]:
        if party not in ("local", "member"):
            raise ValueError("party must be 'local' or 'member'")
        with self._conn() as conn:
            sess = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if sess is None:
                raise KeyError(f"session not found: {session_id}")
            # Messages FROM the other party, after after_seq
            other = "member" if party == "local" else "local"
            rows = conn.execute(
                """
                SELECT seq, from_party, body AS message, created_at
                FROM messages
                WHERE session_id = ? AND from_party = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (session_id, other, after_seq),
            ).fetchall()
            messages = [dict(r) for r in rows]
            if mark_delivered and messages:
                col = (
                    "delivered_to_local"
                    if party == "local"
                    else "delivered_to_member"
                )
                seqs = [m["seq"] for m in messages]
                placeholders = ",".join("?" * len(seqs))
                conn.execute(
                    f"UPDATE messages SET {col} = 1 "
                    f"WHERE session_id = ? AND seq IN ({placeholders})",
                    [session_id, *seqs],
                )
                # ringing → open when the called party first polls
                if sess["status"] == "ringing" and party == "member":
                    conn.execute(
                        "UPDATE sessions SET status = 'open', updated_at = ? WHERE session_id = ?",
                        (_utcnow(), session_id),
                    )
            status_row = conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return {
                "session_id": session_id,
                "party": party,
                "status": status_row["status"],
                "messages": messages,
                "latest_seq": messages[-1]["seq"] if messages else after_seq,
            }

    def hangup(
        self, session_id: str, by_party: str, reason: str | None = None
    ) -> dict[str, Any]:
        if by_party not in ("local", "member", "ops"):
            raise ValueError("by_party must be 'local', 'member', or 'ops'")
        with self._conn() as conn:
            sess = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if sess is None:
                raise KeyError(f"session not found: {session_id}")
            now = _utcnow()
            conn.execute(
                """
                UPDATE sessions
                SET status = 'hungup', hangup_by = ?, hangup_reason = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (by_party, reason, now, session_id),
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def session_info(self, session_id: str) -> dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"session not found: {session_id}")
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(seq), 0) AS max_seq "
                "FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return {
            **sess,
            "message_count": count["c"],
            "max_seq": count["max_seq"],
        }
