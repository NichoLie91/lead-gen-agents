# lead-gen-agents

A 6-agent AI lead-generation system that finds, scores, enriches, and contacts
small service businesses (plumbers, HVAC, cleaning, mechanics, dental) in 12 US
metros, tracks every lead in a live Google Sheet, and is remotely controlled
from a Telegram bot. Everything runs in **GitHub's cloud (GitHub Actions)** —
no servers to manage.

> **Public repo, PII-safe.** This repository is public by design (unlimited
> Actions minutes for 5-minute bot polling). Lead contact data lives **only** in
> the private Google Sheet. Anything committed to `state/` is non-PII metadata
> plus one-way hashes. Never commit secrets or lead data.

## The 6 agents

| Agent | Role |
|---|---|
| **Atlas** | Lead discovery — Google Maps candidate pool, dedupe, exclusion filters |
| **Scout** | Lead scoring — deterministic 0–100 rubric, tiers (Hot ≥90 / Warm ≥70 / Nurture) |
| **Maps Agent** | Owns every `COMPOSIO_SEARCH_GOOGLE_MAPS` call |
| **Sheets Agent** | Only writer to the Google Sheet (Pipeline / Score / Outreach / Followup tabs) |
| **GitHub Agent** | Commits non-PII state, triggers workflows, guards rate limits |
| **Composio Agent** | Tool gateway (`composio-core` SDK) + web-search enrichment |

## Remote control (Telegram)

| Command | Action |
|---|---|
| `/run` | Trigger a full pipeline run |
| `/status` | Last-run report & metrics |
| `/stop` | Halt a running pipeline (flag checked between stages) |
| `/approve`, `/reject`, `/reject all` | Outreach review decisions |
| `/send all email` | Send emails from approved + Hot leads (50/run cap) |
| `/send all instagram` | Send IG DMs (15/24h cap, Meta cold-start rules) |
| `/sheet` | Google Sheet link |
| `/help`, `/id` | Command list; show your Telegram user ID |

## GitHub rate limiting

All GitHub API usage flows through `src/core/rate_limiter.py`: a conservative
4,000 req/hr ceiling (headroom under the 5,000/hr PAT limit), server-header
awareness (`x-ratelimit-*`), 900 pts/min secondary guard, `Retry-After`-first
backoff, and a persisted budget that survives across ephemeral Actions jobs.

## Requirements

- **Secrets** (GitHub Actions secrets; see `.env.example` for names):
  `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GH_PAT` (fine-grained, Contents
  read/write + Actions + Workflows), `COMPOSIO_API_KEY`, `GOOGLE_SHEET_ID`.
- **Composio connections**: `googlesheets` + `gmail` (ACTIVE for
  nicholas98.thebest@gmail.com), `instagram` (connect a Meta Business account).
- **Gemini**: `gemini-2.5-flash` needs a billing-enabled (Tier 1) project for
  bulk drafting — free tier is ~20 requests/day.

## Local development

```bash
pip install -r requirements.txt

# Full pipeline, fully offline with mock data (no keys required):
python -m src.pipeline --mode full --dry-run

# Single Telegram poll pass (needs TELEGRAM_BOT_TOKEN):
python -m src.bot.telegram_bot

# Tests + lint:
pytest
ruff check src tests
```

## Workflows

- `.github/workflows/bot-poll.yml` — every 5 min: one `getUpdates` pass,
  command dispatch, persisted offset, state commits.
- `.github/workflows/pipeline.yml` — daily at 15:00 UTC (22:00 WIB) + manual
  `workflow_dispatch` with a `mode` input (full / discovery / enrichment /
  outreach-email / outreach-ig / report).
- `.github/workflows/ci.yml` — ruff + pytest + PII-commit check on push/PR.

See `lead-gen-agents-spec.md` (in the project folder) for the full
specification, scoring rubric, and rate-limiter design.
