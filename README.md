# Sift — AI news intelligence (MVP)

**Less news. More signal.** Sift reads reports from approved publishers and official sources, merges
duplicate coverage into single stories, throws away what doesn't deserve attention, and shows the rest as
short cards with a summary, why it matters, and links to every original source.

`dist/sift-snapshot.html` is a self-contained build of the app running on a real snapshot:
51 reports collected on 19 Nov 2026 → 19 events → 12 kept.

## Run it

```bash
pip install -r requirements.txt
pytest -q                                  # 32 tests: pipeline, validation, feed, API, delivery, deploy

# Offline demo on the saved snapshot
NEWSINTEL_FIXTURE=fixtures/articles_2026-11-19.json \
NEWSINTEL_REVIEWED=fixtures/summaries_2026-11-19.json \
uvicorn newsintel.api:app --reload         # open http://localhost:8000

# Live: pull the authorised RSS feeds in config/sources.json and summarise with Claude
export ANTHROPIC_API_KEY=...               # server-side only, never sent to the browser
NEWSINTEL_LIVE=1 uvicorn newsintel.api:app # refreshes itself every 5 min (2 min during breaking news)
python -m newsintel.pipeline --live --db newsintel.db --out web/feed.json   # or run the pipeline from cron

python scripts/build_snapshot.py           # rebuild web/index.html and dist/sift-snapshot.html
```

## Architecture

```
config/sources.json   source registry: tier, credibility, owner, beats, feeds   ← add/remove sources here
config/settings.json  score weights, feed mix, thresholds, interest→topic map    ← tune ranking here

newsintel/
  ingestion/      1  News ingestion      RSSAdapter (authorised feeds), FixtureAdapter. Any feed can fail without stopping the run.
  verification/   2  Source verification approved publisher? URL really theirs? dated? news, not brokerage/marketing? on its beat?
  parsing/        3  Article parsing     clean text, content type (report/official/explainer/analysis/opinion/promotional), topics, entities
  clustering/     4  Story clustering    TF-IDF + rare-entity overlap; strong links form cores, weak links merge only with broad coverage
  summarization/  5  AI summarisation    cache → Claude → limited fallback; every output validated (length, clickbait, numbers grounded in sources)
  scoring/        6,7 Relevance + importance, credibility, freshness, practical impact; final score /100
  quality.py         Final quality gate (old, trivial, poorly sourced, clickbait, too thin)
  feed/           8  Feed generation     For you (75/25), What's important, Breaking, Catch me up (3 / 2 / 1)
  users/          9  Preferences         interests, priorities, exclusions, regions; feedback with decay + undo
  notify/        10  Notifications       breaking alerts only, once per story unless it develops, daily caps
  db/                SQLite schema: sources, articles, stories, story_articles, summaries (cache), users, feedback, saved, reports
  api/               FastAPI endpoints; serves web/index.html
web/app.template.html  mobile-first client (vanilla JS, no build step)
```

### Pipeline stages → spec

| Spec stage | Where | What it does |
|---|---|---|
| 1 Source verification | `verification/` | Rejects unlisted publishers, blocked domains, URLs not on the publisher's domain, undated items, brokerage/marketing outlets, promotional copy. Specialist sources lose credibility outside their listed beat. |
| 2 Duplicate detection | `clustering/` | Five publishers → one story. Syndicated copies (same `owner`) never count as independent confirmation. |
| 3 Significance | `scoring/signals.py` | Transparent rules (event type, magnitude, independent coverage, primary source, routine/gossip penalties) blended 60/40 with the model's view. Reasons are shown to the user. |
| 4 Personal relevance | `scoring/relevance.py` | Interest → topic-group overlap using the summariser's event topics, not headline keywords. Priority ×1.25, region +10, followed topic +10, learned affinity ±35% max. |
| 5 Freshness | `scoring/signals.py` | Half-life decay on the latest factual report; labels New / New development / Earlier this week; anything older than 96 h leaves the feed. |
| 6 Quality gate | `quality.py` | Fewer stories rather than filler. |
| Scoring | `scoring/__init__.py` | 30% importance, 25% relevance, 20% credibility, 15% freshness, 10% practical (configurable). |
| Breaking | `pipeline.py` | Importance ≥ 75, credibility ≥ 80, latest report ≤ 6 h, not a limited summary. Developing stories update one card with a timeline. |

### Credibility rules the code enforces
- Opinion and analysis pieces are attached to a story as labelled links and are never used as facts.
- Claims are typed: confirmed, official, reported, allegation, analysis, unclear. Allegations are never stated as fact.
- A summary is rejected if any number in it does not appear in a source (rounded numbers are allowed only after "about", "around", etc.).
- If the model is unavailable or its output fails validation, the card shows the best source's own description, marked **Limited summary**.
- Times are shown only to the precision the source gave (date-only where no time was published).

## Updates, real time and notifications

- **Refresh cadence:** with `NEWSINTEL_LIVE=1` the server runs the pipeline every 5 minutes, and every 2 minutes while any story is breaking (`config/settings.json → refresh`). Only clusters whose article set changed are re-summarised, so frequent runs stay cheap.
- **Live app updates:** open apps subscribe to `/api/stream` (Server-Sent Events). New stories show as an "N new stories · tap to add" pill instead of reshuffling the deck; developing stories update in place with a new timeline entry.
- **Push alerts:** `web/sw.js` + Web Push (pywebpush; VAPID keys are generated by the server on first start, or set `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY`). Works for the installed web app on Android, iOS 16.4+ and desktop; a native app swaps `realtime.PushSender` for FCM/APNs. Alerts go only for breaking stories, once per story unless it develops, capped at 3/day (Major only) or 6/day (All breaking).
- **Faster than 2 minutes** needs licensed wire feeds with push delivery (e.g. Reuters or AP media APIs) instead of RSS polling; plug them in as another adapter in `ingestion/`.

## Daily briefings (user-set times)

Users pick a morning, afternoon and evening time (each can be switched off) plus up to two extra times, and choose how briefings arrive: app notification, email, or both. At each time `realtime.digest_loop` sends one briefing with the top stories for that user since their previous briefing; if nothing new cleared the bar, nothing is sent. Each time goes out at most once a day (`digest_log`).

- **Notifications:** Web Push for the installed web app (no app store needed). On iPhone, users must first tap Share → Add to Home Screen (iOS 16.4+).
- **Email:** any SMTP provider (Amazon SES, Resend, SendGrid, Zoho, Gmail app password). Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`, `PUBLIC_BASE_URL`, `UNSUBSCRIBE_SECRET`. Every email has a one-click unsubscribe link and `List-Unsubscribe` headers. Send from your own domain with SPF/DKIM set up, or briefings will land in spam.
- `dist/sample-briefing-email.html` shows what an emailed briefing looks like.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/bundle` | All live stories + settings (client re-ranks instantly on preference changes) |
| GET | `/api/feed/{user}` | For you, Important, Breaking |
| GET | `/api/catch-up/{user}` | 3 most important, 2 worth knowing, 1 to watch, since last visit |
| GET/PUT | `/api/users/{user}/preferences` | |
| POST/DELETE | `/api/users/{user}/feedback[/{story}]` | Feedback and undo |
| PUT | `/api/users/{user}/saved/{story}` | |
| POST | `/api/story/{id}/report` | |
| GET | `/api/users/{user}/alerts` | Breaking alerts due for this user |
| GET | `/api/ranking/{user}` | Debug: score breakdown for every story |
| GET | `/api/stream` | Server-Sent Events: new_story, development, breaking |
| POST | `/api/users/{user}/push` | Register a Web Push subscription |

## Deploy (Railway, one always-on service + a volume)

`Dockerfile` and `railway.json` are ready. One process runs the web app, the 5-minute refresh loop and the
briefing timer; the SQLite database lives on a volume mounted at `/data`, so it survives restarts and deploys.

1. Push this folder to a GitHub repo. In Railway: **New Project → Deploy from GitHub repo**.
2. On the service: **Settings → Networking → Generate Domain**; **right-click the canvas → Volume**, attach it to the
   service with mount path `/data`.
3. **Variables**: `PUBLIC_BASE_URL` (the generated https address), `ADMIN_TOKEN` (a long random password), and
   optionally `ANTHROPIC_API_KEY` and the `SMTP_*` / `MAIL_FROM` email settings (see `.env.example`).
4. Check `https://<your-app>/api/health`: `last_refresh` should move forward every ~5 minutes on its own,
   and `feeds` shows which publisher feeds worked on the last run.

Push keys (VAPID) and the link-signing secret are generated by the server on first start and kept in the database.
Owner-only checks (send header `X-Admin-Token: <ADMIN_TOKEN>`):
`POST /api/refresh`, `POST /api/admin/test-briefing` (`{"to": "you@…"}`), `POST /api/admin/test-push`
(devices that turned alerts on in the last 30 minutes).

**Install and alerts for users.** Android (Chrome): open the link → menu ⋮ → *Install app* (or *Add to Home screen*),
then Settings → Breaking alerts → allow. iPhone (iOS 16.4+): open the link in Safari → Share → *Add to Home Screen* →
open Sift from the home-screen icon → Settings → Breaking alerts → *Allow*. (iPhone only allows alerts for installed web apps.)

**Email** needs a sending domain you own with SPF and DKIM records (the provider shows the exact DNS records).
Every address must confirm by clicking a link before any briefing is sent to it.

## Before going to production
- **Licensing.** Check each publisher's feed/API terms before commercial use; several (Reuters, AP, FT, Bloomberg) require a licence. The app stores only headlines, short descriptions and links, and always sends readers to the original.
- **Live feeds** were not exercised from this build environment (no outbound access to publishers). The RSS adapter is standard `feedparser` code; run it once against your feed list and drop any feed that returns errors.
- **Snapshot summaries** in `fixtures/summaries_2026-11-19.json` were written from the fixture articles in the same JSON shape the Claude summariser returns, and pass the same validator. Live runs generate them with Claude and cache them by article set.
- **Scale-ups when needed:** Postgres instead of SQLite, sentence embeddings in `clustering/` (the `strong_link`/`weak_link` functions are the only things to swap), a push provider (FCM/APNs) behind `notify/`, auth in front of `/api/users/*`.
