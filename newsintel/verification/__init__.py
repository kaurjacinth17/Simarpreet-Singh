"""Stage 1: source verification.

An article survives only if its publisher is on the approved registry, its
URL really belongs to that publisher (or a declared syndication partner),
it carries a publication date, and the publisher is a news source rather
than a brokerage or marketing outlet. Content-level checks (promotional
copy, opinion) run after parsing in `verify_content`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import registry
from ..models import Article

NON_NEWS_KINDS = {"brokerage", "marketing", "aggregator"}


def verify_source(a: Article, now: datetime) -> Article:
    reg = registry()
    if reg.is_blocked(a.url):
        a.reject("source", "Blocked domain (blog, newsletter or aggregator)")
        return a
    src = reg.get(a.source_id)
    if src is None:
        a.reject("source", "Publisher not on the approved source list")
        return a
    if not src.owns_url(a.url):
        a.reject("source", f"URL domain does not belong to {src.name}")
        return a
    if src.kind in NON_NEWS_KINDS or src.tier == 0:
        a.reject("source", f"{src.name} publishes brokerage/marketing content, not news reporting")
        return a
    if a.published is None:
        a.reject("source", "No publication date")
        return a
    if a.published > now + timedelta(hours=1):
        a.reject("source", "Publication date is in the future")
        return a
    if not a.title.strip():
        a.reject("source", "Missing headline")
        return a
    a.publisher, a.owner, a.tier, a.credibility = src.name, src.owner, src.tier, float(src.credibility)
    return a


def verify_content(a: Article) -> Article:
    """Runs after parsing: drops promotional copy and discounts sources outside their beat."""
    if a.rejected:
        return a
    if a.content_type == "promotional":
        a.reject("source", "Promotional content")
        return a
    src = registry().get(a.source_id)
    if src and src.subjects:
        on_beat = bool(set(src.subjects) & set(a.topics) | ({a.category.lower()} & set(src.subjects)))
        if not on_beat:
            # Reliable for some subjects only (e.g. a company newsroom on anything but itself).
            a.credibility = round(a.credibility * 0.7, 1)
    return a
