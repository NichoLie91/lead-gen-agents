"""Telegram command parsing + formatting (spec section 5). Pure functions —
no I/O — so they are unit-testable."""
from __future__ import annotations

COMMAND_HELP = {
    "/help": "Show available commands",
    "/status": "Latest execution report & metrics (how the last run did)",
    "/run": "Trigger an immediate pipeline run: /run, or /run <mode> (full | discovery | enrichment | outreach-email | outreach-ig | followups | inbound | report)",
    "/list drafts": "List WARM drafts waiting for your approval",
    "/approve": "Approve drafts: /approve all, or /approve <id> from /list drafts",
    "/reject": "Reject drafts: /reject all, or /reject <id>",
    "/reject all": "Reject every draft currently waiting for approval",
    "/inbound": "Scan email for lead replies now (classify + escalate)",
    "/followups": "Send due Day 3/7/14 follow-ups now",
    "/stop": "Halt the running execution",
    "/sheet": "Google Sheet link from the last batch",
    "/send all email": "Send all emails from the approved leads",
    "/send all instagram": "Send all leads an Instagram DM",
    "/id": "Show your Telegram user ID (for the admin allow-list)",
}

KNOWN_COMMANDS = {c.lower(): c for c in COMMAND_HELP}


def parse_command(text: str | None) -> tuple[str, str]:
    """Return (canonical_command, args) from a message text.

    Telegram sends commands with a leading slash, e.g. "/send all email".
    Multi-word commands are matched longest-first.
    """
    if not text:
        return "", ""
    lowered = " ".join(text.strip().split())
    # longest match first so "/reject all" beats "/reject"
    for candidate in sorted(KNOWN_COMMANDS, key=len, reverse=True):
        if lowered == candidate or lowered.startswith(candidate + " "):
            rest = lowered[len(candidate):].strip()
            return KNOWN_COMMANDS[candidate], rest
    return "", ""


def is_allowed(user_id: int, admin_ids: list[int]) -> bool:
    """Allow-list rule (spec 5.2): empty allow-list == open bot."""
    return not admin_ids or user_id in admin_ids


def build_help() -> str:
    lines = ["Available commands:"]
    for cmd, desc in COMMAND_HELP.items():
        lines.append(f"{cmd} — {desc}")
    lines.append("")
    lines.append("💬 You can also just type what you want in plain English — "
                 "e.g. \"run the pipeline\", \"send the follow-ups\", "
                 "\"approve all drafts\", \"what happened last run?\".")
    return "\n".join(lines)


def format_status(report: dict | None, sheet_url: str, running: bool) -> str:
    if running:
        return f"Pipeline is RUNNING right now.\n\n{sheet_url and f'Sheet: {sheet_url}' or ''}"
    if not report:
        return "No runs yet. Send /run to start the pipeline."
    metrics = report.get("metrics", {})
    lines = [
        (
            f"Last run [{report.get('run_id', '?')}] mode={report.get('mode', '?')} "
            f"status={report.get('status', '?')}"
        ),
        f"Started: {report.get('started_at', '?')}",
    ]
    if report.get("error"):
        lines.append(f"Error: {report['error']}")
    if metrics:
        lines.append(f"Candidates: {metrics.get('candidates', 0)}")
        tiers = metrics.get("tiers") or {}
        if tiers:
            lines.append("Tiers: " + ", ".join(f"{k}={v}" for k, v in tiers.items()))
        lines.append(
            f"Emails: sent={metrics.get('emails_sent', 0)} "
            f"drafted={metrics.get('emails_drafted', 0)} "
            f"skipped={metrics.get('emails_skipped', 0)}"
        )
        lines.append(
            f"IG: sent={metrics.get('ig_sent', 0)} skipped={metrics.get('ig_skipped', 0)}"
        )
        lines.append(f"Needs enrichment: {metrics.get('needs_enrichment', 0)}")
        if metrics.get("sheet_tabs_failed"):
            lines.append(f"⚠️ Sheet write FAILED for {metrics['sheet_tabs_failed']} tab(s) "
                         f"({metrics.get('sheet_tabs_written', 0)} ok)")
    if sheet_url:
        lines.append(f"Sheet: {sheet_url}")
    return "\n".join(lines)
