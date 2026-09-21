"""Single-account SQLite store. All methods run on the service event-loop thread.

Transactions contain no awaits or network operations. An OS lock prevents two
service processes from accidentally sharing the same database and queue.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .messages import normalize


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, settings, account_id, chats):
        self.settings = settings
        self.chats = chats
        self.db = None
        path = Path(settings.database)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = open(str(path) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_file.seek(0)
                self._lock_file.write(b"0")
                self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(path, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            if os.name == "posix":
                os.chmod(path, 0o600)
                os.chmod(str(path) + ".lock", 0o600)
            self._migrate()
            with self.db:
                previous = self.get_meta("account_id")
                if previous is not None and previous != str(account_id):
                    raise ValueError("Index belongs to another Telegram account; use a separate database")
                self.set_meta("account_id", str(account_id))
                requested = settings.history_since
                since = (datetime.fromisoformat(requested.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
                         if requested else self.get_meta("history_since") or
                         (datetime.now(timezone.utc) - timedelta(days=settings.history_days)).isoformat())
                if since != self.get_meta("history_since"):
                    self.db.execute("DELETE FROM sync_state")
                self.set_meta("history_since", since)
                self.since = since
                # Purge revoked peers on enabled startup. Every read also checks ACL.
                peers = [c.marked_id for c in chats.values()]
                slots = ",".join("?" for _ in peers)
                self.db.execute(f"DELETE FROM messages WHERE peer_id NOT IN ({slots}) OR date < ?", [*peers, since])
                self.db.execute(f"DELETE FROM sync_state WHERE peer_id NOT IN ({slots})", peers)
                for peer in peers:
                    self.db.execute("INSERT OR IGNORE INTO sync_state(peer_id) VALUES (?)", (peer,))
                # The process lock means these jobs can only belong to a dead worker.
                self.db.execute("UPDATE transcription_jobs SET status='queued' WHERE status='processing'")
                self.refill_queue()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("Index schema is newer than this server")
        if version == 1:
            return
        self.db.executescript("""
            BEGIN;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('revision', '0');
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                date TEXT NOT NULL, sender_id INTEGER, outgoing INTEGER NOT NULL,
                kind TEXT NOT NULL, media_version TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(peer_id, message_id)
            );
            CREATE INDEX messages_date ON messages(peer_id, date);
            CREATE TABLE transcripts(
                message_row INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                backend TEXT NOT NULL, media_version TEXT NOT NULL, text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE transcription_jobs(
                message_row INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                polls INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE sync_state(
                peer_id INTEGER PRIMARY KEY, high_id INTEGER NOT NULL DEFAULT 0,
                history_before INTEGER NOT NULL DEFAULT 0, history_done INTEGER NOT NULL DEFAULT 0,
                reconcile_before INTEGER NOT NULL DEFAULT 0,
                last_sync TEXT, last_reconciled TEXT, error TEXT
            );
            CREATE VIRTUAL TABLE search_index USING fts5(text, voice, filename, url, tokenize='trigram');
            CREATE TRIGGER remove_search AFTER DELETE ON messages BEGIN
                DELETE FROM search_index WHERE rowid=old.id;
                UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision';
            END;
            PRAGMA user_version=1;
            COMMIT;
        """)

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def require_peer(self, peer):
        if peer not in {c.marked_id for c in self.chats.values()}:
            raise ValueError("Chat is not allowed")

    def _refresh_search(self, row_id):
        row = self.db.execute("SELECT payload FROM messages WHERE id=?", (row_id,)).fetchone()
        data = json.loads(row[0])
        transcript = self.db.execute("SELECT text FROM transcripts WHERE message_row=?", (row_id,)).fetchone()
        self.db.execute("DELETE FROM search_index WHERE rowid=?", (row_id,))
        self.db.execute("INSERT INTO search_index(rowid,text,voice,filename,url) VALUES (?,?,?,?,?)", (
            row_id, normalize(data["text"]), normalize(transcript[0]) if transcript else "",
            normalize(data["filename"] or ""), normalize("\n".join(data["urls"]))))
        self.db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def _upsert(self, peer, data):
        old = self.db.execute("SELECT * FROM messages WHERE peer_id=? AND message_id=?", (peer, data["message_id"])).fetchone()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if old and old["payload"] == payload:
            return old["id"]
        if old and (old["media_version"] != data["media_version"] or old["kind"] != data["message_type"]):
            self.db.execute("DELETE FROM transcripts WHERE message_row=?", (old["id"],))
            self.db.execute("DELETE FROM transcription_jobs WHERE message_row=?", (old["id"],))
        self.db.execute("""INSERT INTO messages(peer_id,message_id,date,sender_id,outgoing,kind,media_version,payload)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(peer_id,message_id) DO UPDATE SET
            date=excluded.date,sender_id=excluded.sender_id,outgoing=excluded.outgoing,
            kind=excluded.kind,media_version=excluded.media_version,payload=excluded.payload""", (
            peer, data["message_id"], data["date"], data["sender_id"], int(data["outgoing"]),
            data["message_type"], data["media_version"], payload))
        row_id = self.db.execute("SELECT id FROM messages WHERE peer_id=? AND message_id=?", (peer, data["message_id"])).fetchone()[0]
        self._refresh_search(row_id)
        return row_id

    def save_page(self, peer, rows, state=None, delete_range=None):
        self.require_peer(peer)
        with self.db:
            # Deletion is allowed only for a fully fetched ID interval, never on errors.
            if delete_range is not None:
                low, high = delete_range
                ids = [r["message_id"] for r in rows]
                slots = ",".join("?" for _ in ids) or "SELECT NULL WHERE 0"
                self.db.execute(f"DELETE FROM messages WHERE peer_id=? AND message_id>=? AND message_id<? AND message_id NOT IN ({slots})", [peer, low, high, *ids])
            for data in rows:
                if data["date"] >= self.since:
                    self._upsert(peer, data)
            if state:
                allowed = {"high_id", "history_before", "history_done", "reconcile_before", "last_sync", "last_reconciled", "error"}
                if set(state) - allowed:
                    raise ValueError("Invalid sync state")
                self.db.execute("UPDATE sync_state SET " + ",".join(f"{k}=?" for k in state) + " WHERE peer_id=?", [*state.values(), peer])
            self.refill_queue()

    def refill_queue(self):
        if not self.settings.transcribe:
            return
        active = self.db.execute("SELECT count(*) FROM transcription_jobs WHERE status IN ('queued','pending','processing')").fetchone()[0]
        free = max(0, self.settings.max_queue_size - active)
        self.db.execute("""INSERT OR IGNORE INTO transcription_jobs(message_row,status)
            SELECT m.id,'queued' FROM messages m
            LEFT JOIN transcripts t ON t.message_row=m.id
            LEFT JOIN transcription_jobs j ON j.message_row=m.id
            WHERE m.kind IN ('voice','video_note') AND t.message_row IS NULL AND j.message_row IS NULL
            ORDER BY m.date DESC,m.id DESC LIMIT ?""", (free,))

    def state(self, peer):
        self.require_peer(peer)
        return dict(self.db.execute("SELECT * FROM sync_state WHERE peer_id=?", (peer,)).fetchone())

    def get(self, peer, message_id):
        self.require_peer(peer)
        row = self.db.execute("SELECT * FROM messages WHERE peer_id=? AND message_id=?", (peer, message_id)).fetchone()
        return self.present(row) if row else None

    def present(self, row):
        data = json.loads(row["payload"])
        transcript = self.db.execute("SELECT * FROM transcripts WHERE message_row=?", (row["id"],)).fetchone()
        job = self.db.execute("SELECT status,error FROM transcription_jobs WHERE message_row=?", (row["id"],)).fetchone()
        data["transcript"] = transcript["text"] if transcript and self.settings.transcribe else None
        data["transcript_source"] = transcript["backend"] if data["transcript"] is not None else None
        data["transcript_status"] = (
            "not_applicable" if row["kind"] not in {"voice", "video_note"} else
            "disabled" if not self.settings.transcribe else
            "ready" if transcript else job["status"] if job else "not_queued")
        data["transcript_error"] = job["error"] if job and not transcript else None
        if data["transcript"] is not None:
            data["transcript_note"] = "Machine transcription; not a verbatim quote."
        return data

    def context(self, peer, message_id, before, after):
        self.require_peer(peer)
        def side(operator, direction, limit):
            return [self.present(r) for r in self.db.execute(
                f"SELECT * FROM messages WHERE peer_id=? AND message_id {operator} ? ORDER BY message_id {direction} LIMIT ?",
                (peer, message_id, limit))]
        center = self.get(peer, message_id)
        reply = self.get(peer, center["reply_to_message_id"]) if center and center["reply_to_message_id"] else None
        return {"before": list(reversed(side("<", "DESC", before))), "after": side(">", "ASC", after),
                "reply_to": reply, "source": "local_index"}

    def prioritize(self, peer, message_id, retry_failed=False):
        self.require_peer(peer)
        with self.db:
            active = self.db.execute("SELECT count(*) FROM transcription_jobs WHERE status IN ('queued','pending','processing')").fetchone()[0]
            if active < self.settings.max_queue_size:
                if retry_failed:
                    self.db.execute("""UPDATE transcription_jobs SET status='queued',attempts=0,polls=0,error=NULL,next_attempt=0
                        WHERE status='failed' AND message_row=(SELECT id FROM messages WHERE peer_id=? AND message_id=?)""", (peer, message_id))
                self.db.execute("""INSERT OR IGNORE INTO transcription_jobs(message_row,status,priority)
                    SELECT id,'queued',1 FROM messages WHERE peer_id=? AND message_id=? AND kind IN ('voice','video_note')
                    AND id NOT IN (SELECT message_row FROM transcripts)""", (peer, message_id))
            self.db.execute("""UPDATE transcription_jobs SET priority=1,next_attempt=CASE WHEN status='queued' THEN 0 ELSE next_attempt END
                WHERE message_row=(SELECT id FROM messages WHERE peer_id=? AND message_id=?)""", (peer, message_id))

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.refill_queue()
            row = self.db.execute("""SELECT m.*, j.attempts, j.polls FROM transcription_jobs j
                JOIN messages m ON m.id=j.message_row WHERE j.status IN ('queued','pending') AND j.next_attempt<=?
                ORDER BY j.priority DESC,j.next_attempt,m.date DESC LIMIT 1""", (now,)).fetchone()
            if row:
                self.require_peer(row["peer_id"])
                self.db.execute("UPDATE transcription_jobs SET status='processing' WHERE message_row=?", (row["id"],))
                return dict(row)
        return None

    def finish(self, job, text):
        self.require_peer(job["peer_id"])
        with self.db:
            current = self.db.execute("SELECT media_version FROM messages WHERE id=?", (job["id"],)).fetchone()
            if not current or current[0] != job["media_version"]:
                return
            self.db.execute("INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?)", (job["id"], "telegram", job["media_version"], text, utcnow()))
            self.db.execute("DELETE FROM transcription_jobs WHERE message_row=?", (job["id"],))
            self._refresh_search(job["id"])
            self.refill_queue()

    def defer(self, job, status, error=None, delay=0, attempt=False, poll=False):
        with self.db:
            self.db.execute("""UPDATE transcription_jobs SET status=?,error=?,next_attempt=?,attempts=attempts+?,polls=polls+?
                WHERE message_row=? AND EXISTS(SELECT 1 FROM messages WHERE id=? AND media_version=?)""",
                (status, error, time.time() + delay, int(attempt), int(poll), job["id"], job["id"], job["media_version"]))

    def status(self, keys):
        result = []
        for key in keys:
            chat = self.chats[key]
            peer = chat.marked_id
            state = self.state(peer)
            state.pop("peer_id")
            counts = self.db.execute("""SELECT count(*) total,
                sum(CASE WHEN kind IN ('voice','video_note') THEN 1 ELSE 0 END) voices,
                min(date) oldest,max(date) newest FROM messages WHERE peer_id=?""", (peer,)).fetchone()
            ready = self.db.execute("SELECT count(*) FROM transcripts t JOIN messages m ON m.id=t.message_row WHERE m.peer_id=?", (peer,)).fetchone()[0]
            jobs = {r[0]: r[1] for r in self.db.execute("SELECT j.status,count(*) FROM transcription_jobs j JOIN messages m ON m.id=j.message_row WHERE m.peer_id=? GROUP BY j.status", (peer,))}
            voices = counts["voices"] or 0
            result.append({"chat_key": key, "history_since": self.since, **state,
                           "messages": counts["total"], "oldest_message": counts["oldest"], "newest_message": counts["newest"],
                           "voices": voices, "transcripts_ready": ready if self.settings.transcribe else 0,
                           "voices_unavailable": voices - (ready if self.settings.transcribe else 0), "jobs": jobs})
        return result
