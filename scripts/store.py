"""The public bot's database: who uses it, the papers it knows, what each person was sent, liked,
saved and follows, and the journals people added.

It's one SQLite file, data/db/bot.db, kept out of git because it holds people's Telegram and
Discord IDs and interests. /stop deletes everything about a person.

People from both apps share one table: Telegram IDs have at most 52 bits and Discord's are larger,
so the two never collide.
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
    id INTEGER PRIMARY KEY,          -- their Telegram or Discord user ID
    platform TEXT NOT NULL DEFAULT 'telegram',  -- telegram or discord
    name TEXT,
    username TEXT,
    status TEXT NOT NULL,            -- pending, active or denied
    is_admin INTEGER NOT NULL DEFAULT 0,
    step TEXT,                       -- what setup is waiting for, or NULL once it's done
    topics TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '',
    tz TEXT,
    hour INTEGER,
    days TEXT,                       -- the weekdays it comes (JSON list, 0 is Monday), or NULL for every day
    lookback INTEGER,                -- how many days back their digest reaches, or NULL for the default
    per_digest INTEGER,
    sources TEXT,                    -- the sources they ticked (JSON list of keys); NULL until they tick one
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
    added_at TEXT NOT NULL,
    src TEXT,                        -- the source it came from, like "pubmed:neuron" (see catalog.py)
    hidden INTEGER NOT NULL DEFAULT 0  -- 1: never in digests or searches (looked up by DOI, or off topic)
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
CREATE TABLE IF NOT EXISTS watches (   -- topics people asked to hear about (/watch)
    user_id INTEGER NOT NULL, phrase TEXT NOT NULL,
    PRIMARY KEY (user_id, phrase)
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (user_id, day, kind)
);
CREATE TABLE IF NOT EXISTS sources (   -- journals people added with /sources, offered to everyone
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, query TEXT NOT NULL, name TEXT NOT NULL,
    broad INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
ADDED_COLUMNS = [  # columns that came later, added to older databases when the bot starts
    ("users", "sources", "TEXT"),
    ("users", "platform", "TEXT NOT NULL DEFAULT 'telegram'"),
    ("users", "days", "TEXT"),
    ("users", "lookback", "INTEGER"),
    ("papers", "src", "TEXT"),
    ("papers", "hidden", "INTEGER NOT NULL DEFAULT 0"),
]


def _user(row):
    user = dict(row)
    user["topics"] = json.loads(user["topics"])
    user["sources"] = None if user["sources"] is None else json.loads(user["sources"])
    user["days"] = None if user["days"] is None else json.loads(user["days"])
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
        for table, column, kind in ADDED_COLUMNS:
            if column not in {row[1] for row in self.run(f"PRAGMA table_info({table})")}:
                self.run(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

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

    def add_user(self, uid, name, username, status, is_admin=False, platform="telegram"):
        self.run(
            "INSERT OR IGNORE INTO users(id, platform, name, username, status, is_admin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            uid, platform, name, username, status, int(is_admin), iso(),
        )

    def update_user(self, uid, **fields):
        for name in ("topics", "sources", "days"):
            if fields.get(name) is not None:
                fields[name] = json.dumps(fields[name])
        if fields.get("profile") is not None:
            fields["profile"] = pack(fields["profile"])
        sets = ", ".join(f"{name}=?" for name in fields)
        self.run(f"UPDATE users SET {sets} WHERE id=?", *fields.values(), uid)

    def delete_user(self, uid):
        for table, column in (("sent", "user_id"), ("follows", "user_id"), ("watches", "user_id"), ("usage", "user_id"), ("users", "id")):
            self.run(f"DELETE FROM {table} WHERE {column}=?", uid)

    def use(self, uid, kind, day):
        """Count one use of a limited feature today, and return today's count."""
        self.run("INSERT OR IGNORE INTO usage(user_id, day, kind, n) VALUES (?, ?, ?, 0)", uid, day, kind)
        self.run("UPDATE usage SET n=n+1 WHERE user_id=? AND day=? AND kind=?", uid, day, kind)
        return self.run("SELECT n FROM usage WHERE user_id=? AND day=? AND kind=?", uid, day, kind).fetchone()[0]

    # ---------- papers ----------
    def has_paper(self, doi):
        return self.run("SELECT 1 FROM papers WHERE doi=?", doi.lower()).fetchone() is not None

    def add_paper(self, p, vector=None, quality=None, reason=None, hidden=False, added_at=None):
        """Save a paper; added_at is when it counts as new (now, or its own date for older papers filled in)."""
        pid = short_id(p["doi"])
        self.run(
            "INSERT OR IGNORE INTO papers(pid, doi, source, venue, src, title, authors, author_keys, date, link, abstract,"
            " license, quality, reason, hidden, vector, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            pid, p["doi"].lower(), p.get("source"), p.get("venue"), p.get("src"), p["title"],
            json.dumps(p.get("authors") or []), json.dumps(p.get("author_keys") or []), p.get("date"), p.get("link"),
            p.get("abstract"), p.get("license"), quality, reason, int(bool(hidden)),
            None if vector is None else pack(vector), added_at or iso(),
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

    def set_vector(self, pid, vector):
        self.run("UPDATE papers SET vector=? WHERE pid=?", pack(vector), pid)

    def unembedded(self, since=None, before=None, limit=-1):
        """Papers waiting for their fingerprint, newest first: all of them, or those added since or before a time."""
        rows = self.run(
            "SELECT * FROM papers WHERE vector IS NULL AND hidden=0 AND added_at>=? AND added_at<? ORDER BY added_at DESC LIMIT ?",
            since or "", before or "9999", limit,
        )
        return [_paper(r) for r in rows]

    def newest_unembedded(self, before):
        """When the newest paper added before `before` that still waits for its fingerprint counts from, or None."""
        row = self.run("SELECT MAX(added_at) FROM papers WHERE vector IS NULL AND hidden=0 AND added_at<?", before).fetchone()
        return row[0] if row else None

    def oldest_paper(self):
        """When the oldest fingerprinted paper counts from, or None."""
        return self.run("SELECT MIN(added_at) FROM papers WHERE vector IS NOT NULL AND hidden=0").fetchone()[0]

    def candidates(self, days):
        """Just what ranking needs (pid, src, quality, vector) for the papers of the last `days` days:
        quick even for a year of papers."""
        since = iso(now_utc() - timedelta(days=days))
        rows = self.run("SELECT pid, src, quality, vector FROM papers WHERE added_at>=? AND vector IS NOT NULL AND hidden=0", since)
        return [{"pid": r[0], "src": r[1], "quality": r[2], "vector": unpack(r[3])} for r in rows]

    def recent(self, days):
        """Papers added in the last `days` days that have a fingerprint."""
        since = iso(now_utc() - timedelta(days=days))
        rows = self.run("SELECT * FROM papers WHERE added_at>=? AND vector IS NOT NULL AND hidden=0", since)
        return [_paper(r) for r in rows]

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

    # ---------- topic watches ----------
    def watch(self, uid, phrase):
        self.run("INSERT OR IGNORE INTO watches(user_id, phrase) VALUES (?, ?)", uid, phrase)

    def unwatch(self, uid, phrase):
        return self.run("DELETE FROM watches WHERE user_id=? AND lower(phrase)=lower(?)", uid, phrase).rowcount > 0

    def watches(self, uid=None):
        """(user ID, phrase) for each topic watched, by one person or by everyone who's active."""
        if uid is not None:
            return [tuple(r) for r in self.run("SELECT user_id, phrase FROM watches WHERE user_id=? ORDER BY phrase", uid)]
        rows = self.run("SELECT user_id, phrase FROM watches JOIN users ON users.id=watches.user_id WHERE status='active'")
        return [tuple(r) for r in rows]

    # ---------- what everyone liked, and numbers for the admin ----------
    def popular(self, days, n, people=2):
        """This week's papers that at least `people` people gave 👍 or saved, most first: (paper, likes, saves)."""
        since = iso(now_utc() - timedelta(days=days))
        rows = self.run(
            "SELECT papers.*, SUM(feedback=1) AS likes, SUM(saved=1) AS saves FROM sent JOIN papers USING (pid)"
            " WHERE papers.added_at>=? AND (feedback=1 OR saved=1)"
            " GROUP BY pid HAVING COUNT(DISTINCT user_id)>=? ORDER BY likes + saves DESC, likes DESC LIMIT ?",
            since, people, n,
        ).fetchall()
        return [(_paper({k: r[k] for k in r.keys() if k not in ("likes", "saves")}), r["likes"], r["saves"]) for r in rows]

    def stats(self, days):
        """Counts for /stats over the last `days` days."""
        since = iso(now_utc() - timedelta(days=days))
        one = lambda sql, *args: self.run(sql, *args).fetchone()[0] or 0  # noqa: E731
        return {
            "telegram": one("SELECT COUNT(*) FROM users WHERE status='active' AND platform='telegram'"),
            "discord": one("SELECT COUNT(*) FROM users WHERE status='active' AND platform='discord'"),
            "waiting": one("SELECT COUNT(*) FROM users WHERE status='pending'"),
            "papers": one("SELECT COUNT(*) FROM papers WHERE added_at>=? AND hidden=0 AND vector IS NOT NULL", since),
            "off_topic": one("SELECT COUNT(*) FROM papers WHERE added_at>=? AND hidden=1 AND vector IS NULL AND quality IS NOT NULL", since),
            "hooks": one("SELECT COUNT(*) FROM papers WHERE added_at>=? AND hook IS NOT NULL", since),
            "summaries": one("SELECT COUNT(*) FROM papers WHERE added_at>=? AND summary IS NOT NULL", since),
            "digest": one("SELECT COUNT(*) FROM sent WHERE at>=? AND kind IN ('digest', 'more')", since),
            "alerts": one("SELECT COUNT(*) FROM sent WHERE at>=? AND kind IN ('alert', 'watch')", since),
            "likes": one("SELECT COUNT(*) FROM sent WHERE at>=? AND feedback=1", since),
            "dislikes": one("SELECT COUNT(*) FROM sent WHERE at>=? AND feedback=-1", since),
            "saves": one("SELECT COUNT(*) FROM sent WHERE at>=? AND saved=1", since),
            "searches": one("SELECT SUM(n) FROM usage WHERE day>=? AND kind IN ('search', 'journal')", since[:10]),
            "watches": one("SELECT COUNT(*) FROM watches"),
        }

    # ---------- sources ----------
    def chosen_sources(self):
        """Every source at least one active person has ticked."""
        keys = set()
        for (value,) in self.run("SELECT sources FROM users WHERE status='active' AND sources IS NOT NULL"):
            keys.update(json.loads(value))
        return keys

    def added_sources(self):
        return [dict(r) for r in self.run("SELECT * FROM sources ORDER BY added_at")]

    def add_source(self, s):
        self.run(
            "INSERT OR IGNORE INTO sources(key, kind, query, name, broad, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            s["key"], s["kind"], s["query"], s["name"], int(s["broad"]), iso(),
        )

    def drop_sources(self, keep):
        """Forget added journals that nobody has on any more."""
        for row in self.added_sources():
            if row["key"] not in keep:
                self.run("DELETE FROM sources WHERE key=?", row["key"])
