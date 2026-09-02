# COLD EMAIL PLAYBOOK — READ FIRST

This file is the **permanent rulebook** for every email this system drafts or sends.
Every new session MUST read this before touching outreach code. Do not weaken these
rules. Do not bypass the linter (`src/email_lint.py`). If a rule conflicts with "send
more emails", the rule wins.

---

## 1. Why these rules exist

Researched and merged from (2025-2026 consensus, tested against real sends):

| Source | What it contributed |
|---|---|
| Sam McKenna — **SMYKM** ("Show Me You Know Me") | First line proves research about THEM, never about us |
| Alex Hormozi — outreach rules | Boring subjects get opened; specificity = credibility; ask for reply, not a meeting |
| Growthflare 4.5M-email analysis | Long emails die; reply-first CTAs outperform calendar asks |
| 2026 deliverability guides (Google/Yahoo bulk-sender rules) | One link max, zero links on first touch, SPF/DKIM/DMARC, warm up mailboxes, ≤80 sends/day/mailbox |

## 2. Hard limits (enforced by `src/email_lint.py` — BLOCKING)

| Rule | Limit | Rationale |
|---|---|---|
| Subject length | ≤ 8 words, observation-based | "4.8 stars and the after-hours phone" beats "Quick question!!!" |
| Body length | ≤ 90 words | Reply rates fall off a cliff above ~90 words |
| Sentences | ≤ 5 | One idea per line; busy owners skim |
| No "I/we" opener | First sentence may not start with I/we | SMYKM: it is about them, not us |
| Question marks | max ONE "?" | Two asks = zero asks |
| CTA | interest CTA only | "Worth a look?" / "Open to it?" — NEVER "15 minutes", "book a call", calendar links |
| Links | ZERO links on first touch | First-touch links are the #1 spam-filter trigger |
| Attachments | NEVER on first touch | Same reason as links |
| Banned phrases | see list below | AI tells + spam triggers kill deliverability |
| Physical address | REQUIRED in footer | CAN-SPAM: $53,088 per violating email — see §4 |
| Unsubscribe | one line in every footer | CAN-SPAM requires a clear opt-out; honor within 10 days (we honor same-run) |
| Honest subject | no clickbait, no ALL CAPS, no Re:/Fwd: tricks | FTC deceptive-subject rule |

## 3. Banned phrases (never appear in subject or body)

AI tells: `delve`, `testament`, `beacon`, `tapestry`, `furthermore`, `moreover`,
`plethora`, `synergy`, `in today's world`, `it is important to note`,
`at the end of the day`, `I hope this email finds you well`, `I came across`,
`I don't sell a generic chatbot`, `custom AI`, `revolutionary`, `game-changer`,
`cutting-edge`, `seamless`, `leverage` (as a verb).

Spam triggers: `free`, `guarantee`, `risk-free`, `buy now`, `special promotion`,
`increase revenue`, `urgent`, `save money`, `click here`, `best price`,
`act now`, `limited time`, `100%`, `no obligation`, `winner`, `cash`,
`discount`, `apply now`, `call now`, `don't delete`, `double your`.

## 4. CAN-SPAM footer (every email, no exceptions)

```
{business_name}
You're receiving this one-time note. Reply "stop" to opt out — honored immediately.
{sender.physical_address}
```

**`sender.physical_address` lives in `config/criteria.json` and is INTENTIONALLY
EMPTY until Nicholas provides a real street address or PO Box. The linter blocks ALL
sends while it is empty. Do NOT remove or bypass this check.** The FTC fine is
$53,088 PER EMAIL. A PO Box is fully compliant — "give me the fastest compliant
path" means "get a PO Box today", never "skip the footer".

CAN-SPAM quick facts (FTC, 2026):
- Physical postal address required in every commercial email (street or PO Box).
- Opt-out must be honored promptly (we do it same-run via the inbound agent).
- What we send is a one-to-one business note, but the footer rules still apply.
- Violations: up to $53,088 per email. Not worth it. Ever.

## 5. Deliverability rules

- ≤ 80 sends/day per mailbox (Gmail connected account via Composio `ca_rKt68BMTLcH_`).
  The pipeline's own cap (50/run) is already below this — do not raise it.
- New mailbox: warm up 2+ weeks (5-10/day, +5/day) before pipeline sends.
- SPF + DKIM + DMARC must pass on the sending domain. Verify once per domain.
- Zero links on first touch. Links only after a reply.
- Send windows: Tue-Thu, 8-11am local recipient time beat Monday/Friday blasts.
- Bounce halt: if bounce rate crosses 8% in a run, STOP sends (guardrail in
  system prompt). Bounces poison domain reputation.

## 6. The SMYKM body template (category hooks in `pipeline.py`)

```
[OBSERVATION — one specific, true fact about their business from the lead row]
[PROBLEM — one sentence on the real cost, in their world]
[PROOF/OFFER — one sentence, specific, no hype]
[INTEREST CTA — one question, reply-first]
```

- Every fact must come from the lead row (rating, reviews, city, website status).
  Never invent details. The `_lead_facts()` helper is the only allowed source.
- Vary sentence structure across leads (spintax) so sends don't look templated.
- Vertical hooks: plumbing = missed emergency calls; HVAC = after-hours overflow;
  dental = new-patient call drop-off. See `HOOKS` in `pipeline.py`.

## 7. Linter contract (what code MUST do)

`src/email_lint.py` exposes:

```python
from src.email_lint import lint_email
violations = lint_email(subject, body, criteria)
# violations: list[str] — non-empty = BLOCKED, do not send
```

- `outreach-email` and `send-approved` modes run the lint BEFORE any Gmail call
  (guardrail #0). A violation never sends; it is logged and counted as `blocked`.
- Empty `sender.physical_address` blocks 100% of sends.
- Tests: `tests/test_email_lint.py` — keep them green. If you change lint rules,
  change the tests in the same commit.

## 8. Change policy

1. Rules here may be TIGHTENED at any time.
2. Rules may only be LOOSENED with a written legal/practical reason in this file.
3. The linter is the enforcer — it must always match this playbook. If they
   disagree, this file wins and the linter gets fixed the same day.
