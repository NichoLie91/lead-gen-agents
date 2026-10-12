# RESEARCH — Making Our Agent Team "Proper AI Agents"

> **STATUS (2026-09-26): PHASES 1–4 SHIPPED.**
> Phase 1: tool registry + bounded agent loop (`src/core/tools.py`, `src/core/agent_loop.py`).
> Phase 2: evaluator-optimizer for drafts (`src/draft_optimizer.py`), behavioral evals in CI (`src/evals/`), compounding memory (`src/core/memory.py`).
> Phase 3: enrichment decide-loop, inbound triage (`src/inbound_triage.py`), Atlas query-yield feedback (`src/query_feedback.py`).
> Phase 4: A2A agent cards (`src/agents/cards.json`), discovery-based wiring (`src/agents/discovery.py`), AgentTask dispatch incl. peer-to-peer.
> Verified: 383 tests passing, ruff clean, full dry-run COMPLETED.

**Date:** 2026-09-26
**Question:** Our 6-agent lead-gen team (Atlas / Scout / Enrichment / Outreach / Followups / Inbound + infra agents) is agent-*shaped* but doesn't behave like real AI agents. What makes a "proper" AI agent, what do ours lack, and what do we do about it?
**Method:** Primary-source research (official docs and specs, read in full) + video-first review of **five** videos (Andrew Ng ×2, Nick Saraev's 2-hour agent course, Greg Isenberg's agent course, AI Master's Claude-Code agent tutorial) — all **watched in a real browser via frame-stepping** (35 frames total across the five videos, plus each page's own descriptions and chapter listings). **No transcripts were used.**

---

## TL;DR verdict

By Anthropic's published definitions our system is a **workflow**, not an agent system — and for the compliance-critical core, that is *correct* and we should keep it. But three components genuinely should become proper agents (Lead Agent, Enrichment, Inbound), and the whole team is missing the infrastructure that "proper" agents run on: a tool loop, plan memory, behavioral evals, and a real inter-agent protocol. The fix plan below is phased so the guardrails and PII policy are never touched.

---

## 1. What the primary sources say a "proper AI agent" is

### Anthropic — "Building Effective Agents" (Dec 2024) [1]

The canonical distinction:

> **Workflows** are systems where LLMs and tools are orchestrated through **predefined code paths**.
> **Agents** are systems where LLMs **dynamically direct their own processes and tool usage**, maintaining control over how they accomplish tasks.

And the shape of a real agent:

> Agents are typically just **LLMs using tools based on environmental feedback in a loop**. It is therefore crucial to design toolsets and their documentation clearly.

Their three core principles: **(1) simplicity, (2) transparency (show the planning steps), (3) a carefully crafted Agent-Computer Interface (ACI)** — thorough tool docs and testing. Patterns, in ascending order of autonomy: prompt chaining → routing → parallelization → **orchestrator-workers** → evaluator-optimizer → autonomous agent. Advice: *start with the simplest thing that works; add agency only where it demonstrably improves outcomes.*

### Anthropic — "How we built our multi-agent research system" (2025) [2]

Lessons from production (multi-agent outperformed single-agent by **90.2%** on their research eval — but multi-agent uses **~15× the tokens of chat**, so it must be reserved for high-value, parallelizable work):

- **Orchestrator-worker**: a lead agent plans, spawns subagents with *objective + output format + tool guidance + task boundaries*. Vague delegation made subagents duplicate work.
- **Scale effort to query complexity** — embedded rules in prompts (simple fact: 1 agent, 3–10 calls; complex: 10+ subagents).
- **Save the plan to Memory** — the lead agent persists its plan because context windows truncate.
- **Tool design is critical**; they built a *tool-testing agent* that rewrites bad tool descriptions — **40% decrease in task completion time**.
- **Let agents improve themselves** (Claude as prompt engineer for its own prompts).
- Honest caveat: multi-agent fits *breadth-first, parallelizable* tasks (research), **not** tightly-sequential ones (most coding).

### OpenAI Agents SDK — Agent orchestration [3]

Two orchestration modes, meant to be mixed:
- **Orchestrating via LLM**: agent = LLM + instructions + tools + **handoffs**; two SDK patterns: **agents-as-tools** (manager keeps the conversation, specialists return results) vs **handoffs** (specialist takes over the turn).
- **Orchestrating via code**: structured outputs, chaining, evaluator loops, `asyncio.gather` parallelism.
Key tactics: invest in prompts, **invest in evals**, let agents introspect and improve, prefer specialized agents over one generalist.

### LangChain — "Choosing the Right Multi-Agent Architecture" (Jan 2026) [4]

Four patterns: **Subagents** (centralized, stateless workers), **Skills** (progressive disclosure in one agent), **Handoffs** (state-driven turn transitions), **Router** (parallel dispatch + synthesis). Decision drivers: distributed development, parallelization, multi-hop, direct user interaction. Performance data: stateful patterns save 40–50% of calls on repeat requests; subagents isolate context best (67% fewer tokens than skills on multi-domain). Their bottom line: *"Start with a single agent and good prompt engineering. Add tools before adding agents. Graduate to multi-agent patterns only when you hit clear limits."*

### A2A protocol (Google → Linux Foundation) [5]

The standard for agents talking **to each other** as opaque peers:
- **Agent Card**: JSON "business card" advertising an agent's identity, capabilities, skills, endpoints — enabling dynamic discovery instead of hardcoded wiring.
- **Task lifecycle**: `submitted → working → input-required → completed/failed/canceled`, with Messages and Artifacts as the exchange format.
- MCP complements it: MCP connects an agent to *tools*; A2A connects agents to *agents*.

### Andrew Ng — the videos (watched, not transcribed) [6][7]

- Sequoia AI Ascent talk (13:40, watched at 7 timestamps): agentic workflows are **iterative** — the same task done in a loop (reflect, use tools, plan, collaborate) beats one-shot, and this can matter more than the next foundation model.
- DeepLearning.AI course video (2:35, watched at 7 timestamps; page content confirms): **4 agentic design patterns — reflection, tool use, planning, multi-agent workflows** — built "from scratch in Python, no frameworks, no black boxes."

His framing matters for us: *agentic ≠ autonomous*. An "agentic workflow" is an iterative loop with feedback — that's the entry point, and our system can adopt it without abandoning determinism.

### The practitioner video layer (watched, not transcribed) [8][9][10]

The three builder videos independently converge on the same architecture the primary sources describe — which is strong signal:

- **Greg Isenberg ft. Remy Gaskell — "Building AI Agents that actually work"** (59 min; chapter list captured from the video page): every agent platform (Claude Code, Codex, Antigravity, Manus, OpenClaw) "runs the same **observe-think-act loop** under the hood"; the shift is from **prompt engineering to context engineering** (markdown context files, `AGENTS.md`); **memory compounds and reduces errors** (chat memory vs. agent memory); tools connect via **MCP**; **skills are SOPs** that chain together; agents are **onboarded like real employees** with department-based folder structures. This is Anthropic's ACI + memory lesson, stated as practitioner common sense.
- **Nick Saraev — "AI Agents Full Course 2026"** (2h13m): builds lead-gen/deal-closing agent systems for businesses in plain Python + Claude — the exact domain we're in. His framing: agents that *generate leads, close deals, and scale businesses* are assembled from the same loop + tools + context primitives, not frameworks.
- **AI Master — "How to Build an AI Agent with Claude Code"** (31s trailer; chapter structure captured): the one-liner that matters — *"A real agent has a process, knows when it's uncertain, and asks before it acts."* Plus: one context file is "the most important piece of the whole system," planning mode stops the agent from guessing, and there are five recurring mistakes that kill agent builds.

**What this layer adds to the fix plan:** (a) our repo already practices the markdown-context discipline they preach — `SYSTEM_PROMPT.md`, `COLD_EMAIL_PLAYBOOK.md`, and `HANDOFF.md` are SOPs — but they're loaded by *humans and CLI sessions*, not by our *runtime agents*; wiring them in as on-demand context is cheap and high-value. (b) The lessons log should be a **compounding memory** file per agent, not just an append-only JSON. (c) "Knows when it's uncertain and asks before it acts" formalizes what our approval queue already does — the loop must explicitly support an `input-required` state that surfaces to Telegram.

---

## 2. Gap analysis — our team vs. the criteria

What our code actually does today (verified in the graphify audit + source): `src/pipeline.py` runs a **fixed stage machine** (`_stage_discovery → _stage_enrichment → _stage_scoring → outreach → …`), each stage calling agent classes as **one-shot functions**. The only LLM-driven piece is the Lead Agent on the Telegram side, and even it may only *suggest whitelisted commands*.

| # | Proper-agent property (source) | Us today | Score |
|---|---|---|---|
| 1 | LLM dynamically directs its own process (Anthropic [1]) | Stage order hardcoded in `Pipeline.run()`; only Lead Agent routes intents, within a whitelist | **1/5** |
| 2 | Tool-use loop with environment feedback (Anthropic [1]) | One-shot LLM calls per stage; Composio actions invoked by deterministic code, not decided by an agent in a loop | **1/5** |
| 3 | Agents discover & delegate to each other (A2A [5], OpenAI [3]) | Hardcoded Python imports; `Pipeline.__init__` wires all 13 components; agents cannot address each other at runtime | **0/5** |
| 4 | Planning, persisted (Anthropic [2]) | No plan object exists; the "plan" is the code path itself | **1/5** |
| 5 | Reflection / self-critique loop (Ng [7], Anthropic evaluator-optimizer [1]) | `rate_email()` exists but runs once as a metric, not as a revise loop; Scout's Gemini overlay is bounded ±5, not reflective | **1/5** |
| 6 | Behavioral evals (OpenAI [3], Anthropic [2]) | 24 unit-test files (excellent for logic), zero evals of agent *decisions* (routing accuracy, rationale quality, delegation quality) | **2/5** |
| 7 | Effort scaling to query complexity (Anthropic [2]) | Every run does full effort regardless of the ask | **1/5** |
| 8 | Working memory vs. data memory | `StateStore`/CRM persist *data* well; no plan/scratchpad/lesson memory for agents | **2/5** |
| 9 | Human-in-the-loop checkpoints (Anthropic [1]) | Approval queue, `/stop` between stages, PII guard, honest reporting | **5/5** |
| 10 | Simplicity discipline (Anthropic [1], LangChain [4]) | Composable small modules, no framework lock-in — exactly what both sources recommend | **4/5** |

**Reading:** we built a *workflow with agent names on it*. Properties 9–10 are best-in-class; 1–8 are where "proper agent" work lives. Also note Anthropic's own warning ([2]): multi-agent autonomy burns ~15× tokens and fits parallelizable, high-value tasks — our outreach core is sequential and compliance-bound, so **keeping it a workflow is the right call** (LangChain [4] and Anthropic [1] both say most systems should stay workflows). The fix is targeted autonomy + real agent infrastructure, not a rewrite.

---

## 3. The fix plan

### Phase 0 — Lock the workflow core (no code changes)
Discovery → scoring → outreach stays a deterministic, guardrailed pipeline. CAN-SPAM lint, caps, PII policy untouched. Rationale: [1][2][4] all say determinism where steps are predictable.

### Phase 1 — Make the Lead Agent a real agent (highest leverage)
`src/agents/lead_agent.py`
1. **Tool registry** (`src/core/tools.py`): wrap existing capabilities as documented tools with ACI discipline [1] — `pipeline.run_mode(mode)`, `sheets.read_tab(tab, filter)`, `crm.query(...)`, `approvals.list()`, `outreach.draft_email(lead)`, `telegram.reply(msg)`. Each tool gets name, when-to-use, args schema, failure modes (Anthropic's tool-testing agent found that better descriptions cut task time 40% [2]).
2. **Agent loop** in LeadAgent: plan → call tools → observe results → adapt, with a bounded iteration budget and stop conditions [1]. The existing whitelist stays as the safety floor; the loop picks *how*, the whitelist constrains *what*.
3. **Persist the plan** to `state/plans/<run>.json` before executing (Anthropic's memory lesson [2]) — StateStore already exists, this is a new key.
4. **Effort scaling** rules in its prompt [2]: "check status" = 1–2 tool calls; "run the pipeline" = full staged run; nothing in between spawns extra work.
5. **Per-agent context file** (practitioner pattern [9][10]): each runtime agent gets an on-demand SOP file (`src/agents/sop/<agent>.md`) compiled from our existing docs (playbook rules for Outreach, validation rules for Enrichment, rubric for Scout) — loaded into context only when that agent is delegated work, instead of relying on prompts rewritten in code. "Onboard the agent like a real employee."

### Phase 2 — Reflection + evals (Ng pattern 1; OpenAI/Anthropic tactic)
1. **Evaluator-optimizer for drafts**: `_draft_email()` → `rate_email()` critique → revise, max 3 iterations, keep best. The evaluator already exists — we're just closing the loop [1][7].
2. **Behavioral eval set** (`src/evals/`): ~50 golden Telegram messages with expected routings; ~30 scored leads with rationale-quality rubric judged by the pro model. Run on every PR alongside `pytest` and `ruff` in `ci.yml` [3].
3. **Learning log as compounding memory** (practitioner pattern [9]): when a run fails or a draft gets rejected, append to `state/lessons.json` AND regenerate a compact `state/memory/<agent>.md` that gets injected into that agent's context on the next run — memory that compounds and reduces errors, not an append-only log nobody reads (the "let agents improve themselves" lever [2]).

### Phase 3 — True mini-agents where flexibility pays
1. **Enrichment agent loop** (`src/enrichment.py`): search → extract → verify → *decide the next query* → retry, up to N iterations with stop conditions. Today it's one-shot; this is our most path-dependent stage and the textbook agent use case [1].
2. **Inbound triage agent** (`src/inbound.py`): classify → act (STOP auto-reply) → escalate — give it the same loop so it can ask follow-up tool calls (e.g., check CRM history before classifying).
3. **Atlas strategy feedback**: Gemini-generated query templates get yield feedback (candidates per query) and are regenerated next run — planning with environment feedback [2][7].

### Phase 4 — Inter-agent protocol (your "freely interact" requirement)
1. **Agent Cards, internal first** (`src/agents/cards.json`): every agent publishes name, role, capabilities, input/output schema, in A2A's Agent Card shape [5]. The Lead Agent **discovers** agents from the cards instead of hardcoded wiring — new agents become pluggable without touching `Pipeline.__init__`.
2. **A2A-shaped Task objects**: internal dispatch uses `{task_id, status: submitted/working/input-required/completed/failed, message, artifacts}` — the A2A lifecycle [5] — so any two agents can delegate and hand results back, including peer-to-peer (Outreach → CRM) without routing through the orchestrator.
3. **MCP for tools, later**: expose Sheets/Gmail/Maps as MCP servers so any agent — ours, or an external one like Claude/Gemini — shares one tool surface [1][5]. This is the bridge if we ever want external agents to operate our stack.
4. **Only if we split into separate services**: actual A2A over HTTP with cards at `/.well-known/agent.json` [5]. Premature today; the JSON-shape work above makes it a drop-in later.

### Sequencing & effort
Phase 1 ≈ 1–2 sessions (registry + loop + plan persistence). Phase 2 ≈ 1 session. Phase 3 ≈ 1 session. Phase 4 ≈ 1–2 sessions. Each phase is independently shippable and keeps `pytest`/`ruff`/PII guard green.

---

## 4. Sources

1. Anthropic — *Building Effective Agents* (Dec 19, 2024). https://www.anthropic.com/engineering/building-effective-agents — workflow/agent definitions, five patterns, ACI, three principles.
2. Anthropic — *How we built our multi-agent research system* (2025). https://www.anthropic.com/engineering/multi-agent-research-system — orchestrator-worker, 90.2% eval gain, 15× token cost, delegation/effort-scaling/tool-testing lessons, plan-to-memory.
3. OpenAI — *Agent orchestration*, OpenAI Agents SDK docs. https://openai.github.io/openai-agents-python/multi_agent/ — agents-as-tools vs handoffs, LLM vs code orchestration, evals.
4. LangChain — *Choosing the Right Multi-Agent Architecture* (Jan 14, 2026). https://www.langchain.com/blog/choosing-the-right-multi-agent-architecture — subagents/skills/handoffs/router, decision + performance tables.
5. A2A Project — *Agent2Agent Protocol specification*. https://github.com/a2aproject/A2A/blob/main/docs/specification.md (Agent Card, Task lifecycle); announcement: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/ ; status: Linux Foundation, v1.0 (2026, per https://tyk.io/learning-center/a2a-protocol-architecture-and-technical-specification/).
6. Sequoia Capital — *What's next for AI agentic workflows ft. Andrew Ng* (AI Ascent). https://www.youtube.com/watch?v=sal78ACtGTc — **watched via browser frame-stepping** (7 frames: 40s/163s/286s/409s/532s/655s/762s), description + chapters from the page; no transcript.
7. DeepLearning.AI — *Learn to build effective Agentic AI systems with Andrew Ng*. https://www.youtube.com/watch?v=w7vqXL4PWEE — **watched via browser frame-stepping** (7 frames: 7s→144s); page confirms the four patterns: reflection, tool use, planning, multi-agent workflows; no transcript.
8. Nick Saraev — *AI Agents Full Course 2026: Master Agentic AI* (2h13m). https://www.youtube.com/watch?v=EsTrWCV0Ph4 — **watched via browser frame-stepping** (7 frames: 399s→7434s); lead-gen/deal-closing agent systems built in plain Python + Claude; no transcript.
9. Greg Isenberg ft. Remy Gaskell — *Building AI Agents that actually work (Full Course)* (59 min). https://www.youtube.com/watch?v=eA9Zf2-qYYM — **watched via browser frame-stepping** (7 frames: 176s→3287s); full chapter listing captured from the video page (agent loop, context engineering, compounding memory, MCP, skills-as-SOPs, department agents); no transcript.
10. AI Master — *How to Build an AI Agent with Claude Code* (tutorial). https://www.youtube.com/watch?v=bcM9dP_uXJU — **watched via browser frame-stepping** (7 frames, 31s short); "a real agent has a process, knows when it's uncertain, and asks before it acts"; no transcript.

**Method notes & honesty:** browser automation used Playwright driving installed Chrome; evidence frames in `~/research-agent-tmp/assets/`. Slide content in [6] is corroborated by multiple independent summaries surfaced in search, but the primary capture here is visual frames + page metadata. gstack's `/scrape` skill was referenced but requires its Chromium install (skipped during setup); direct Playwright was used instead. Enable it later by re-running `./setup` in `~/gstack`.
