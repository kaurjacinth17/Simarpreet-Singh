"""Core data types shared by every pipeline stage."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

# Content types a parsed article can carry. Only "report", "official" and
# "explainer" contribute facts to a story summary; "analysis"/"opinion" are
# attached as clearly labelled links.
FACTUAL_TYPES = {"report", "official", "explainer"}


@dataclass
class Article:
    id: str
    source_id: str
    url: str
    title: str
    published: datetime
    time_precision: str = "minute"          # "minute" | "day"
    extract: str = ""
    language: str = "en"
    title_translated: bool = False
    # Filled by later stages
    publisher: str = ""
    owner: str = ""
    tier: int = 0
    credibility: float = 0.0
    content_type: str = "report"            # report | official | explainer | analysis | opinion | promotional | live
    category: str = ""
    topics: list[str] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)
    rejected: Optional[str] = None          # reason, if dropped
    rejected_stage: Optional[str] = None

    def reject(self, stage: str, reason: str) -> None:
        if self.rejected is None:
            self.rejected, self.rejected_stage = reason, stage

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat()
        d["entities"] = sorted(self.entities)
        return d


@dataclass
class Claim:
    text: str
    kind: str          # confirmed | official | reported | allegation | analysis | uncertain
    source: str = ""


@dataclass
class TimelineEntry:
    at: str            # ISO date or datetime
    text: str
    source: str = ""
    precision: str = "day"


@dataclass
class Summary:
    headline: str
    summary: str
    why_it_matters: str
    category: str = ""
    subcategory: str = ""
    topics: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    significance: Optional[float] = None    # model's 0-100 view, blended with rules
    practical: Optional[float] = None       # 0-100 practical impact
    status: str = "confirmed"               # confirmed | developing | unconfirmed
    limited: bool = False
    limited_reason: str = ""
    explainer: dict = field(default_factory=dict)  # what, why, before, who, next
    claims: list[Claim] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    check: dict = field(default_factory=dict)       # optional recall question: q, options, answer (index)
    provider: str = ""


@dataclass
class Story:
    """A cluster of articles about one event, plus everything we computed about it."""
    id: str
    articles: list[Article]
    first_seen: datetime
    last_updated: datetime
    summary: Optional[Summary] = None
    importance: float = 0.0
    credibility: float = 0.0
    freshness: float = 0.0
    practical: float = 0.0
    freshness_label: str = ""
    importance_label: str = ""              # Major | Significant | Developing | Notable
    breaking: bool = False
    rejected: Optional[str] = None
    rejected_stage: Optional[str] = None
    signals: dict = field(default_factory=dict)

    @property
    def factual_articles(self) -> list[Article]:
        return [a for a in self.articles if a.content_type in FACTUAL_TYPES]

    @property
    def independent_owners(self) -> set[str]:
        return {a.owner for a in self.factual_articles}

    def reject(self, stage: str, reason: str) -> None:
        if self.rejected is None:
            self.rejected, self.rejected_stage = reason, stage
