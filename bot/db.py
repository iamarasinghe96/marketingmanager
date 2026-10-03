from __future__ import annotations
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc)


def stamp():
    return utcnow().isoformat()


TRANSITIONS = {
    "planned": {"generating", "skipped", "failed"},
    "generating": {"awaiting_image", "in_review", "failed", "skipped"},
    "awaiting_image": {"generating", "in_review", "skipped", "failed"},
    "in_review": {"approved", "generating", "skipped", "failed"},
    "approved": {"publishing", "generating", "skipped", "failed"},
    "publishing": {"published", "failed", "skipped"},
    "failed": {"generating", "approved", "publishing", "skipped"},
    "published": set(), "skipped": set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(slug TEXT PRIMARY KEY, name TEXT, paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS drafts(
 id TEXT PRIMARY KEY, campaign TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned',
 version INTEGER NOT NULL DEFAULT 0, copy TEXT, post_path TEXT, story_path TEXT,
 feed_layout TEXT, story_layout TEXT, feed_variant INTEGER, story_variant INTEGER,
 compliance TEXT NOT NULL DEFAULT '[]', warnings TEXT NOT NULL DEFAULT '[]',
 approved_version INTEGER, immediate INTEGER NOT NULL DEFAULT 0, reminded INTEGER NOT NULL DEFAULT 0,
 error TEXT, idea TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(campaign, day, kind));
CREATE TABLE IF NOT EXISTS draft_versions(draft_id TEXT, version INTEGER, copy TEXT, post_path TEXT, story_path TEXT,
 compliance TEXT, warnings TEXT, created_at TEXT, PRIMARY KEY(draft_id,version));
CREATE TABLE IF NOT EXISTS assets(id INTEGER PRIMARY KEY, draft_id TEXT, version INTEGER, kind TEXT, path TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS schedules(draft_id TEXT, kind TEXT, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(draft_id,kind));
CREATE TABLE IF NOT EXISTS publications(
 idempotency_key TEXT PRIMARY KEY, draft_id TEXT NOT NULL, platform TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'reserved', stage TEXT, remote_id TEXT, container_id TEXT, upload_id TEXT,
 permalink TEXT, error TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ideas_queue(id INTEGER PRIMARY KEY, campaign TEXT, day TEXT, text TEXT, draft_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS history(draft_id TEXT PRIMARY KEY, campaign TEXT, day TEXT, topic TEXT, headline TEXT,
 feed_layout TEXT, story_layout TEXT, feed_variant INTEGER, story_variant INTEGER);
CREATE TABLE IF NOT EXISTS quota_usage(day TEXT, service TEXT, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day,service));
CREATE TABLE IF NOT EXISTS tokens(service TEXT, account TEXT, value TEXT NOT NULL, expires_at TEXT, checked_at TEXT,
 valid INTEGER, PRIMARY KEY(service,account));
CREATE TABLE IF NOT EXISTS review_messages(chat_id INTEGER, message_id INTEGER, draft_id TEXT, version INTEGER,
 PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS interactions(id TEXT PRIMARY KEY, kind TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS image_requests(id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
 reminded INTEGER NOT NULL DEFAULT 0, asset_path TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incoming_images(id TEXT PRIMARY KEY, campaign TEXT, path TEXT NOT NULL, caption TEXT,
 draft_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notifications(key TEXT PRIMARY KEY, text TEXT, created_at TEXT, sent INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_drafts_state ON drafts(state);
CREATE INDEX IF NOT EXISTS idx_pub_draft ON publications(draft_id);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=20000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def rows(self, sql, args=()):
        return [dict(x) for x in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def sync_campaigns(self, campaigns):
        for c in campaigns.values():
            self.execute("INSERT INTO campaigns(slug,name) VALUES(?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name", (c.slug,c.name))

    def paused(self, slug):
        row = self.one("SELECT paused FROM campaigns WHERE slug=?", (slug,))
        return bool(row and row["paused"])

    def draft(self, draft_id):
        return self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def reserve_draft(self, campaign, day, kind, due_at, idea=""):
        draft_id = uuid.uuid4().hex[:12]
        with self.transaction():
            self.conn.execute("INSERT INTO drafts(id,campaign,day,kind,idea,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                              (draft_id, campaign, day.isoformat(), kind, idea, stamp(), stamp()))
            self.conn.execute("INSERT INTO schedules(draft_id,kind,due_at) VALUES(?,?,?)", (draft_id,kind,due_at.isoformat()))
            if idea:
                self.conn.execute("INSERT INTO ideas_queue(campaign,day,text,draft_id,created_at) VALUES(?,?,?,?,?)",
                                  (campaign,day.isoformat(),idea,draft_id,stamp()))
        return self.draft(draft_id)

    def transition(self, draft_id, target, **fields):
        with self.transaction():
            current = self.draft(draft_id)
            if not current or target not in TRANSITIONS[current["state"]]:
                raise ValueError(f"Cannot move draft to {target}")
            fields.update(state=target, updated_at=stamp())
            allowed = {"state", "updated_at", "approved_version", "error", "immediate", "reminded"}
            if not set(fields) <= allowed:
                raise ValueError("Unknown draft field")
            sql = ",".join(f"{key}=?" for key in fields)
            self.conn.execute(f"UPDATE drafts SET {sql} WHERE id=?", (*fields.values(),draft_id))

    def save_version(self, draft_id, copy, post_path, story_path, layouts, reasons, warnings, awaiting_image=False):
        with self.transaction():
            row = self.draft(draft_id)
            if row["state"] != "generating":
                raise ValueError("Draft is not generating")
            version = row["version"] + 1
            data = copy.model_dump_json()
            reason_json, warning_json = json.dumps(reasons,ensure_ascii=False), json.dumps(warnings,ensure_ascii=False)
            self.conn.execute("INSERT INTO draft_versions VALUES(?,?,?,?,?,?,?,?)",
                              (draft_id,version,data,str(post_path or ''),str(story_path or ''),reason_json,warning_json,stamp()))
            self.conn.execute("UPDATE drafts SET version=?,copy=?,post_path=?,story_path=?,feed_layout=?,story_layout=?,"
                              "feed_variant=?,story_variant=?,compliance=?,warnings=?,state=?,approved_version=NULL,"
                              "error=NULL,updated_at=? WHERE id=?",
                              (version,data,str(post_path or ''),str(story_path or ''),*layouts,reason_json,warning_json,
                               'awaiting_image' if awaiting_image else 'in_review',stamp(),draft_id))
            self.conn.execute("INSERT OR REPLACE INTO history VALUES(?,?,?,?,?,?,?,?,?)",
                              (draft_id,row["campaign"],row["day"],copy.topic,copy.headline,*layouts))
            for kind, path in (("post",post_path),("story",story_path)):
                if not path:
                    continue
                self.conn.execute("INSERT INTO assets(draft_id,version,kind,path,created_at) VALUES(?,?,?,?,?)",
                                  (draft_id,version,kind,str(path),stamp()))
        return self.draft(draft_id)

    def pending(self):
        return self.rows("SELECT * FROM drafts WHERE state IN ('awaiting_image','in_review','approved','failed') ORDER BY created_at DESC")

    def token(self, service, account):
        return self.one("SELECT * FROM tokens WHERE service=? AND account=?", (service,account))

    def save_token(self, service, account, value, expires_at=None, valid=1):
        self.execute("INSERT OR REPLACE INTO tokens VALUES(?,?,?,?,?,?)", (service,account,value,expires_at,stamp(),valid))

    def quota(self, day, service, limit):
        with self.transaction():
            row = self.one("SELECT count FROM quota_usage WHERE day=? AND service=?", (day,service))
            count = row["count"] if row else 0
            if count >= limit:
                return False
            self.conn.execute("INSERT INTO quota_usage VALUES(?,?,1) ON CONFLICT(day,service) DO UPDATE SET count=count+1", (day,service))
        return True

    def notify(self, key, text):
        self.execute("INSERT OR IGNORE INTO notifications(key,text,created_at) VALUES(?,?,?)", (key,text,stamp()))

    def reserve_publication(self, draft_id, platform, kind, dry_run=False):
        key = f"{'dry' if dry_run else 'live'}:{draft_id}:{platform}:{kind}"
        self.execute("INSERT OR IGNORE INTO publications(idempotency_key,draft_id,platform,kind,updated_at) VALUES(?,?,?,?,?)",
                     (key,draft_id,platform,kind,stamp()))
        return self.one("SELECT * FROM publications WHERE idempotency_key=?", (key,))

    def publication_update(self, key, **fields):
        if not set(fields) <= {"status","stage","remote_id","container_id","upload_id","permalink","error"}:
            raise ValueError("Unknown publication field")
        fields["updated_at"] = stamp()
        sql = ",".join(f"{x}=?" for x in fields)
        self.execute(f"UPDATE publications SET {sql} WHERE idempotency_key=?", (*fields.values(),key))
