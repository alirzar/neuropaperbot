"""The public bot's database: who uses it, the papers it knows, and what each person was sent,
liked, saved and follows.

It's one SQLite file, data/db/bot.db, kept out of git because it holds people's Telegram IDs and
interests. /stop deletes everything about a person.
"""
import json
import sqlite3
import threading
from datetime import timedelta

import numpy as np

from common import DATA, iso, now_utc, shorten, short_id
from vectors import pack, unpack

DB_PATH = DATA / "db" / "bot.db"  # in a folder of its own, with the files SQLite adds while it runs

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,          -- Telegram user ID, which is also their private chat's ID
    name TEXT,
    username TEXT,
    status TEXT NOT NULL,            -- pending, active or denied
    is_admin INTEGER NOT NULL DEFAULT 0,
    step TEXT,                       -- what setup is waiting for, or NULL once it's done
    topics TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '',
    tz TEXT,
    hour INTEGER,
    per_digest INTEGER,
    profile BLOB,                    -- a fingerprint per topic and keyword phrase
    last_digest TEXT,                -- their local date of the last digest
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers (
    pid TEXT PRIMARY KEY,            -- short ID used in Telegram buttons
    doi TEXT UNIQUE NOT NULL,        -- lowercase
    source TEXT, venue TEXT, title TEXT NOT NULL, authors TEXT, author_keys TEXT,
    date TEXT, link TEXT, abstract TEXT, license TEXT,
    quality INTEGER, reason TEXT, hook TEXT, summary TEXT,
    vector BLOB,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    user_id INTEGER NOT NULL, pid TEXT NOT NULL, kind TEXT NOT NULL, message_id INTEGER,
    feedback INTEGER NOT NULL DEFAULT 0,    -- 1 for 👍, -1 for 👎
    saved INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    PRIMARY KEY (user_id, pid)
);
CREATE TABLE IF NOT EXISTS follows (
    user_id INTEGER NOT NULL, author_key TEXT NOT NULL, label TEXT NOT NULL,
    PRIMARY KEY (user_id, author_key)
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (user_id, day, kind)
);
CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _user(row):
    user = dict(row)
    user["topics"] = json.loads(user["topics"])
    user["profile"] = unpack(user["profile"])
    return user


def _paper(row):
    paper = dict(row)
    paper["authors"] = json.loads(paper["authors"] or "[]")
    paper["author_keys"] = json.loads(paper["author_keys"] or "[]")
    paper["vector"] = unpack(paper["vector"])
    paper["snippet"] = shorten(paper["abstract"] or "", 300)
    return paper


class Store:
    def __init__(self, path=DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._local = threading.local()  # one connection per thread: the daily update runs in its own
        self.db.executescript(SCHEMA)

    @property
    def db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # every statement commits
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    def run(self, sql, *args):
        return self.db.execute(sql, args)

    # ---------- small settings ----------
    def get(self, key, default=None):
        row = self.run("SELECT value FROM meta WHERE key=?", key).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.run("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", key, str(value))

    def cached_vector(self, key):
        row = self.run("SELECT vector FROM vectors WHERE key=?", key).fetchone()
        return unpack(row[0])[0] if row else None

    def cache_vector(self, key, vector):
        self.run("INSERT OR REPLACE INTO vectors(key, vector) VALUES (?, ?)", key, pack(vector))

    # ---------- people ----------
    def user(self, uid):
        row = self.run("SELECT * FROM users WHERE id=?", uid).fetchone()
        return _user(row) if row else None

    def users(self, status=None):
        if status:
            rows = self.run("SELECT * FROM users WHERE status=? ORDER BY created_at", status)
        else:
            rows = self.run("SELECT * FROM users ORDER BY created_at")
        return [_user(r) for r in rows]

    def add_user(self, uid, name, username, status, is_admin=False):
        self.run(
            "INSERT OR IGNORE INTO users(id, name, username, status, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            uid, name, username, status, int(is_admin), iso(),
        )

    def update_user(self, uid, **fields):
        if "topics" in fields:
            fields["topics"] = json.dumps(fields["topics"])
        if fields.get("profile") is not None:
            fields["profile"] = pack(fields["profile"])
        sets = ", ".join(f"{name}=?" for name in fields)
        self.run(f"UPDATE users SET {sets} WHERE id=?", *fields.values(), uid)

    def delete_user(self, uid):
        for table, column in (("sent", "user_id"), ("follows", "user_id"), ("usage", "user_id"), ("users", "id")):
            self.run(f"DELETE FROM {table} WHERE {column}=?", uid)

    def use(self, uid, kind, day):
        """Count one use of a limited feature today, and return today's count."""
        self.run("INSERT OR IGNORE INTO usage(user_id, day, kind, n) VALUES (?, ?, ?, 0)", uid, day, kind)
        self.run("UPDATE usage SET n=n+1 WHERE user_id=? AND day=? AND kind=?", uid, day, kind)
        return self.run("SELECT n FROM usage WHERE user_id=? AND day=? AND kind=?", uid, day, kind).fetchone()[0]

    # ---------- papers ----------
    def has_paper(self, doi):
        return self.run("SELECT 1 FROM papers WHERE doi=?", doi.lower()).fetchone() is not None

    def add_paper(self, p, vector=None, quality=None, reason=None):
        pid = short_id(p["doi"])
        self.run(
            "INSERT OR IGNORE INTO papers(pid, doi, source, venue, title, authors, author_keys, date, link, abstract,"
            " license, quality, reason, vector, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            pid, p["doi"].lower(), p.get("source"), p.get("venue"), p["title"], json.dumps(p.get("authors") or []),
            json.dumps(p.get("author_keys") or []), p.get("date"), p.get("link"), p.get("abstract"), p.get("license"),
            quality, reason, None if vector is None else pack(vector), iso(),
        )
        return pid

    def paper(self, pid):
        row = self.run("SELECT * FROM papers WHERE pid=?", pid).fetchone()
        return _paper(row) if row else None

    def paper_by_doi(self, doi):
        row = self.run("SELECT * FROM papers WHERE doi=?", doi.lower()).fetchone()
        return _paper(row) if row else None

    def set_paper(self, pid, **fields):
        sets = ", ".join(f"{name}=?" for name in fields)
        self.run(f"UPDATE papers SET {sets} WHERE pid=?", *fields.values(), pid)

    def recent(self, days):
        """Papers added in the last `days` days that have a fingerprint."""
        since = iso(now_utc() - timedelta(days=days))
        return [_paper(r) for r in self.run("SELECT * FROM papers WHERE added_at>=? AND vector IS NOT NULL", since)]

    def prune(self, days):
        """Forget papers older than `days` days, except ones someone saved."""
        before = iso(now_utc() - timedelta(days=days))
        self.run("DELETE FROM papers WHERE added_at<? AND pid NOT IN (SELECT pid FROM sent WHERE saved=1)", before)
        self.run("DELETE FROM sent WHERE pid NOT IN (SELECT pid FROM papers)")

    # ---------- what each person was sent ----------
    def record_sent(self, uid, pid, kind, message_id):
        self.run(
            "INSERT OR IGNORE INTO sent(user_id, pid, kind, message_id, at) VALUES (?, ?, ?, ?, ?)",
            uid, pid, kind, message_id, iso(),
        )

    def sent_row(self, uid, pid):
        row = self.run("SELECT * FROM sent WHERE user_id=? AND pid=?", uid, pid).fetchone()
        return dict(row) if row else None

    def sent_pids(self, uid):
        return {r[0] for r in self.run("SELECT pid FROM sent WHERE user_id=?", uid)}

    def set_feedback(self, uid, pid, value):
        self.run("UPDATE sent SET feedback=? WHERE user_id=? AND pid=?", value, uid, pid)

    def toggle_saved(self, uid, pid):
        self.run("UPDATE sent SET saved=1-saved WHERE user_id=? AND pid=?", uid, pid)
        return bool(self.sent_row(uid, pid)["saved"])

    def saved_papers(self, uid, limit):
        rows = self.run(
            "SELECT papers.* FROM sent JOIN papers USING (pid) WHERE user_id=? AND saved=1 ORDER BY sent.at DESC LIMIT ?",
            uid, limit,
        )
        return [_paper(r) for r in rows]

    def feedback_vectors(self, uid):
        """Fingerprints of the papers this person gave 👍 and 👎, as (liked, disliked); None when there are none."""
        result = []
        for value in (1, -1):
            rows = self.run(
                "SELECT vector FROM sent JOIN papers USING (pid) WHERE user_id=? AND feedback=? AND vector IS NOT NULL",
                uid, value,
            ).fetchall()
            result.append(np.vstack([unpack(r[0]) for r in rows]) if rows else None)
        return tuple(result)

    # ---------- author alerts ----------
    def follow(self, uid, key, label):
        self.run("INSERT OR REPLACE INTO follows(user_id, author_key, label) VALUES (?, ?, ?)", uid, key, label)

    def unfollow(self, uid, key):
        return self.run("DELETE FROM follows WHERE user_id=? AND author_key=?", uid, key).rowcount > 0

    def follows(self, uid):
        return [dict(r) for r in self.run("SELECT * FROM follows WHERE user_id=? ORDER BY label", uid)]

    def follow_keys(self):
        """Every followed author, from people who are still active."""
        rows = self.run("SELECT DISTINCT author_key FROM follows JOIN users ON users.id=follows.user_id WHERE status='active'")
        return {r[0] for r in rows}

    def followers(self, key):
        return [(r[0], r[1]) for r in self.run("SELECT user_id, label FROM follows WHERE author_key=?", key)]
