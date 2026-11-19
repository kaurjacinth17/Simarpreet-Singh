"""Loads the source registry and pipeline settings from /config."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@dataclass
class Source:
    id: str
    name: str
    tier: int
    credibility: float
    kind: str
    owner: str
    domains: list[str]
    syndication_domains: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    timezone: str | None = None   # IANA zone for feeds that print local times with no zone (e.g. RBI)

    def owns_url(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in self.domains + self.syndication_domains)


class Registry:
    def __init__(self, data: dict):
        self.sources = {s["id"]: Source(**{k: v for k, v in s.items()}) for s in data["sources"]}
        self.blocked = data.get("blocked_domains", [])

    def get(self, source_id: str) -> Source | None:
        return self.sources.get(source_id)

    def is_blocked(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in self.blocked)

    def feeds(self) -> list[tuple[Source, str]]:
        return [(s, f) for s in self.sources.values() for f in s.feeds]


@lru_cache
def registry() -> Registry:
    return Registry(json.loads((CONFIG_DIR / "sources.json").read_text()))


@lru_cache
def settings() -> dict:
    s = json.loads((CONFIG_DIR / "settings.json").read_text())
    total = sum(s["score_weights"].values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"score_weights must sum to 1.0, got {total}")
    return s
