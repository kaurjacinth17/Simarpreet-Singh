"""Stage 0: ingestion. Every adapter yields Article objects; nothing downstream knows where they came from."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

from ..config import registry
from ..models import Article


class Adapter(Protocol):
    def fetch(self) -> Iterable[Article]: ...


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class FixtureAdapter:
    """Loads a saved batch of real articles (used for the snapshot build and tests)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())

    @property
    def snapshot_time(self) -> datetime:
        return _dt(self.data["snapshot_utc"])

    def fetch(self) -> Iterable[Article]:
        for a in self.data["articles"]:
            yield Article(
                id=a["id"], source_id=a["source"], url=a["url"], title=a["title"],
                published=_dt(a["published"]), time_precision=a.get("time_precision", "minute"),
                extract=a.get("extract", ""), language=a.get("language", "en"),
                title_translated=a.get("title_translated", False),
            )


class RSSAdapter:
    """Pulls every authorised feed listed in config/sources.json.

    Uses feedparser; any one feed failing is logged and skipped so the
    app never depends on a single source.
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.report: list[dict] = []   # per-feed outcome of the last fetch, shown on /api/health

    def fetch(self) -> Iterable[Article]:
        import hashlib
        import logging

        import feedparser
        import httpx

        log = logging.getLogger("ingest.rss")
        self.report = []
        for source, feed_url in registry().feeds():
            entry = {"source": source.id, "url": feed_url, "ok": False, "items": 0, "dated_items": 0, "error": None}
            self.report.append(entry)
            try:
                resp = httpx.get(feed_url, timeout=self.timeout, follow_redirects=True,
                                 headers={"User-Agent": "NewsIntel/1.0 (+feed reader)"})
                resp.raise_for_status()
            except Exception as exc:  # network errors must not stop the run
                log.warning("feed failed %s: %s", feed_url, exc)
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                continue
            parsed = feedparser.parse(resp.content)
            entry["items"] = len(parsed.entries)
            entry["ok"] = bool(parsed.entries)
            if not parsed.entries:
                entry["error"] = "no items (not an RSS/Atom feed, or empty)"
            for e in parsed.entries:
                url = e.get("link")
                if not url:
                    continue
                ts = e.get("published_parsed") or e.get("updated_parsed")
                if ts is None:
                    precision, published = "unknown", None
                else:
                    precision = "minute"
                    published = datetime(*ts[:6], tzinfo=timezone.utc)
                if published is None:
                    # Stage 1 requires a date; drop rather than guess. Keep a sample of what the feed
                    # did send, so /api/health shows why (e.g. a date format feedparser can't read).
                    if "undated_sample" not in entry:
                        raw = {k: str(e.get(k))[:80] for k in ("published", "updated", "date", "dc_date", "created")
                               if e.get(k)}
                        entry["undated_sample"] = {"title": e.get("title", "")[:80], "raw_dates": raw,
                                                   "fields": sorted(k for k in e.keys() if not k.endswith("_detail"))[:20]}
                    continue
                entry["dated_items"] += 1
                yield Article(
                    id=hashlib.sha1(url.encode()).hexdigest()[:12],
                    source_id=source.id, url=url, title=e.get("title", "").strip(),
                    published=published, time_precision=precision,
                    extract=_strip_html(e.get("summary", "")),
                )


def _strip_html(s: str) -> str:
    import html
    import re
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()
