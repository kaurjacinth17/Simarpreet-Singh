"""Story-level signals: importance (stage 3), credibility, freshness (stage 5), practical impact.

Each signal is 0-100 and explained in story.signals so the UI and the
'Why am I seeing this?' panel can show the reasoning.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..config import settings
from ..models import Story

# Base importance by what kind of event it is (highest matching topic wins).
TOPIC_BASE = [
    ({"conflict", "gulf"}, 72, "Armed conflict affecting a major shipping and energy route"),
    ({"geopolitics", "diplomacy"}, 70, "Leader-level diplomacy between major powers"),
    ({"public-health"}, 66, "Public-health emergency or global health policy"),
    ({"central-banks", "rates"}, 66, "Central-bank policy outlook"),
    ({"inflation"}, 62, "Inflation data"),
    ({"climate"}, 60, "Global climate negotiations"),
    ({"earnings", "semiconductors"}, 60, "Results of a company central to the AI industry"),
    ({"courts"}, 54, "Court decision with policy consequences"),
    ({"drug-regulation"}, 52, "Health regulation change"),
    ({"oil"}, 52, "Energy prices"),
    ({"pollution"}, 50, "City-wide public-health conditions"),
    ({"health"}, 46, "Health"),
    ({"ipo"}, 36, "Capital-markets deal"),
    ({"markets"}, 34, "Daily market move"),
    ({"startups", "venture"}, 30, "Startup funding"),
    ({"education", "careers"}, 30, "Education notice"),
]
MAGNITUDE = [
    (r"\$\s?\d+(\.\d+)?\s?(billion|trillion|bn)\b|₹\s?[\d,]{4,}\s?crore", 6, "Large sums involved"),
    (r"\b\d{1,3}(,\d{3})+\s+(cases|deaths|people)\b|\b(million|billion) people\b", 6, "Affects many people"),
    (r"\b(unprecedented|historic|first[- ]ever|first time)\b", 4, "Unprecedented development"),
]
ROUTINE = [r"\bdaily\b.*\broundup\b", r"\bclosing bell\b", r"\bredemption (price|rate)\b", r"\badmit card\b",
           r"\bmarket close\b", r"\bdaily market\b", r"\bipo updates\b"]
TRIVIAL = [r"\bsaid to\b", r"\bprivately vent", r"\bslams\b", r"\bfeud\b", r"\bcelebrit", r"\bviral\b", r"\bgossip\b"]
PRACTICAL_BASE = {"inflation": 72, "rates": 66, "oil": 66, "pollution": 75, "drug-regulation": 60,
                  "public-health": 55, "conflict": 52, "geopolitics": 45, "climate": 48, "courts": 45,
                  "markets": 50, "ipo": 40, "earnings": 42, "education": 50, "startups": 30}


def _text(story: Story) -> str:
    return " ".join(f"{a.title}. {a.extract}" for a in story.factual_articles)


def _topics(story: Story) -> set[str]:
    from .relevance import story_topics
    return story_topics(story)


def importance(story: Story) -> float:
    topics, text = _topics(story), _text(story).lower()
    base, why = 25, "General news"
    for keys, val, label in TOPIC_BASE:
        if keys <= topics or (len(keys) == 1 and keys & topics):
            if val > base:
                base, why = val, label
    reasons = [f"{why} (+{base})"]
    score = float(base)
    for pat, pts, label in MAGNITUDE:
        if re.search(pat, text):
            score += pts
            reasons.append(f"{label} (+{pts})")
    owners = len(story.independent_owners)
    if owners > 1:
        bonus = min(12, 3 * (owners - 1))
        score += bonus
        reasons.append(f"Covered by {owners} independent newsrooms (+{bonus})")
    if any(a.content_type == "official" for a in story.articles):
        score += 4
        reasons.append("Primary/official source (+4)")
    titles = " ".join(a.title.lower() for a in story.factual_articles)
    if any(re.search(p, titles) for p in ROUTINE):
        score -= 15
        reasons.append("Routine update (-15)")
    if any(re.search(p, titles) for p in TRIVIAL):
        score = min(score - 25, 25)
        reasons.append("Gossip or political point-scoring (capped at 25)")
    if owners == 1 and max(a.tier for a in story.factual_articles) >= 3:
        score -= 8
        reasons.append("Single specialist source (-8)")
    rules = max(0.0, min(100.0, score))
    model = story.summary.significance if story.summary else None
    final = rules if model is None else 0.6 * rules + 0.4 * model
    story.signals["importance"] = {"rules": round(rules), "model": model, "reasons": reasons}
    return round(final, 1)


def credibility(story: Story) -> float:
    facts = story.factual_articles
    best = max(a.credibility for a in facts)
    owners = len(story.independent_owners)
    bonus = min(10, 4 * (owners - 1))
    story.signals["credibility"] = {"best_source": max(facts, key=lambda a: a.credibility).publisher,
                                    "independent_sources": owners, "corroborated": owners >= 2}
    return round(min(100.0, best + bonus), 1)


def freshness(story: Story, now: datetime) -> tuple[float, str, float]:
    cfg = settings()["freshness"]
    facts = story.factual_articles
    latest = max(a.published for a in facts)
    first = min(a.published for a in facts)
    age_h = (now - latest).total_seconds() / 3600
    first_h = (now - first).total_seconds() / 3600
    score = 100 * 0.5 ** (max(age_h, 0) / cfg["half_life_hours"])
    if first_h <= cfg["new_event_hours"]:
        label = "New"
    elif age_h <= cfg["new_event_hours"]:
        label = "New development"
    else:
        label = "Earlier this week"
    story.last_updated, story.first_seen = latest, first
    return round(score, 1), label, age_h


def practical(story: Story) -> float:
    if story.summary and story.summary.practical is not None:
        return float(story.summary.practical)
    topics = _topics(story)
    return float(max([PRACTICAL_BASE.get(t, 25) for t in topics] or [25]))


def label_for(importance_score: float, status: str, breaking: bool) -> str:
    if breaking and status == "developing":
        return "Developing"
    if importance_score >= 78:
        return "Major"
    if importance_score >= 62:
        return "Significant"
    if status == "developing":
        return "Developing"
    return "Notable"
