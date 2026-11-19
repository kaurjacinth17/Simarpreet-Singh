"""SQLite persistence for articles, stories, the summary cache, users and feedback."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..config import registry
from ..models import Article, Story
from ..users import FeedbackEvent, UserProfile

SCHEMA = Path(__file__).with_name("schema.sql")


class Store:
    def __init__(self, path: str = "newsintel.db"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)  # e.g. /data on a mounted volume
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text())
        self.sync_sources()

    def sync_sources(self) -> None:
        for s in registry().sources.values():
            self.conn.execute(
                "INSERT INTO sources(id,name,tier,credibility,kind,owner) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, tier=excluded.tier, "
                "credibility=excluded.credibility, kind=excluded.kind, owner=excluded.owner",
                (s.id, s.name, s.tier, s.credibility, s.kind, s.owner))
        self.conn.commit()

    # --- articles & stories -------------------------------------------------
    def known_urls(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT url FROM articles")}

    def recent_articles(self, hours: int) -> list[Article]:
        rows = self.conn.execute(
            "SELECT * FROM articles WHERE published_at >= datetime('now', ?) AND rejected_stage IS NULL",
            (f"-{hours} hours",))
        return [Article(id=r["id"], source_id=r["source_id"], url=r["url"], title=r["title"],
                        published=datetime.fromisoformat(r["published_at"]), time_precision=r["time_precision"],
                        extract=r["extract"] or "") for r in rows]

    def save_articles(self, arts: list[Article]) -> None:
        for a in arts:
            if a.source_id not in registry().sources:
                continue  # unapproved publishers are never stored
            self.conn.execute(
                "INSERT OR REPLACE INTO articles(id,source_id,url,title,extract,published_at,time_precision,"
                "content_type,category,topics,rejected_stage,rejected_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.source_id, a.url, a.title, a.extract, a.published.isoformat(), a.time_precision,
                 a.content_type, a.category, json.dumps(a.topics), a.rejected_stage, a.rejected))
        self.conn.commit()

    def save_stories(self, stories: list[Story]) -> None:
        for s in stories:
            sm = s.summary
            self.conn.execute(
                "INSERT OR REPLACE INTO stories(id,first_seen,last_updated,category,subcategory,status,importance,"
                "credibility,freshness,practical,importance_label,breaking,rejected_stage,rejected_reason,signals,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (s.id, s.first_seen.isoformat(), s.last_updated.isoformat(), sm.category if sm else None,
                 sm.subcategory if sm else None, sm.status if sm else None, s.importance, s.credibility,
                 s.freshness, s.practical, s.importance_label, int(s.breaking), s.rejected_stage, s.rejected,
                 json.dumps(s.signals, default=str)))
            for a in s.articles:
                if a.source_id in registry().sources:
                    self.conn.execute("INSERT OR IGNORE INTO story_articles VALUES(?,?)", (s.id, a.id))
        self.conn.commit()

    # --- summary cache ------------------------------------------------------
    def get_summary(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT payload FROM summaries WHERE cluster_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_summary(self, key: str, payload: dict, story_id: str | None = None, provider: str = "claude") -> None:
        self.conn.execute("INSERT OR REPLACE INTO summaries(cluster_key,story_id,payload,provider) VALUES(?,?,?,?)",
                          (key, story_id, json.dumps(payload), provider))
        self.conn.commit()

    # --- server settings -------------------------------------------------------
    def get_kv(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, value))
        self.conn.commit()

    # --- users ---------------------------------------------------------------
    def load_user(self, user_id: str) -> UserProfile:
        row = self.conn.execute("SELECT preferences,last_seen FROM users WHERE id=?", (user_id,)).fetchone()
        user = UserProfile(user_id=user_id)
        if row:
            for k, v in json.loads(row["preferences"]).items():
                if hasattr(user, k) and k not in {"feedback", "last_seen"}:
                    setattr(user, k, v)
            user.last_seen = datetime.fromisoformat(row["last_seen"]) if row["last_seen"] else None
        for f in self.conn.execute("SELECT * FROM feedback WHERE user_id=? ORDER BY at", (user_id,)):
            user.feedback.append(FeedbackEvent(f["story_id"], f["kind"], json.loads(f["topics"] or "[]"),
                                               datetime.fromisoformat(f["at"] + "+00:00"), f["source_id"]))
        user.saved = [r[0] for r in self.conn.execute("SELECT story_id FROM saved_stories WHERE user_id=?", (user_id,))]
        return user

    def save_user(self, user: UserProfile) -> None:
        prefs = {k: v for k, v in asdict(user).items() if k not in {"feedback", "last_seen", "saved", "user_id"}}
        self.conn.execute(
            "INSERT INTO users(id,preferences,last_seen) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "preferences=excluded.preferences, last_seen=excluded.last_seen",
            (user.user_id, json.dumps(prefs), user.last_seen.isoformat() if user.last_seen else None))
        self.conn.commit()

    def add_feedback(self, user_id: str, story_id: str, kind: str, topics: list[str], source_id: str | None) -> None:
        self.conn.execute("INSERT INTO feedback(user_id,story_id,kind,topics,source_id) VALUES(?,?,?,?,?)",
                          (user_id, story_id, kind, json.dumps(topics), source_id))
        self.conn.commit()

    def undo_feedback(self, user_id: str, story_id: str) -> None:
        self.conn.execute("DELETE FROM feedback WHERE user_id=? AND story_id=?", (user_id, story_id))
        self.conn.commit()

    def set_saved(self, user_id: str, story_id: str, saved: bool) -> None:
        if saved:
            self.conn.execute("INSERT OR IGNORE INTO saved_stories(user_id,story_id) VALUES(?,?)", (user_id, story_id))
        else:
            self.conn.execute("DELETE FROM saved_stories WHERE user_id=? AND story_id=?", (user_id, story_id))
        self.conn.commit()

    def add_push_subscription(self, user_id: str, sub: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO push_subscriptions(endpoint,user_id,subscription) VALUES(?,?,?)",
                          (sub["endpoint"], user_id, json.dumps(sub)))
        self.conn.commit()

    def push_subscriptions(self) -> list[tuple[str, dict]]:
        return [(r[0], json.loads(r[1])) for r in self.conn.execute("SELECT user_id, subscription FROM push_subscriptions")]

    def recent_push_subscriptions(self, minutes: int) -> list[tuple[str, dict]]:
        rows = self.conn.execute("SELECT user_id, subscription FROM push_subscriptions "
                                 "WHERE created_at >= datetime('now', ?)", (f"-{int(minutes)} minutes",))
        return [(r[0], json.loads(r[1])) for r in rows]

    def remove_push_subscription(self, endpoint: str) -> None:
        self.conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        self.conn.commit()

    def user_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM users")]

    def digests_sent(self, user_id: str) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT key FROM digest_log WHERE user_id=?", (user_id,))}

    def mark_digest(self, user_id: str, key: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO digest_log(user_id,key) VALUES(?,?)", (user_id, key))
        self.conn.commit()

    def report(self, user_id: str | None, story_id: str, reason: str) -> None:
        self.conn.execute("INSERT INTO reports(user_id,story_id,reason) VALUES(?,?,?)", (user_id, story_id, reason))
        self.conn.commit()
