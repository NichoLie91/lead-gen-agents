"""Daily research batch: detective -> writer -> closer.

The four roles, in order, for each business:

1. RESEARCH  the lead's own record (name, city, sector) -- never invented.
2. DETECTIVE open the site and find ONE visible problem that costs money.
             Absence is only claimed after checking the homepage AND the
             same-site contact/booking/review pages, because "no way to book"
             is a lie if the booking page is one click deeper. If the pages
             cannot be read (blocked, empty, JavaScript shell) the lead is
             skipped instead of guessed at.
3. WRITER    a personal email under 100 words: name the problem, one line on the
             fix, one soft question.
4. CLOSER    a day-4 follow-up and a day-10 close.

Nothing here sends email. Output is text for Telegram (plus a CSV attachment)
and rows for the Drafts tab, because the owner sends these by hand.

Batches are ten at a time, one per day, and the next batch waits for an
explicit confirmation (``/next`` in Telegram). Only non-PII state is persisted:
lead hashes, a cursor, retry dates and counters (see state.py's PII policy).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx

from src.core.ident import lead_id

BATCH_SIZE = 10
STATE_FILE = "research_batch"

# Homepage + up to three deeper pages. Absence claims need the deeper pages;
# more than four requests per business is not worth the wall clock.
MAX_PAGES = 4
# A skipped lead (blocked / unreadable / plain broken) gets another attempt in a
# week, twice, then it stops coming back and stops occupying a batch slot.
RETRY_DAYS = 7
MAX_RETRIES = 3

# Why a lead in a batch produced no email. Persisted as a tally so /batch can
# show whether the Pipeline is full of dead websites or just clean ones.
SKIP_NO_WEBSITE = "no_website"
SKIP_NO_EMAIL = "no_email"
SKIP_UNREACHABLE = "unreachable"
SKIP_UNREADABLE = "unreadable"
SKIP_CLEAN = "clean"
SKIP_DUPLICATE = "duplicate"

SKIP_LABELS = {
    SKIP_NO_WEBSITE: "no website on record",
    SKIP_NO_EMAIL: "no verified email",
    SKIP_UNREACHABLE: "site did not answer",
    SKIP_UNREADABLE: "page blocked or unreadable",
    SKIP_CLEAN: "no visible defect",
    SKIP_DUPLICATE: "already in an earlier batch",
}

# Permanent: retrying cannot change the answer, so the lead is done for good.
_PERMANENT_SKIPS = (SKIP_CLEAN, SKIP_DUPLICATE, SKIP_NO_WEBSITE, SKIP_NO_EMAIL)

# Fetched pages shorter than this, or carrying one of these markers, are treated
# as unreadable rather than as a site with no content. Guessing here would break
# the "never invent a problem" rule in the worst way: an email describing a
# defect on a page we never actually saw.
_MIN_READABLE = 500
_JS_SHELL_MARKERS = (
    "enable javascript",
    "just a moment",
    "checking your browser",
    "attention required",
    "access denied",
)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_HREF_RE = re.compile(r"""(?is)\bhref\s*=\s*["']([^"']+)["']""")
_FORM_BLOCK_RE = re.compile(r"(?is)<form\b.*?</form>")
# A real contact form asks for something you can write back to, or is a quote
# request. A site-search box (<input name="q">) does not count as a way in.
_CONTACT_FIELD_RE = re.compile(
    r"""(?is)<(?:input|textarea)\b[^>]*\bname\s*=\s*["'][^"']*"""
    r"(?:email|e-mail|mail|message|msg|comment|enquir|inquir|phone|telephone|tel|name)"
    r"""["']"""
)
_DATE_FIELD_RE = re.compile(r"""(?is)<input\b[^>]*\btype\s*=\s*["'](?:date|datetime-local)["']""")
_VIEWPORT_RE = re.compile(r"""(?i)<meta\b[^>]*\bname\s*=\s*["']viewport["']""")
_TEL_RE = re.compile(r"""(?i)\bhref\s*=\s*["']tel:""")
_MAILTO_RE = re.compile(r"""(?i)\bhref\s*=\s*["']mailto:""")
_BOOKING_PATH_RE = re.compile(
    r"""(?i)(?:^|/)(?:book|booking|book-now|schedule|scheduling|appointment|"""
    r"""appointments|reserve|reservation|request-service)(?:[-/.?#]|$)"""
)
# Embeds and links to the booking tools trades actually use.
_BOOKING_HOSTS = (
    "calendly.com", "housecallpro.com", "servicetitan.com", "getjobber.com",
    "jobber.com", "booksy.com", "getsquire.com", "squire.com", "setmore.com",
    "acuityscheduling.com", "square.site", "vagaro.com", "mindbodyonline.com",
    "housecall", "schedulicity.com", "workiz.com", "fieldedge.com",
)
# Hosted form builders: the form is an iframe, so "<form" is absent by design.
_FORM_HOSTS = (
    "jotform.com", "typeform.com", "google.com/forms", "docs.google.com/forms",
    "formstack.com", "wufoo.com", "123formbuilder.com", "forms.gle",
    "gravityforms", "cognitoforms.com", "paperform.co", "tally.so",
)

# Word-boundary regexes, NOT substring tests: "facebook" contains "book" and
# every trade site links a Facebook page, so a substring test for "book" made
# the highest-value finding (no way to book) almost impossible to ever report.
_WORD_BOOKING = re.compile(
    r"(?i)\b(book|books|booking|bookings|schedule|scheduling|appointment|"
    r"appointments|reserve|reservation)\b"
)
_WORD_REVIEW = re.compile(
    r"(?i)\b(reviews?|testimonials?|rated|rating|ratings|stars?)\b"
)

# Pages worth a second and third look before claiming something is missing.
_CRAWL_HINTS = (
    "book", "schedul", "appoint", "reserv", "contact", "quote",
    "review", "service", "pricing", "price", "about",
)


@dataclass(frozen=True)
class Problem:
    """One visible, checkable defect.

    ``label`` is the table phrase ("no way to book online"); ``followup`` is the
    work itself as a noun phrase ("the booking link"), so the day-4 and day-10
    closers read as sentences instead of stapled labels. ``looked_for`` is what
    the detective actually checked, shown to the owner as evidence.
    """

    code: str
    label: str
    followup: str
    looked_for: str


# Order = severity. The first defect the page shows is the one we write about.
PROBLEMS: dict[str, Problem] = {
    "no_viewport": Problem(
        "no_viewport",
        "not built for a phone screen",
        "the phone layout",
        "no viewport meta tag in the page head",
    ),
    "no_booking": Problem(
        "no_booking",
        "no way to book online",
        "the booking link",
        "no booking link, calendar embed or date field",
    ),
    "no_click_to_call": Problem(
        "no_click_to_call",
        "the phone number is not tap-to-call",
        "the tap-to-call number",
        "no tap-to-call link on any page checked",
    ),
    "no_reviews": Problem(
        "no_reviews",
        "no reviews shown on the page",
        "the reviews block",
        "no review or rating text on any page checked",
    ),
    "no_form": Problem(
        "no_form",
        "no contact form and no email, only a phone number",
        "the contact form",
        "no contact form, hosted form embed or email address",
    ),
}


def _text_of(html: str) -> str:
    """Visible-ish text: drop scripts/styles/tags. Enough for keyword checks."""
    stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", stripped)).lower()


def is_readable(html: str) -> bool:
    if len(html.strip()) < _MIN_READABLE:
        return False
    low = html.lower()
    return not any(marker in low for marker in _JS_SHELL_MARKERS)


def _hrefs(html: str) -> list[str]:
    return [h.strip() for h in _HREF_RE.findall(html) if h.strip()]


def _has_booking(pages: list[str], text: str) -> bool:
    """Is there any way to book, anywhere in the pages we opened?"""
    for html in pages:
        if _DATE_FIELD_RE.search(html):
            return True
        for href in _hrefs(html):
            low = href.lower()
            if "facebook" in low or "twitter" in low or "instagram" in low:
                # Social links are not booking links, whatever their path.
                continue
            if any(host in low for host in _BOOKING_HOSTS):
                return True
            if _BOOKING_PATH_RE.search(urlparse(low).path):
                return True
        if any(host in html.lower() for host in _BOOKING_HOSTS):
            return True
    return bool(_WORD_BOOKING.search(text))


def _has_contact_form(pages: list[str], text: str) -> bool:
    for html in pages:
        low = html.lower()
        if _MAILTO_RE.search(html):
            return True
        if any(host in low for host in _FORM_HOSTS):
            return True
        for block in _FORM_BLOCK_RE.findall(html):
            if _CONTACT_FIELD_RE.search(block) or _DATE_FIELD_RE.search(block):
                return True
    return False


def detect_problem(pages: list[str] | str) -> Problem | None:
    """The single most expensive visible defect, or None.

    Takes every page we actually opened: an absence claim is only made against
    the union, so a booking page behind the nav link beats "no way to book".
    Pure and network-free so the rules can be tested against fixture HTML.
    """
    if isinstance(pages, str):
        pages = [pages]
    if not pages:
        return None
    home = pages[0]
    low_all = "\n".join(pages).lower()
    text = " ".join(_text_of(p) for p in pages)

    if not _VIEWPORT_RE.search(home):
        return PROBLEMS["no_viewport"]
    if not _has_booking(pages, text):
        return PROBLEMS["no_booking"]
    if not _TEL_RE.search(low_all):
        return PROBLEMS["no_click_to_call"]
    if not _WORD_REVIEW.search(text):
        return PROBLEMS["no_reviews"]
    if not _has_contact_form(pages, text):
        return PROBLEMS["no_form"]
    return None


def _crawl_targets(base_url: str, html: str) -> list[str]:
    """Same-site pages worth opening before claiming something is absent.

    Document order, so the choice is deterministic between runs.
    """
    host = urlparse(base_url).netloc.lower()
    out: list[str] = []
    for href in _hrefs(html):
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != host:
            continue
        if not parsed.path or parsed.path in ("/", ""):
            continue
        if re.search(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4)$",
                     parsed.path, re.IGNORECASE):
            continue
        if not any(hint in (parsed.path + parsed.query).lower() for hint in _CRAWL_HINTS):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean not in out:
            out.append(clean)
    return out[:MAX_PAGES - 1]


def _path_of(url: str) -> str:
    path = urlparse(url).path or "/"
    return path if path.startswith("/") else f"/{path}"


async def _fetch(client: httpx.AsyncClient, url: str) -> tuple[str | None, str]:
    """(html, "") on success, (None, reason) otherwise. Never raises."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return None, f"could not fetch ({type(exc).__name__})"
    if resp.status_code >= 400:
        return None, f"could not fetch (HTTP {resp.status_code})"
    return resp.text or "", ""


async def inspect_site(url: str, timeout: float = 12.0) -> tuple[Problem | None, str]:
    """Fetch one site and return (problem, evidence).

    ``(None, reason)`` when nothing could be judged -- unreachable, blocked,
    unreadable, or clean. A lead with no problem gets no email.
    """
    url = (url or "").strip()
    if not url:
        return None, f"{SKIP_NO_WEBSITE}: no website on record"
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": _UA},
        ) as client:
            home, err = await _fetch(client, url)
            if err or home is None:
                # Not a finding. A failed fetch can be our own bad URL, our
                # network, or bot blocking -- claiming "your site is down" from
                # here would be inventing a problem, the one thing this must
                # never do.
                return None, f"{SKIP_UNREACHABLE}: {err}"
            if not is_readable(home):
                return None, (f"{SKIP_UNREADABLE}: page not readable "
                              "(blocked, empty or JavaScript-only)")

            pages = [home]
            paths = [_path_of(url)]
            opened = {url}
            for target in _crawl_targets(url, home):
                if target in opened:
                    continue
                opened.add(target)
                sub, sub_err = await _fetch(client, target)
                if sub_err or sub is None or not is_readable(sub):
                    continue
                pages.append(sub)
                paths.append(_path_of(target))
    except httpx.HTTPError as exc:  # client construction / teardown
        return None, f"{SKIP_UNREACHABLE}: {type(exc).__name__}"

    problem = detect_problem(pages)
    checked = ", ".join(paths)
    if problem is None:
        return None, f"{SKIP_CLEAN}: no defect in the checked list ({checked})"
    # Evidence the owner can verify in one glance before sending.
    return problem, f"checked {checked}: {problem.looked_for}"


# ---------------------------------------------------------------- writer
# Three phrasings per defect, all template-only: no model writes these, so no
# sentence can describe something the detective did not see. A batch walks the
# variants in order so ten plumbers never receive the same observation.
_OBSERVATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "no_booking": (
        (
            ("There is no way to book on the site for {shop}, so anyone ready "
            "to pay has to call during business hours."),
            ("A booking link that confirms by text within a minute would catch "
            "the ones who call after five."),
        ),
        (
            ("Nothing on the site for {shop} lets someone book, which leaves the "
            "phone as the only way in."),
            ("One booking button in the header would take that step off your "
            "evening."),
        ),
        (
            ("The site for {shop} asks people to call instead of letting them "
            "pick a time."),
            ("A booking page behind a single button would hold the work that "
            "comes in after hours."),
        ),
    ),
    "no_click_to_call": (
        (
            ("The number on the site for {shop} is not tap-to-call, so anyone on "
            "a phone has to copy it by hand."),
            ("Making it a click-to-call link takes ten minutes and removes that "
            "step entirely."),
        ),
        (
            ("The phone number for {shop} sits as plain text, so a tap on a "
            "phone does nothing."),
            "Wrapping it in a call link makes the number work with one tap.",
        ),
        (
            ("On a phone the number for {shop} has to be copied before it can be "
            "dialled."),
            ("A tap-to-call link on every page would fix that in one line of "
            "code."),
        ),
    ),
    "no_viewport": (
        (
            ("The site for {shop} does not fit a phone screen, so the menu and "
            "the number run off the edge."),
            "One viewport tag in the header fixes that on every phone.",
        ),
        (
            ("On a phone the {shop} site shows the desktop layout, so reading it "
            "means pinching and scrolling sideways."),
            "Adding a viewport tag lets the page size itself to the screen.",
        ),
        (
            ("The {shop} site loads at desktop width on a phone, which is where "
            "most of these clicks come from."),
            "A viewport tag in the page head is the whole fix.",
        ),
    ),
    "no_reviews": (
        (
            "No reviews appear on the site for {shop}{google}.",
            "Two real Google reviews on the page would do more than a redesign.",
        ),
        (
            "Nothing on the {shop} site shows what past customers said{google}.",
            ("A short reviews block near the top would carry more weight than "
            "the rest of the page."),
        ),
        (
            "The site for {shop} asks for trust without showing any reviews{google}.",
            "Three quoted reviews with first names would cover that gap.",
        ),
    ),
    "no_form": (
        (
            ("The page for {shop} has no contact form and no email, so the only "
            "way in is a phone call."),
            "A three-field form would catch the people who will not call.",
        ),
        (
            ("There is no way to write to {shop} from the site, only a number to "
            "ring."),
            ("A short form with name, phone and message would pick up the ones "
            "who read at night."),
        ),
        (
            ("The {shop} site gives a phone number but no form and no email "
            "address."),
            "Adding a form to the contact page takes about an hour.",
        ),
    ),
}

_SUBJECTS: dict[str, tuple[str, ...]] = {
    "no_viewport": ("your site on a phone", "the site on a phone",
                    "phone layout on your site"),
    "no_booking": ("booking on your site", "the booking gap on your site",
                   "your site after 5pm"),
    "no_click_to_call": ("the number on your site", "tapping the number on your site",
                         "one tap on your number"),
    "no_reviews": ("reviews on your site", "the reviews gap",
                   "what customers said"),
    "no_form": ("getting in touch on your site", "the contact page",
                "writing to you from the site"),
}


def variant_count(problem: Problem) -> int:
    """How many distinct phrasings exist for this defect."""
    return len(_OBSERVATIONS.get(problem.code, ()))


def _google_clause(rating: str, count: str) -> str:
    """", though Google shows 4.9 from 80 reviews" -- only when the sheet says so.

    The old copy asserted the business was rated on Google unconditionally,
    which was a fact we did not always have.
    """
    rating = str(rating or "").strip()
    if not rating:
        return ""
    try:
        float(rating)
    except ValueError:
        return ""
    count = str(count or "").strip()
    try:
        n = int(float(count))
    except ValueError:
        return f", though Google shows a {rating} rating"
    if n <= 0:
        return f", though Google shows a {rating} rating"
    noun = "review" if n == 1 else "reviews"
    return f", though Google shows {rating} from {n} {noun}"


def write_email(lead: dict, problem: Problem, variant: int = 0) -> tuple[str, str]:
    """Personal first-touch email: problem, fix, one soft question.

    Under 100 words by construction; every sentence refers to something the
    detective actually saw. No greeting block, no links, no hype.
    """
    shop = str(lead.get("name") or "").strip() or "your business"
    google = ""
    if problem.code == "no_reviews":
        google = _google_clause(lead.get("rating", ""), lead.get("review_count", ""))

    pool = _OBSERVATIONS[problem.code]
    observation, fix = pool[variant % len(pool)]
    body = f"{observation.format(shop=shop, google=google)} {fix} Worth a look?"
    subjects = _SUBJECTS[problem.code]
    return subjects[variant % len(subjects)], body


def write_closers(name: str, problem: Problem) -> tuple[str, str]:
    """Day 4 bump and day 10 close. Short, one question, no pressure."""
    day4 = (
        f"Bumping this once. If {problem.followup} is not on the list this "
        "month, that is a fine answer. Is it timing, or not worth the effort?"
    )
    day10 = (
        f"Last note from me on this. {problem.followup.capitalize()} is still "
        f"worth doing on the site for {name}, and that is your call to make. If "
        "it comes up later, reply here and I will pick it back up."
    )
    return day4, day10


# ---------------------------------------------------------------- table
def _fit(text: str, width: int) -> str:
    """Pad or truncate to an exact width so the columns line up in monospace."""
    text = str(text or "")
    if len(text) > width:
        text = f"{text[:width - 1]}~"
    return text.ljust(width)


def format_table(items: list[dict], html: bool = True) -> str:
    """Plain-text table for Telegram: what was seen, per business.

    Columns are width-capped so a long business name cannot push the table off
    a phone screen; the untruncated copy is in the attached CSV.
    """
    if not items:
        return "No businesses inspected in this batch."
    widths = (3, 26, 12, 30, 24)
    rows = [("#", "Business", "City", "Problem seen", "Subject")]
    for i, it in enumerate(items, start=1):
        rows.append((
            str(i), it["name"], it.get("city", ""),
            it["problem"].label, it["subject"],
        ))
    lines = ["  ".join(_fit(c, w) for c, w in zip(row, widths)) for row in rows]
    table = "\n".join(lines)
    if not html:
        return table
    import html as _html

    return f"<pre>{_html.escape(table)}</pre>"


def format_drafts(items: list[dict]) -> list[str]:
    """Drafts split into Telegram-sized chunks (4096 is the hard cap)."""
    blocks: list[str] = []
    for i, it in enumerate(items, start=1):
        day4, day10 = it["closers"]
        # The evidence string already starts with "checked /, /contact", so the
        # label must not repeat the word.
        checked = re.sub(r"^checked\s+", "", it.get("evidence") or "")
        checked = checked or "not recorded"
        blocks.append(
            f"{i}. {it['name']} ({it.get('city', '')})\n"
            f"Seen: {it['problem'].label}\n"
            f"Checked: {checked}\n"
            f"Subject: {it['subject']}\n{it['body']}\n"
            f"Day 4: {day4}\n"
            f"Day 10: {day10}"
        )
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > 3500:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}".strip()
    if current:
        chunks.append(current)
    return chunks


def format_csv(items: list[dict]) -> str:
    """The batch as a CSV, so the owner copy-pastes from a spreadsheet."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["#", "business", "city", "sector", "email", "website",
                     "problem", "evidence", "subject", "body", "day4", "day10"])
    for i, it in enumerate(items, start=1):
        day4, day10 = it["closers"]
        writer.writerow([
            i, it["name"], it.get("city", ""), it.get("vertical", ""),
            it.get("email", ""), it.get("website", ""), it["problem"].label,
            it.get("evidence", ""), it["subject"], it["body"], day4, day10,
        ])
    return buf.getvalue()


# ---------------------------------------------------------------- batch gate
def lead_hash(lead: dict) -> str:
    """Stable PII-safe id for a lead.

    ``address`` when the record has one, else ``city``: the Pipeline tab does not
    carry a street address, and the id only has to be stable, not semantic.
    """
    where = lead.get("address") or lead.get("city") or ""
    return lead_id(str(lead.get("name") or ""), str(where))


def new_state() -> dict:
    return {
        "cursor": 0,
        "awaiting_confirm": False,
        "done": [],
        "retry": {},
        "retries": {},
        "batches": 0,
        "last_batch_date": "",
        # Bumped by /next so an explicit request can bypass the daily guard and
        # the awaiting-confirmation gate without weakening either.
        "force_next": False,
        "last_skips": {},
    }


def eligible(lead: dict) -> bool:
    """Needs a site to inspect and an address to write to. Nothing else."""
    return bool(str(lead.get("website") or "").strip()) and bool(
        str(lead.get("email") or "").strip()
    )


def select_batch(leads: list[dict], state: dict, size: int = BATCH_SIZE,
                 today: str = "") -> list[dict]:
    """Next ``size`` leads for this batch.

    Skips leads already drafted, leads whose retry date has not arrived, and
    duplicates of a lead already picked in this same batch (the Pipeline tab
    carries the same business more than once in practice).
    """
    done = set(state.get("done") or [])
    retry = state.get("retry") or {}
    today = today or datetime.now(UTC).date().isoformat()
    seen: set[str] = set()
    out: list[dict] = []
    for lead in leads:
        lid = lead.get("id")
        if not lid or lid in done or lid in seen:
            continue
        due = retry.get(lid)
        if due and due > today:
            continue
        seen.add(lid)
        out.append(lead)
        if len(out) >= size:
            break
    return out


def record_batch(state: dict, items: list[dict], skipped: list[dict] | None = None,
                 today: str = "") -> dict:
    """Mark the batch delivered and gate the next one behind a confirmation.

    Inspected-but-skipped leads are recorded too. Leaving them out of ``done``
    is what jammed the flow: ten clean websites sat at the front of the pool,
    so every later run re-fetched them, drafted nothing, and never reached lead
    eleven.
    """
    today = today or datetime.now(UTC).date().isoformat()
    done = list(state.get("done") or [])
    retry = dict(state.get("retry") or {})
    retries = dict(state.get("retries") or {})

    done.extend(it["id"] for it in items)
    for item in items:
        retries.pop(item["id"], None)
        retry.pop(item["id"], None)

    tally: dict[str, int] = {}
    for skip in skipped or []:
        lid = skip.get("id")
        reason = skip.get("reason") or SKIP_UNREACHABLE
        tally[reason] = tally.get(reason, 0) + 1
        if not lid:
            continue
        if reason in _PERMANENT_SKIPS:
            done.append(lid)
            continue
        attempts = retries.get(lid, 0) + 1
        retries[lid] = attempts
        if attempts >= MAX_RETRIES:
            done.append(lid)  # answered the same way twice; stop burning slots
            retry.pop(lid, None)
        else:
            retry[lid] = (date.fromisoformat(today) + timedelta(days=RETRY_DAYS)).isoformat()

    # dict.fromkeys keeps first-seen order and stops the list growing with dupes.
    done = list(dict.fromkeys(done))
    retry = {k: v for k, v in retry.items() if k not in done}

    return {
        **state,
        "cursor": state.get("cursor", 0) + len(items),
        "awaiting_confirm": bool(items),
        "done": done,
        "retry": retry,
        "retries": retries,
        "batches": state.get("batches", 0) + 1,
        "last_batch_date": today,
        "force_next": False,
        "last_skips": dict(sorted(tally.items())),
    }


def classify_skip(evidence: str) -> str:
    """The reason tag from an evidence string ("unreachable: could not fetch")."""
    tag = str(evidence or "").split(":", 1)[0].strip().lower()
    return tag if tag in SKIP_LABELS else SKIP_UNREACHABLE
