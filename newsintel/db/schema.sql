-- NewsIntel storage (SQLite; the same schema runs on Postgres with minor type changes).
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, tier INTEGER NOT NULL, credibility REAL NOT NULL,
  kind TEXT NOT NULL, owner TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), url TEXT NOT NULL,
  title TEXT NOT NULL, extract TEXT, published_at TEXT NOT NULL, time_precision TEXT NOT NULL,
  content_type TEXT, category TEXT, topics TEXT, rejected_stage TEXT, rejected_reason TEXT,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (url, title)  -- live blogs carry several items at one URL
);
CREATE TABLE IF NOT EXISTS stories (
  id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_updated TEXT NOT NULL,
  category TEXT, subcategory TEXT, status TEXT, importance REAL, credibility REAL, freshness REAL,
  practical REAL, importance_label TEXT, breaking INTEGER NOT NULL DEFAULT 0,
  rejected_stage TEXT, rejected_reason TEXT, signals TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS story_articles (
  story_id TEXT NOT NULL REFERENCES stories(id), article_id TEXT NOT NULL REFERENCES articles(id),
  PRIMARY KEY (story_id, article_id)
);
-- Summary cache keyed by the exact set of article URLs, so unchanged clusters never call the model again.
CREATE TABLE IF NOT EXISTS summaries (
  cluster_key TEXT PRIMARY KEY, story_id TEXT, payload TEXT NOT NULL, provider TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, preferences TEXT NOT NULL, last_seen TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, story_id TEXT NOT NULL, kind TEXT NOT NULL,
  topics TEXT, source_id TEXT, at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS saved_stories (
  user_id TEXT NOT NULL, story_id TEXT NOT NULL, saved_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, story_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, story_id TEXT NOT NULL, reason TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY, user_id TEXT NOT NULL, subscription TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS digest_log (
  user_id TEXT NOT NULL, key TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, key)            -- key = local date | HH:MM, so each briefing goes once a day
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_stories_updated ON stories(last_updated);
-- Server-generated settings (VAPID keys, link-signing secret). Lives with the data, never in git.
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
