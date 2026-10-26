"""One master lead list — the AllLeads tab.

Every lead this system has ever seen, from every tab, in one sheet:

    Pipeline  — the accumulated discovery queue (name, phone, email, score)
    Score     — rubric totals
    Outreach / Drafts — email + status for anyone that reached the queue
    CRM       — long-term status
    This run  — freshly enriched leads (richest, so they win)

TWO phone numbers are kept per lead, because they are not the same thing:
    public_phone            — the number on the business's Google Maps listing
    owner_or_manager_phone  — a DIFFERENT number printed on the site next to
                              owner/manager/mobile wording (blank when unsure)

NO DUPLICATES. A lead is the same lead when any strong identifier matches:
phone (last 10 digits), email, or normalized name + city. Rows are merged
field-by-field rather than appended, so a repeat run enriches the existing row
instead of adding a twin. The list also ACCUMULATES: it is never rewritten with
only the current run's leads, so history is not lost on a quiet day.

PII stays in the private sheet (spec 11); ``lead_id`` is the sha256 from
src/core/ident.py, same value the approval queue uses.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from src.agents.sheets_agent import ALL_LEADS_HEADER, SheetsAgent
from src.core.ident import lead_id

log = logging.getLogger(__name__)

TAB = "AllLeads"


# ---------- identity ----------
def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _norm_city(value: str) -> str:
    return (value or "").strip().lower()


def _norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def match_keys(row: dict) -> list[str]:
    """Strong identifiers for one row (any match means same business)."""
    keys: list[str] = []
    phone = _norm_phone(str(row.get("public_phone") or ""))
    if len(phone) == 10:
        keys.append(f"p:{phone}")
    email = _norm_email(str(row.get("email") or ""))
    if email:
        keys.append(f"e:{email}")
    name = _norm_name(str(row.get("business_name") or ""))
    city = _norm_city(str(row.get("city") or ""))
    if name and city:
        keys.append(f"n:{name}@{city}")
    return keys


def _num(value) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def merge_into(base: dict, extra: dict) -> dict:
    """Fill blanks in ``base`` from ``extra``; keep the best score and dates."""
    for field in ALL_LEADS_HEADER:
        if field in ("first_seen", "last_seen", "scout_score", "sources"):
            continue
        if not str(base.get(field) or "").strip():
            value = str(extra.get(field) or "").strip()
            if value:
                base[field] = value
    best = max(_num(base.get("scout_score")), _num(extra.get("scout_score")))
    if best:
        base["scout_score"] = int(best) if best == int(best) else best
    if extra.get("scout_tier") and not base.get("scout_tier"):
        base["scout_tier"] = extra["scout_tier"]
    sources = {s for s in str(base.get("sources") or "").split(",") if s}
    sources.update(s for s in str(extra.get("sources") or "").split(",") if s)
    base["sources"] = ",".join(sorted(sources))
    first = [d for d in (base.get("first_seen"), extra.get("first_seen")) if d]
    if first:
        base["first_seen"] = min(str(d) for d in first)
    last = [d for d in (base.get("last_seen"), extra.get("last_seen")) if d]
    if last:
        base["last_seen"] = max(str(d) for d in last)
    return base


def merge_rows(rows: list[dict]) -> list[dict]:
    """Dedupe + merge a list of AllLeads rows (order = precedence).

    Three kinds of identifier, in decreasing strength:
      * phone / email / name+city  — a real business match
      * name only (no city, phone or email — how Outreach/CRM rows arrive)
        merges with another name-only row, or with the ONE city row that has
        that name. If two cities share the name it stays separate, because
        guessing would put Houston's phone on Dallas's row.
    """
    from collections import defaultdict

    out: list[dict] = []
    index: dict[str, int] = {}
    by_name: dict[str, set[int]] = defaultdict(set)
    for raw in rows:
        row = {h: raw.get(h, "") for h in ALL_LEADS_HEADER}
        name = _norm_name(str(row.get("business_name") or ""))
        if not name:
            continue  # a row with no business name identifies nothing
        keys = match_keys(row)
        hit = None
        if keys:
            hit = next((index[k] for k in keys if k in index), None)
        else:
            hit = index.get(f"w:{name}")
            if hit is None:
                candidates = by_name.get(name) or set()
                if len(candidates) == 1:
                    hit = next(iter(candidates))
        if hit is None:
            out.append(row)
            pos = len(out) - 1
        else:
            out[hit] = merge_into(out[hit], row)
            pos = hit
        for key in keys:
            index.setdefault(key, pos)
        if not keys:
            index.setdefault(f"w:{name}", pos)
        by_name[name].add(pos)
    return out


# ---------- source adapters ----------
def _rows_to_dicts(rows: list[list]) -> list[dict]:
    """Header-keyed dicts from a tab's raw rows (stale column order safe)."""
    if not rows or not rows[0]:
        return []
    header = [str(h).strip() for h in rows[0]]
    out: list[dict] = []
    for line in rows[1:]:
        if not line or not any(str(c or "").strip() for c in line):
            continue
        out.append({h: (line[i] if i < len(line) else "")
                    for i, h in enumerate(header)})
    return out


def from_pipeline(row: dict, sources: str = "pipeline") -> dict:
    return {
        "lead_id": lead_id(str(row.get("business_name") or ""),
                           str(row.get("city") or "")),
        "business_name": row.get("business_name", ""),
        "trade": row.get("trade", ""),
        "city": row.get("city", ""),
        "state": row.get("state", ""),
        "public_phone": row.get("phone", ""),
        "owner_or_manager_phone": row.get("owner_or_manager_phone", ""),
        "email": row.get("verified_email", ""),
        "email_status": row.get("email_verification_method", ""),
        "website": row.get("official_site", ""),
        "rating": row.get("rating", ""),
        "review_count": row.get("review_count", ""),
        "scout_score": row.get("scout_score", ""),
        "scout_tier": row.get("scout_tier", ""),
        "sources": sources,
    }


def from_lead(lead: dict, sources: str = "discovery") -> dict:
    return {
        "lead_id": lead_id(str(lead.get("name") or ""),
                           str(lead.get("city") or "")),
        "business_name": lead.get("name", ""),
        "trade": lead.get("vertical", ""),
        "city": lead.get("city", ""),
        "state": lead.get("state", ""),
        "public_phone": lead.get("phone", ""),
        "owner_or_manager_phone": lead.get("owner_phone", ""),
        "email": lead.get("email", "") or "",
        "email_status": lead.get("email_status", ""),
        "website": lead.get("website", "") or "",
        "rating": lead.get("rating", "") or "",
        "review_count": lead.get("reviews", "") or "",
        "scout_score": (lead.get("score") or {}).get("total", "")
        if isinstance(lead.get("score"), dict) else "",
        "scout_tier": (lead.get("score") or {}).get("tier", "")
        if isinstance(lead.get("score"), dict) else "",
        "sources": sources,
    }


def _city_for(name: str, city_map: dict[str, str]) -> str:
    return city_map.get(_norm_name(name), "")


# ---------- the builder ----------
async def build_all_leads(sheets: SheetsAgent, leads: list[dict] | None = None,
                          today: str | None = None) -> list[dict]:
    """Read every lead tab, merge, dedupe, and return AllLeads rows.

    Precedence is deliberate: history first (so nothing is lost), then this
    run's fresh leads (richest contact data), then the derived tabs that only
    add status/score.
    """
    stamp = today or datetime.now(UTC).date().isoformat()

    existing = _rows_to_dicts(await sheets.read_tab(TAB))
    pipeline = _rows_to_dicts(await sheets.read_tab("Pipeline"))
    score = _rows_to_dicts(await sheets.read_tab("Score"))
    outreach = _rows_to_dicts(await sheets.read_tab("Outreach"))
    drafts = _rows_to_dicts(await sheets.read_tab("Drafts"))
    crm = _rows_to_dicts(await sheets.read_tab("CRM"))

    # city lookup so name-only tabs (Score/Outreach/Drafts) can be matched to
    # the right business instead of merging two same-named shops in two cities.
    city_map: dict[str, str] = {}
    for row in pipeline:
        name, city = _norm_name(str(row.get("business_name") or "")), str(row.get("city") or "")
        if name and city:
            city_map.setdefault(name, city)
    for lead in leads or []:
        name = _norm_name(str(lead.get("name") or ""))
        if name and lead.get("city"):
            city_map.setdefault(name, str(lead["city"]))

    rows: list[dict] = [dict(r, first_seen=r.get("first_seen") or stamp,
                             last_seen=r.get("last_seen") or stamp)
                        for r in existing]
    rows += [dict(from_pipeline(r), first_seen=stamp, last_seen=stamp)
             for r in pipeline if str(r.get("business_name") or "").strip()]
    for lead in leads or []:
        if str(lead.get("name") or "").strip():
            rows.append(dict(from_lead(lead), first_seen=stamp, last_seen=stamp))

    for row in crm:
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        rows.append({"business_name": name, "email": row.get("Email", ""),
                     "sales_status": row.get("Status", ""),
                     "city": _city_for(name, city_map),
                     "sources": "crm", "first_seen": stamp, "last_seen": stamp})
    for tab_name, tab_rows in (("outreach", outreach), ("drafts", drafts)):
        for row in tab_rows:
            name = str(row.get("Lead") or "").strip()
            if not name:
                continue
            rows.append({"business_name": name, "email": row.get("Email", ""),
                         "sales_status": row.get("Status", ""),
                         "city": _city_for(name, city_map),
                         "sources": tab_name, "first_seen": stamp,
                         "last_seen": stamp})
    for row in score:
        name = str(row.get("Lead") or "").strip()
        if not name:
            continue
        rows.append({"business_name": name, "scout_score": row.get("Total", ""),
                     "scout_tier": row.get("Tier", ""),
                     "city": _city_for(name, city_map),
                     "sources": "score", "first_seen": stamp, "last_seen": stamp})

    merged = merge_rows(rows)
    merged.sort(key=lambda r: (-_num(r.get("scout_score")),
                               str(r.get("business_name") or "").lower()))
    log.info("all-leads: %d tab rows + %d fresh leads -> %d unique",
             len(existing) + len(pipeline) + len(score) + len(outreach) + len(drafts) + len(crm),
             len(leads or []), len(merged))
    return merged


def to_rows(rows: list[dict]) -> list[list]:
    """AllLeads dicts -> sheet rows in ALL_LEADS_HEADER order."""
    return [[row.get(h, "") for h in ALL_LEADS_HEADER] for row in rows]
