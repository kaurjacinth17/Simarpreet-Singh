"""AI summarisation, with caching and a strict validator.

Providers, tried in order:
  1. CacheSummarizer  - summaries already stored for this exact set of articles
                        (and, for offline builds, a file of reviewed summaries)
  2. ClaudeSummarizer - calls the Anthropic Messages API server-side; the key is
                        read from ANTHROPIC_API_KEY and never reaches the client
  3. ExtractiveFallback - no model available: shows the best source's own
                        description, clearly marked as a limited summary

Every summary goes through `validate`. If a model output fails (too long,
numbers that appear in no source, missing attribution), the story falls back
to a limited summary instead of showing unverified text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Protocol

from ..config import settings
from ..models import Claim, Story, Summary, TimelineEntry

PROMPT = """You are the news editor of a credibility-first news app. Summarise ONE news event from the
source articles below. Use only facts stated in the sources. Never add facts, numbers, names, quotes or
dates that are not in the sources.

Rules:
- headline: neutral, factual, at most 90 characters, no clickbait, no question marks.
- summary: 40-60 words, plain language, keep key numbers, attribute claims ("X said").
- why_it_matters: 1-2 sentences on the consequence for the public, the economy or an industry.
  This is analysis: use "could"/"may" for anything not yet true.
- Separate what is confirmed from what is claimed. An allegation must never be stated as fact.
- Opinion or analysis sources may be cited only as analysis, never as facts.
- If the sources are too thin or contradictory to summarise confidently, set "limited": true and say why.
- Return JSON only, matching this schema:
{"headline": str, "summary": str, "why_it_matters": str, "category": str, "subcategory": str,
 "topics": [str], "regions": [str], "significance": 0-100, "practical": 0-100,
 "status": "confirmed"|"developing"|"unconfirmed", "limited": bool, "limited_reason": str,
 "explainer": {"what": str, "why": str, "before": str, "who": str, "next": str},
 "claims": [{"text": str, "kind": "confirmed"|"official"|"reported"|"allegation"|"analysis"|"uncertain", "source": str}],
 "timeline": [{"at": "YYYY-MM-DD", "text": str, "source": str}],
 "check": {"q": str, "options": [str, str, str], "answer": 0-2}}
"check" is one multiple-choice recall question answerable from the summary alone, with two plausible wrong options.
Allowed categories: {categories}.

SOURCES:
{sources}
"""


def cluster_key(story: Story) -> str:
    urls = sorted(a.url for a in story.articles)
    return hashlib.sha256("\n".join(urls).encode()).hexdigest()[:16]


def _summary_from_dict(d: dict, provider: str) -> Summary:
    return Summary(
        headline=d["headline"].strip(), summary=d["summary"].strip(), why_it_matters=d.get("why_it_matters", "").strip(),
        category=d.get("category", ""), subcategory=d.get("subcategory", ""), topics=d.get("topics", []),
        regions=d.get("regions", []), significance=d.get("significance"), practical=d.get("practical"),
        status=d.get("status", "confirmed"), limited=bool(d.get("limited", False)),
        limited_reason=d.get("limited_reason", ""), explainer=d.get("explainer", {}),
        claims=[Claim(**c) for c in d.get("claims", [])],
        timeline=[TimelineEntry(**t) for t in d.get("timeline", [])], check=d.get("check") or {}, provider=provider,
    )


class Summarizer(Protocol):
    def summarize(self, story: Story) -> Summary | None: ...


class CacheSummarizer:
    """Exact cache (by article set) plus an optional reviewed-summaries file matched by URL overlap."""

    def __init__(self, store=None, reviewed_path: str | Path | None = None):
        self.store = store
        self.reviewed = json.loads(Path(reviewed_path).read_text())["summaries"] if reviewed_path else []

    def summarize(self, story: Story) -> Summary | None:
        if self.store:
            hit = self.store.get_summary(cluster_key(story))
            if hit and "_failed" not in hit:
                return _summary_from_dict(hit, "cache")
        urls = {a.url for a in story.articles}
        best, best_overlap = None, 0.0
        for entry in self.reviewed:
            match = set(entry["match_urls"])
            overlap = len(urls & match) / len(match)
            if overlap > best_overlap:
                best, best_overlap = entry, overlap
        if best and best_overlap >= 0.5:
            return _summary_from_dict(best, "reviewed")
        return None


class ClaudeSummarizer:
    """Calls Claude once per article set. A failed attempt (API error, unparseable reply, or a summary
    that fails `validate`) is remembered, so the same articles are not re-sent on every refresh;
    it is retried after `summary.retry_failed_hours`, or at once if a new article joins the story."""

    def __init__(self, model: str | None = None, store=None):
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("NEWSINTEL_MODEL", "claude-sonnet-5-5")
        self.store = store

    def _recently_failed(self, story: Story) -> bool:
        if not self.store:
            return False
        hit = self.store.get_summary(cluster_key(story))
        if not hit or "_failed" not in hit:
            return False
        age_h = (time.time() - hit.get("_at", 0)) / 3600
        return age_h < settings()["summary"].get("retry_failed_hours", 6)

    def mark_failed(self, story: Story, reason: str) -> None:
        if self.store:
            self.store.put_summary(cluster_key(story), {"_failed": reason[:300], "_at": time.time()},
                                   provider="claude-failed")

    def summarize(self, story: Story) -> Summary | None:
        if not self.key or self._recently_failed(story):
            return None
        data = self._call(story)
        if self.store:
            self.store.put_summary(cluster_key(story), data)
        return _summary_from_dict(data, "claude")

    def _call(self, story: Story) -> dict:
        import httpx

        sources = "\n\n".join(
            f"[{i+1}] {a.publisher} | {a.content_type} | {a.published.date()} | {a.url}\n{a.title}\n{a.extract}"
            for i, a in enumerate(story.articles))
        prompt = PROMPT.replace("{categories}", ", ".join(settings()["categories"])).replace("{sources}", sources)
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages", timeout=60,
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]},
        )
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json()["content"])
        return json.loads(text[text.find("{"): text.rfind("}") + 1])


class ExtractiveFallback:
    def summarize(self, story: Story) -> Summary:
        lead = max(story.factual_articles, key=lambda a: (a.credibility, a.published))
        sentences = re.split(r"(?<=[.!?])\s+", lead.extract)
        text = " ".join(sentences[:2])
        return Summary(headline=lead.title[:90], summary=text, why_it_matters="", category=lead.category,
                       topics=lead.topics, limited=True, provider="extractive",
                       limited_reason=f"Automatic summary unavailable; showing {lead.publisher}'s own description.")


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_APPROX = re.compile(r"\b(about|around|roughly|nearly|almost|some|over|more than|near)\s+[$₹]?\s?$", re.I)


def _norm(n: str) -> str:
    n = n.replace(",", "").rstrip(".")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n.lstrip("0") or "0"


def _numbers(text: str) -> set[str]:
    return {_norm(n) for n in _NUM.findall(text)}


def _grounded(n: str, approx: bool, grounded: set[str]) -> bool:
    if n in grounded:
        return True
    if approx:  # "about 92" may round a reported 92.14
        v = float(n)
        return any(abs(float(g) - v) <= max(0.02 * v, 0.5) for g in grounded if g.replace(".", "", 1).isdigit())
    return False


def validate(summary: Summary, story: Story) -> list[str]:
    """Return a list of problems; empty means the summary may be shown."""
    cfg = settings()["summary"]
    problems = []
    words = len(summary.summary.split())
    if not summary.limited and not (cfg["min_words"] <= words <= cfg["max_words"]):
        problems.append(f"summary is {words} words (want {cfg['min_words']}-{cfg['max_words']})")
    if len(summary.headline) > 90:
        problems.append("headline longer than 90 characters")
    if re.search(r"(you won't believe|shocking|\?\s*$|!)", summary.headline, re.I):
        problems.append("headline reads as clickbait")
    source_text = " ".join(f"{a.title} {a.extract}" for a in story.articles)
    grounded = _numbers(source_text)
    for a in story.articles:  # publication dates are facts of record too
        grounded |= {str(a.published.day), str(a.published.year)}
    for field in (summary.headline, summary.summary):
        for m in _NUM.finditer(field):
            n = _norm(m.group())
            approx = bool(_APPROX.search(field[:m.start()]))
            if not _grounded(n, approx, grounded):
                problems.append(f"number {m.group()} not found in any source")
    if summary.category and summary.category not in settings()["categories"]:
        problems.append(f"unknown category {summary.category}")
    return problems


def summarize(story: Story, providers: list) -> Summary:
    for p in providers:
        mark_failed = getattr(p, "mark_failed", None)
        try:
            s = p.summarize(story)
        except Exception as exc:  # a provider failing must never take the feed down
            story.signals.setdefault("summary_errors", []).append(f"{type(p).__name__}: {exc}")
            if mark_failed:
                mark_failed(story, f"{type(exc).__name__}: {exc}")
            continue
        if s is None:
            continue
        problems = validate(s, story)
        if not problems:
            return s
        story.signals.setdefault("summary_errors", []).append(f"{s.provider}: " + "; ".join(problems))
        if mark_failed:
            mark_failed(story, "; ".join(problems))
    return ExtractiveFallback().summarize(story)
