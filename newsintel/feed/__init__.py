"""Feed generation: Your Feed, What's Important, Breaking and Catch Me Up."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import settings
from ..models import Story
from ..scoring import total_score
from ..scoring.relevance import relevance, story_topics
from ..users import UserProfile


def rank(stories: list[Story], user: UserProfile) -> list[dict]:
    rows = []
    for s in stories:
        if s.rejected or s.id in user.hidden_stories:
            continue
        if user.hidden_sources and all(a.source_id in user.hidden_sources for a in s.factual_articles):
            continue
        rel, why = relevance(s, user)
        total, parts = total_score(s, rel)
        rows.append({"story": s, "relevance": rel, "why": why, "score": total, "parts": parts})
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def your_feed(stories: list[Story], user: UserProfile) -> list[dict]:
    """75% personalised, 25% reserved for globally important stories (configurable).

    Shows fewer items rather than padding with low-scoring ones.
    """
    cfg, mix = settings()["feed"], settings()["feed_mix"]
    personal_share = user.personal_share if user.personal_share is not None else mix["personal"]
    ranked = [r for r in rank(stories, user) if r["score"] >= cfg["min_total_score"]]
    n = min(cfg["max_items"], len(ranked))
    n_global = round(n * (1 - personal_share))
    personal = [r for r in ranked if r["relevance"] >= cfg["relevance_floor"]]
    excluded = set(user.excluded_topics)  # an explicit "never show me" wins in For you; Important still lists it
    global_pool = sorted((r for r in ranked if r["story"].importance >= cfg["global_importance_floor"]
                          and not (story_topics(r["story"]) & excluded)),
                         key=lambda r: r["story"].importance, reverse=True)
    picked, ids = [], set()
    for r in global_pool[:n_global]:
        r = dict(r, slot="global")
        picked.append(r)
        ids.add(r["story"].id)
    for r in personal:
        if len(picked) >= n:
            break
        if r["story"].id not in ids:
            picked.append(dict(r, slot="personal"))
            ids.add(r["story"].id)
    return sorted(picked, key=lambda r: r["score"], reverse=True)


def whats_important(stories: list[Story], user: UserProfile) -> list[dict]:
    """Ranked by importance alone; personalisation and exclusions do not hide must-know events."""
    cfg = settings()["feed"]
    live = [s for s in stories if not s.rejected and s.importance >= cfg["global_importance_floor"]]
    return [{"story": s, "score": s.importance} for s in sorted(live, key=lambda s: s.importance, reverse=True)]


def breaking(stories: list[Story]) -> list[Story]:
    return sorted((s for s in stories if s.breaking and not s.rejected), key=lambda s: s.last_updated, reverse=True)


def catch_me_up(stories: list[Story], user: UserProfile, now: datetime) -> dict:
    """3 most important, 2 worth knowing (personal), 1 developing story to watch."""
    since = user.last_seen or now - timedelta(hours=24)
    fresh = [s for s in stories if not s.rejected and s.last_updated >= since]
    top = sorted(fresh, key=lambda s: s.importance, reverse=True)[:3]
    rest = [r["story"] for r in rank(fresh, user) if r["story"] not in top]
    worth = rest[:2]
    recent = [s for s in stories if not s.rejected and s.last_updated >= now - timedelta(hours=72)]
    developing = [s for s in sorted(recent, key=lambda s: s.importance, reverse=True)
                  if s.summary and s.summary.status == "developing" and s not in top + worth]
    return {"since": since, "top": top, "worth_knowing": worth, "watch": developing[:1],
            "skipped": max(0, len(fresh) - len(top) - len(worth) - len(developing[:1]))}


def topics_of(story: Story) -> list[str]:
    return sorted(story_topics(story))
