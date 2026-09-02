# HANDOFF — Session Transfer for lead-gen-agents

> **READ FIRST: `COLD_EMAIL_PLAYBOOK.md`** (repo root) — it is the permanent
> rulebook. Every rule in it is enforced by `src/email_lint.py` as guardrail #0.
> The next session must read the playbook before touching any outreach code.

**Date of handoff:** 2026-09-03
**Repo:** `lead-gen-agents` (private, runs in GitHub Actions, controlled via Telegram `@leaduntilclient_bot`)
**Local path:** `C:\Users\Nicholas\OneDrive\Documents\Lead to client pipeline\lead-gen-agents`
**Telegram:** `@leaduntilclient_bot` — Gemini (2 keys, pool + failover) classifies every free-text message; Lead Agent (`src/agents/lead_agent.py`) is the master orchestrator delegating to the 6 agents (Atlas/Scout/Enrichment/Outreach/Followups/Inbound).

---

## WHAT THE PREVIOUS SESSION COMPLETED

1. **Researched cold-email frameworks** (Sam McKenna SMYKM "Show Me You Know Me",
   Hormozi outreach rules, Growthflare 4.5M-email findings, 2026 deliverability
   guides) and hardcoded them into `COLD_EMAIL_PLAYBOOK.md`: observation-based
   subjects ≤ 8 words, bodies ≤ 90 words, no I/we opener, max one "?", interest
   CTA only (NEVER "15 minutes" / calendar asks), banned-phrase list
   ("custom AI", "I don't sell a generic chatbot", etc.), CAN-SPAM footer rules,
   deliverability rules (≤ 80 sends/day/mailbox, warmup, SPF/DKIM/DMARC, zero
   links on first touch).
2. **Created `src/email_lint.py`** — hard code enforcement. `lint_email(subject,
   body, criteria)` returns violations; non-empty = BLOCKED. All THREE send
   paths in `src/pipeline.py` run it as guardrail #0 before any Gmail call:
   - `_stage_outreach_email` HOT auto-send (footer → lint → send; blocked rows land
     in Outreach with status `BLOCKED (lint)`, metric `emails_blocked_lint`)
   - `_stage_send_approved` (approved WARM sends; blocked rows marked `BLOCKED (lint)`)
   - `_stage_followups` (blocked follow-ups count as failed, logged warning)
   Call order is fixed: `append_footer()` FIRST, then `lint_email()` on the final
   body — the linter is footer-aware (structure rules apply to the copy only;
   CAN-SPAM gates validate the footer separately). Do not reorder or weaken.
3. **Rewrote `_draft_email()` copy** to a playbook-compliant 4-part template
   (context observation → problem+proof → single interest CTA). The old template
   violated the new rules (2 question marks, ~8 sentences, "I build..." opener)
   and would have been blocked 100% by its own linter. Fixed Day-7 follow-up
   template in `src/followups.py` (removed the banned "15-minute call" ask →
   "Want the breakdown?").
4. **Added `sender.physical_address` to `config/criteria.json`** — INTENTIONALLY
   LEFT EMPTY. The linter blocks ALL sends until Nicholas sets a real street
   address or PO Box. CAN-SPAM liability is $53,088 per email without it.
5. **Created `tests/test_email_lint.py`** — 20 tests, ALL PASSING (self-running:
   `python tests/test_email_lint.py` — no pytest needed). Covers the happy path,
   both CAN-SPAM gates, footer mismatch, subject/body/CTA/banned-phrase rules,
   and footer idempotency.

## KNOWN PRE-EXISTING QUIRKS (out of scope unless Nicholas asks)

- `_outreach_row` writes 9 columns but `OUTREACH_HEADER` has 10 — the `note`
  lands under the "Send Date" column in the Outreach tab. Harmless display bug;
  fix separately if asked.
- `.env` on this machine HAS `COMPOSIO_API_KEY` (Composio v3 REST; Gmail
  connected account `ca_rKt68BMTLcH_` handles send + draft). All 10 GitHub
  Secrets are set and verified via `src.preflight`.
- Quota state at handoff: Gemini both keys 429 (resets midnight UTC), Tavily 432
  (monthly), Hunter.io 429 (25 searches/month exhausted), Google Maps 429 daily
  (resets midnight UTC). Gmail + Sheets + Maps connections ACTIVE in Composio.
  When quotas reset, the pipeline works fully again with no code changes.

## YOUR TASK QUEUE

1. **Ask Nicholas for a physical mailing address** (PO Box is fine — FTC allows
   it) and set `config/criteria.json` → `sender.physical_address`. Do not send
   without it; cite the $53k/email liability. The linter blocks all sends until
   this is set — that is a legal compliance guardrail, not a preference.
2. **Run the test suite** after any change: `python -m pytest tests/ -q` plus
   `python -m ruff check src tests`. The email-lint tests must stay green; if
   you change lint rules, change `tests/test_email_lint.py` in the same commit.
3. **When Nicholas asks for a send**, run one pipeline pass (e.g. dispatch the
   `pipeline.yml` workflow or run the outreach mode) and report
   sent / drafted / blocked(lint) counts from the run report
   (`emails_blocked_lint` metric) plus the Telegram notification.
4. **Spot-check copy quality** with `Pipeline.rate_email(subject, body, facts)`
   (1-10 rating) on any new template before it ships.
5. **Commit and push** so GitHub Actions picks the changes up (workflows run
   from `main`).

## WORKING STYLE WITH NICHOLAS

He moves fast and will ask to skip guardrails (already asked to send without the
postal address). Be direct, give the legal/practical reason, and offer the
fastest COMPLIANT path instead. Don't lecture — one clear reason, then proceed
with everything that is safe. He controls everything through Telegram plain
English; Gemini maps it to agent tasks. When he reports a bug, reproduce locally
with `DRY_RUN=1` first (mock Composio/GitHub/Telegram, no real sends).
