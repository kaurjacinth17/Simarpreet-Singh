"""Runs the full pipeline: ingest -> verify -> parse -> cluster -> summarise -> score -> gate.

    python -m newsintel.pipeline --fixture fixtures/articles_2026-11-19.json --out web/feed.json
    python -m newsintel.pipeline --live --db newsintel.db          # pull authorised RSS feeds
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .clustering import cluster
from urllib.parse import urlparse

from .config import registry, settings
from .models import Article, Story
from .parsing import parse
from .quality import gate
from .scoring import signals
from .summarization import CacheSummarizer, ClaudeSummarizer, summarize
from .verification import verify_content, verify_source


@dataclass
class Result:
    now: datetime
    articles: list[Article]
    stories: list[Story]
    stats: dict = field(default_factory=dict)

    @property
    def live(self) -> list[Story]:
        return [s for s in self.stories if not s.rejected]


def run(articles: list[Article], now: datetime, providers: list) -> Result:
    # Stage 1 + parsing
    for a in articles:
        verify_source(a, now)
        parse(a)
        verify_content(a)
    # Stage 2
    stories = cluster(articles)
    cfg_b = settings()["breaking"]
    for s in stories:
        if s.rejected:
            continue
        s.freshness, s.freshness_label, age_h = signals.freshness(s, now)          # Stage 5
        s.credibility = signals.credibility(s)
        if age_h > settings()["freshness"]["max_age_hours"]:
            gate(s, age_h)          # skip the model call entirely for old news
            continue
        s.summary = summarize(s, providers)                                         # AI summary (cached)
        s.importance = signals.importance(s)                                         # Stage 3
        s.practical = signals.practical(s)
        gate(s, age_h)                                                               # Stage 6
        s.breaking = (not s.rejected and s.importance >= cfg_b["min_importance"]
                      and s.credibility >= cfg_b["min_credibility"] and age_h <= cfg_b["max_age_hours"]
                      and s.summary.status in {"confirmed", "developing"} and not s.summary.limited)
        s.importance_label = signals.label_for(s.importance, s.summary.status, s.breaking)

    rej_articles = [a for a in articles if a.rejected]
    rej_stories = [s for s in stories if s.rejected]
    stats = {
        "articles_scanned": len(articles),
        "articles_rejected": len(rej_articles),
        "stories_formed": len(stories),
        "duplicates_merged": sum(len(s.articles) - 1 for s in stories),
        "stories_rejected": len(rej_stories),
        "stories_kept": len(stories) - len(rej_stories),
        "rejections_by_stage": dict(Counter([a.rejected_stage for a in rej_articles] +
                                            [s.rejected_stage for s in rej_stories])),
    }
    return Result(now=now, articles=articles, stories=stories, stats=stats)


# ---------------------------------------------------------------------------
# Serialisation for the web client
# ---------------------------------------------------------------------------
def _article_json(a: Article) -> dict:
    return {"publisher": a.publisher, "source_id": a.source_id, "url": a.url, "title": a.title,
            "published": a.published.isoformat(), "precision": a.time_precision, "type": a.content_type,
            "tier": a.tier, "translated": a.title_translated}


def story_json(s: Story) -> dict:
    from .feed import topics_of
    sm = s.summary
    return {
        "id": s.id, "headline": sm.headline, "summary": sm.summary, "why": sm.why_it_matters,
        "category": sm.category, "subcategory": sm.subcategory, "topics": topics_of(s), "regions": sm.regions,
        "status": sm.status, "limited": sm.limited, "limited_reason": sm.limited_reason,
        "importance": s.importance, "credibility": s.credibility, "freshness": s.freshness, "practical": s.practical,
        "label": s.importance_label, "breaking": s.breaking, "freshness_label": s.freshness_label,
        "first_seen": s.first_seen.isoformat(), "last_updated": s.last_updated.isoformat(),
        "explainer": sm.explainer, "claims": [c.__dict__ for c in sm.claims],
        "timeline": [t.__dict__ for t in sm.timeline], "check": sm.check,
        "sources": [_article_json(a) for a in sorted(s.factual_articles, key=lambda a: (a.tier, -a.credibility))],
        "analysis": [_article_json(a) for a in s.articles if a.content_type in {"analysis", "opinion"}],
        "independent_sources": len(s.independent_owners), "signals": s.signals, "provider": sm.provider,
    }


def result_json(r: Result) -> dict:
    rejected = []
    for a in r.articles:
        if a.rejected and a.rejected_stage == "source":
            host = (urlparse(a.url).hostname or "").removeprefix("www.")
            src = registry().get(a.source_id)
            rejected.append({"title": a.title, "publisher": a.publisher or (src.name if src else host), "url": a.url,
                             "stage": a.rejected_stage, "reason": a.rejected})
    for s in r.stories:
        if s.rejected:
            lead = s.factual_articles[0] if s.factual_articles else s.articles[0]
            rejected.append({"title": lead.title, "publisher": lead.publisher, "url": lead.url,
                             "stage": s.rejected_stage, "reason": s.rejected, "articles": len(s.articles)})
    return {"generated_at": r.now.isoformat(), "stats": r.stats,
            "weights": settings()["score_weights"], "feed_mix": settings()["feed_mix"],
            "feed_settings": settings()["feed"], "interests": settings()["interests"],
            "stories": [story_json(s) for s in sorted(r.live, key=lambda s: -s.importance)],
            "rejected": rejected}


def main() -> None:
    from .ingestion import FixtureAdapter, RSSAdapter

    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--reviewed", help="file of reviewed summaries for offline builds")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default="feed.json")
    args = ap.parse_args()

    store = None
    if args.db:
        from .db import Store
        store = Store(args.db)
    if args.fixture:
        fx = FixtureAdapter(args.fixture)
        articles, now = list(fx.fetch()), fx.snapshot_time
    else:
        articles, now = list(RSSAdapter().fetch()), datetime.now(timezone.utc)
        if store:
            # Re-cluster against the recent window so new reports join existing stories
            # (a developing event updates one card instead of creating a new one).
            fresh_urls = {a.url for a in articles}
            articles += [a for a in store.recent_articles(settings()["clustering"]["window_hours"])
                         if a.url not in fresh_urls]
    providers = [CacheSummarizer(store, args.reviewed), ClaudeSummarizer(store=store)]
    result = run(articles, now, providers)
    if store:
        store.save_articles(result.articles)
        store.save_stories(result.stories)
    Path(args.out).write_text(json.dumps(result_json(result), indent=1, default=str, ensure_ascii=False))
    print(json.dumps(result.stats, indent=1))


if __name__ == "__main__":
    main()
