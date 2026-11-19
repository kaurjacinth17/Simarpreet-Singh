# Sift — project notes for Claude Code

## What this is
Sift is a mobile-first news intelligence app: "Less news. More signal." It reads approved publishers and
official sources, merges duplicate coverage into one story, rejects low-value or unverified items, and shows
short swipeable cards (summary, why it matters, full explainer on flip, all sources). The original product
brief is summarised in README.md; the full spec is the owner's "Build an AI-Powered News Intelligence App" doc.

## Non-negotiable rules
- Never fabricate news, sources, URLs, quotes, numbers or dates. Every story must link to a real source.
- Opinion/analysis is labelled and never used as fact. Allegations are never stated as fact.
- Summaries must pass `summarization.validate` (40-60 words, every number grounded in a source).
- If a summary can't be made confidently, show it as a "Limited summary" or skip the story.
- Fewer, better stories beat a full feed. Don't loosen quality thresholds just to fill the feed.
- Respect publisher terms: store only headlines, short descriptions and links. Never scrape full articles.
- API keys stay server-side (env vars). Nothing secret in web/ or committed to git.

## Layout
- `config/sources.json` source registry (tiers, credibility, feeds); `config/settings.json` weights and thresholds
- `newsintel/` pipeline: ingestion → verification → parsing → clustering → summarization → scoring → quality → feed;
  plus users (prefs/feedback), notify (breaking alerts), digest (scheduled briefings + email), realtime (refresh loop, SSE, push), db, api
- `web/app.template.html` the whole client (vanilla JS); `scripts/build_snapshot.py` builds web/index.html and dist/
- `fixtures/` a real 19 Nov 2026 snapshot (51 articles) and reviewed summaries used by tests
- `tests/` run with `pytest -q` (32 tests). Keep them passing; add tests for new behaviour.

## Commands
- `pip install -r requirements.txt`
- `pytest -q`
- Offline demo: `NEWSINTEL_FIXTURE=fixtures/articles_2026-11-19.json NEWSINTEL_REVIEWED=fixtures/summaries_2026-11-19.json uvicorn newsintel.api:app --reload`
- Live: `NEWSINTEL_LIVE=1 uvicorn newsintel.api:app` (needs ANTHROPIC_API_KEY for summaries)
- Rebuild client: `python scripts/build_snapshot.py`

## Status (as of 19 Nov 2026)
Built and tested on the snapshot: full pipeline, swipe-deck UI, explainer on flip, share (Sift links + preview tags),
Catch Me Up stories, quiz, preferences, breaking alerts, daily briefings (3 named times + 2 extra, push/email/both),
SSE live updates, Web Push and SMTP email senders.

Added 19 Nov 2026: per-device user ids (no shared "local" profile), email double opt-in, server-generated
VAPID keys + link secret (kv table), installable PWA (manifest, icons, offline shell), /api/health with per-feed
results, absolute share-preview tags with og-card.png, owner-only admin endpoints (ADMIN_TOKEN), failed-summary
memory (no re-billing every refresh), Dockerfile + railway.json, pinned requirements.

NOT yet done:
1. Live RSS feeds have never been run from a machine with internet; check /api/health "feeds" after deploying.
   Only 7 sources have feed URLs; HT, ET, Business Standard, SEBI, PIB ministries still to add and verify.
2. Claude summarisation has never run live (no API key yet).
3. Not deployed yet; push and email have only been tested against local stand-ins, not real devices or inboxes.
4. Reuters/AP/FT/Bloomberg need licences.

## Working style
- The owner is not a professional developer: explain choices in plain language, ask before spending money
  or creating accounts, and give exact click-by-click steps for anything they must do themselves.
- Work in small steps, run tests after each change, commit with clear messages.
