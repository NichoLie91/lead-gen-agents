"""Daily research batch: detective -> writer -> closer.

The rules being protected:
- the detective reports only defects that are actually absent, and only after
  checking the deeper pages that could have satisfied the claim;
- an unreadable page produces NO email rather than a guessed problem;
- copy stays under 100 words, one question, no links, no hype, and never states
  a Google rating the sheet does not carry;
- inspected-but-skipped leads leave the pool, so the batch never jams;
- one batch a day, and the next one waits for /next.
"""
import asyncio
from types import SimpleNamespace

from src import site_research as r
from src.bot.commands import parse_command
from src.core.state import StateStore
from src.pipeline import Pipeline


def _page(*, viewport=True, booking=True, tel=True, reviews=True, form=True,
          body="Plumbing and drain service in Tampa.") -> str:
    """A realistic small-business page with switches for each defect."""
    head = "<html><head><title>Shop</title>"
    if viewport:
        head += '<meta name="viewport" content="width=device-width">'
    head += "</head><body>"
    text = body
    if booking:
        text += ' Book an appointment online. <a href="/book-online">Book</a>'
    if reviews:
        text += " Rated 4.9 stars by 80 customers. Read our reviews."
    # Every trade site has these; a substring test for "book" matches
    # "facebook", which is exactly how the booking finding went silent.
    social = ('<a href="https://www.facebook.com/shop">Facebook</a>'
              '<a href="https://www.instagram.com/shop">Instagram</a>')
    links = '<a href="tel:+18135551212">Call</a>' if tel else "<span>Call</span>"
    links += '<a href="/contact">Contact</a>'
    form_html = '<form action="/contact"><input name="email"></form>' if form else ""
    # Real pages are kilobytes; the readability guard ignores tiny documents.
    filler = ("<p>Family owned since 1998, serving Tampa and Hillsborough County. "
              "Licensed and insured. Emergency service available.</p>") * 3
    return head + f"<h1>Shop</h1>{text}{links}{form_html}{social}{filler}</body></html>"


# ---------- detective

def test_clean_page_reports_nothing():
    assert r.detect_problem([_page()]) is None


def test_missing_viewport_is_reported_first():
    assert r.detect_problem([_page(viewport=False)]).code == "no_viewport"


def test_missing_booking_beats_other_defects():
    assert r.detect_problem([_page(booking=False, form=False)]).code == "no_booking"


def test_a_facebook_link_is_not_a_booking_link():
    """Regression: "book" is a substring of "facebook", so the substring test
    made the highest-value finding almost impossible to report."""
    page = _page(booking=False)
    assert "facebook" in page.lower() and "book an appointment" not in page.lower()
    assert r.detect_problem([page]).code == "no_booking"


def test_a_booking_page_one_click_deeper_satisfies_the_claim():
    home = _page(booking=False)
    deeper = _page()
    assert r.detect_problem([home]).code == "no_booking"
    assert r.detect_problem([home, deeper]) is None


def test_calendly_embed_counts_as_booking():
    page = _page(booking=False).replace(
        "<h1>Shop</h1>",
        '<h1>Shop</h1><iframe src="https://calendly.com/shop/estimate"></iframe>',
    )
    assert r.detect_problem([page]) is None


def test_a_booking_link_path_counts_even_without_the_word():
    page = _page(booking=False).replace(
        "<h1>Shop</h1>", '<h1>Shop</h1><a href="/schedule-a-visit">Go</a>'
    )
    assert r.detect_problem([page]) is None


def test_missing_tap_to_call():
    assert r.detect_problem([_page(tel=False)]).code == "no_click_to_call"


def test_missing_reviews():
    assert r.detect_problem([_page(reviews=False)]).code == "no_reviews"


def test_missing_contact_form():
    assert r.detect_problem([_page(form=False)]).code == "no_form"


def test_a_site_search_box_is_not_a_contact_form():
    page = _page(form=False).replace(
        "<h1>Shop</h1>", '<h1>Shop</h1><form action="/search"><input name="q"></form>'
    )
    assert r.detect_problem([page]).code == "no_form"


def test_a_hosted_form_embed_counts_as_a_contact_form():
    page = _page(form=False).replace(
        "<h1>Shop</h1>",
        '<h1>Shop</h1><iframe src="https://form.jotform.com/1234"></iframe>',
    )
    assert r.detect_problem([page]) is None


def test_an_email_address_counts_as_a_way_in():
    page = _page(form=False).replace(
        "<h1>Shop</h1>", '<h1>Shop</h1><a href="mailto:hi@shop.test">Email</a>'
    )
    assert r.detect_problem([page]) is None


def test_no_prices_is_no_longer_a_finding():
    """Dropped: trades rarely publish prices, so it was true of nearly every
    lead and therefore worthless as personalisation."""
    assert "no_prices" not in r.PROBLEMS
    page = _page().replace("$99", "")
    assert r.detect_problem([page]) is None


def test_unreadable_pages_are_not_judged():
    assert r.is_readable("<html>short</html>") is False
    assert r.is_readable("<html>" + "x" * 600 + "Just a moment...</html>") is False
    assert r.is_readable(_page()) is True


# ---------- fetching (evidence + crawl)

class _Response:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


def _client(monkeypatch, pages: dict[str, _Response]):
    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url not in pages:
                raise AssertionError(f"unexpected fetch {url}")
            return pages[url]

    monkeypatch.setattr(r.httpx, "AsyncClient", Client)


def test_a_failed_fetch_is_never_reported_as_a_finding(monkeypatch):
    """A 503 can be our bad URL, our network, or bot blocking. Claiming the
    owner's site is down from one failed fetch would invent a problem."""
    _client(monkeypatch, {"https://example.test": _Response(503, "")})
    problem, evidence = asyncio.run(r.inspect_site("https://example.test"))
    assert problem is None
    assert evidence.startswith(r.SKIP_UNREACHABLE)
    assert "503" in evidence
    assert r.classify_skip(evidence) == r.SKIP_UNREACHABLE


def test_blocked_page_returns_no_problem(monkeypatch):
    _client(monkeypatch, {
        "https://example.test": _Response(200, "<html><body>Just a moment...</body></html>"),
    })
    problem, evidence = asyncio.run(r.inspect_site("https://example.test"))
    assert problem is None
    assert evidence.startswith(r.SKIP_UNREADABLE)
    assert "not readable" in evidence


def test_the_detective_opens_the_contact_page(monkeypatch):
    home = _page(booking=False, tel=False)
    deeper = _page()
    _client(monkeypatch, {
        "https://example.test": _Response(200, home),
        "https://example.test/contact": _Response(200, deeper),
    })
    problem, evidence = asyncio.run(r.inspect_site("https://example.test"))
    # The contact page had booking, tap-to-call, reviews and a form: nothing to
    # claim, and the evidence names both pages that were actually read.
    assert problem is None
    assert "/contact" in evidence


def test_findings_carry_the_evidence_the_owner_can_check(monkeypatch):
    home = _page().replace(
        "<h1>Shop</h1>", '<h1>Shop</h1><a href="/contact">Contact</a>'
    ).replace('<a href="tel:+18135551212">Call</a>', "<span>Call</span>")
    _client(monkeypatch, {
        "https://example.test": _Response(200, home),
        "https://example.test/contact": _Response(404, ""),
        "https://example.test/book-online": _Response(404, ""),
    })
    problem, evidence = asyncio.run(r.inspect_site("https://example.test"))
    assert problem.code == "no_click_to_call"
    assert evidence.startswith("checked /")
    assert "tap-to-call" in evidence
    # The 404 page never contributed to the claim.
    assert "/contact" not in evidence


# ---------- writer

def test_every_email_is_short_and_asks_once():
    for problem in r.PROBLEMS.values():
        for variant in range(r.variant_count(problem)):
            subject, body = r.write_email(
                {"name": "Blue Creek Plumbing", "city": "Tampa"}, problem, variant)
            assert subject and len(subject.split()) <= 8
            assert len(body.split()) < 100, (
                f"{problem.code}/{variant} is {len(body.split())} words")
            assert body.count("?") == 1
            assert not body.startswith(("I ", "We "))
            assert "http" not in body and "www." not in body
            # The repo's own cold-email rules, not just a word count.
            assert Pipeline._lint_email_rules(body) == [], f"{problem.code}/{variant}"


def test_every_defect_has_several_phrasings():
    for problem in r.PROBLEMS.values():
        assert r.variant_count(problem) >= 3, problem.code


def test_variants_actually_differ():
    lead = {"name": "Blue Creek Plumbing", "city": "Tampa"}
    problem = r.PROBLEMS["no_booking"]
    bodies = {r.write_email(lead, problem, v)[1] for v in range(3)}
    assert len(bodies) == 3
    assert all("Blue Creek Plumbing" in b for b in bodies)


def test_a_variant_wraps_instead_of_breaking():
    lead = {"name": "Blue Creek Plumbing"}
    problem = r.PROBLEMS["no_form"]
    first = r.write_email(lead, problem, 0)
    assert r.write_email(lead, problem, 3) == first


def test_email_names_the_problem_it_saw():
    subject, body = r.write_email(
        {"name": "Blue Creek Plumbing"}, r.PROBLEMS["no_booking"])
    assert "no way to book" in body.lower()
    assert "booking" in subject.lower()


def test_the_google_rating_is_only_claimed_when_the_sheet_has_it():
    problem = r.PROBLEMS["no_reviews"]
    without = r.write_email({"name": "Shop"}, problem)[1]
    assert "though Google shows" not in without

    with_rating = r.write_email(
        {"name": "Shop", "rating": "4.9", "review_count": "80"}, problem)[1]
    assert "4.9" in with_rating and "80 reviews" in with_rating
    assert Pipeline._lint_email_rules(with_rating) == []

    count_only = r.write_email({"name": "Shop", "rating": "4.9"}, problem)[1]
    assert "a 4.9 rating" in count_only
    assert Pipeline._lint_email_rules(count_only) == []


def test_a_nonsense_rating_is_ignored():
    problem = r.PROBLEMS["no_reviews"]
    body = r.write_email({"name": "Shop", "rating": "N/A"}, problem)[1]
    assert "though Google shows" not in body


def test_closers_are_day_four_and_day_ten_style():
    day4, day10 = r.write_closers("Blue Creek Plumbing", r.PROBLEMS["no_booking"])
    assert day4.count("?") == 1
    assert "this month" in day4
    assert "?" not in day10
    assert "Blue Creek Plumbing" in day10


# ---------- table, drafts, csv

def _item(i: int, name: str = "", code: str = "no_booking") -> dict:
    problem = r.PROBLEMS[code]
    shop = name or f"Shop {i}"
    subject, body = r.write_email({"name": shop, "city": "Tampa"}, problem)
    return {
        "id": f"id{i}", "name": shop, "city": "Tampa", "vertical": "plumber",
        "email": f"shop{i}@example.test", "website": "https://example.test",
        "problem": problem, "evidence": "checked /: no booking link",
        "subject": subject, "body": body,
        "closers": r.write_closers(shop, problem),
    }


def test_table_lists_every_business_in_fixed_width():
    table = r.format_table([_item(1, "Alpha"), _item(2, "Bravo")], html=False)
    assert "Alpha" in table and "Bravo" in table
    assert "no way to book online" in table
    widths = {len(line) for line in table.splitlines()}
    assert len(widths) == 1, "columns must line up in monospace"


def test_table_is_wrapped_for_telegram():
    assert r.format_table([_item(1)]).startswith("<pre>")
    assert r.format_table([_item(1)]).endswith("</pre>")


def test_a_long_business_name_cannot_break_the_table():
    table = r.format_table([_item(1, "A" * 90)], html=False)
    widths = {len(line) for line in table.splitlines()}
    assert len(widths) == 1


def test_drafts_show_what_was_checked_and_are_split_for_telegram():
    chunks = r.format_drafts([_item(i) for i in range(10)])
    assert len(chunks) >= 2
    assert all(len(c) <= 4096 for c in chunks)
    assert "Checked: /: no booking link" in chunks[0]
    assert "checked checked" not in chunks[0]
    # Plain text: the batch table is the only HTML message.
    assert "<pre>" not in "".join(chunks)


def test_csv_has_a_row_per_business_and_escapes_commas():
    items = [_item(1, 'Shop, "The" Best'), _item(2)]
    out = r.format_csv(items)
    lines = out.strip().splitlines()
    assert lines[0].startswith("#,business,city")
    assert len(lines) == 3
    assert '"Shop, ""The"" Best"' in out


# ---------- batch gate

def test_batch_is_ten_and_skips_finished():
    leads = [{"id": f"id{i}", "name": f"S{i}"} for i in range(25)]
    state = {"done": ["id0", "id1"], "cursor": 0, "awaiting_confirm": False,
             "batches": 1}
    batch = r.select_batch(leads, state)
    assert len(batch) == r.BATCH_SIZE
    assert batch[0]["id"] == "id2"


def test_select_batch_respects_a_retry_date_and_dedupes():
    today = "2026-10-25"
    leads = [
        {"id": "a", "name": "A"}, {"id": "a", "name": "A"},   # same business twice
        {"id": "b", "name": "B"},
    ]
    state = {"done": [], "retry": {"b": "2026-11-01"}, "retries": {}}
    picked = r.select_batch(leads, state, today=today)
    assert [p["id"] for p in picked] == ["a"]
    # Once the retry date arrives it is eligible again.
    later = r.select_batch(leads, state, today="2026-11-02")
    assert [p["id"] for p in later] == ["a", "b"]


def test_record_batch_gates_the_next_one():
    state = r.new_state()
    state = r.record_batch(state, [_item(1), _item(2)], today="2026-10-25")
    assert state["awaiting_confirm"] is True
    assert state["done"] == ["id1", "id2"]
    assert state["batches"] == 1
    assert state["last_batch_date"] == "2026-10-25"


def test_skipped_leads_leave_the_pool_instead_of_jamming_it():
    """The bug this protects: only drafted ids were marked done, so ten clean
    websites sat at the front of the pool and every later run re-fetched them,
    drafted nothing, and never reached lead eleven."""
    clean = [{"id": f"clean{i}", "name": f"C{i}"} for i in range(3)]
    fresh = [{"id": f"new{i}", "name": f"N{i}"} for i in range(2)]
    state = r.new_state()
    state = r.record_batch(
        state, [],
        [{"id": c["id"], "reason": r.SKIP_CLEAN} for c in clean],
        today="2026-10-25")
    assert state["awaiting_confirm"] is False
    assert set(state["done"]) == {"clean0", "clean1", "clean2"}
    assert state["last_skips"] == {r.SKIP_CLEAN: 3}
    picked = r.select_batch(clean + fresh, state, today="2026-10-26")
    assert [p["id"] for p in picked] == ["new0", "new1"]


def test_a_blocked_site_is_retried_twice_then_dropped():
    state = r.new_state()
    for day, expected in (("2026-10-25", "2026-11-01"), ("2026-11-02", "2026-11-09")):
        state = r.record_batch(
            state, [], [{"id": "x", "reason": r.SKIP_UNREACHABLE}], today=day)
        assert state["retry"]["x"] == expected
        assert "x" not in state["done"]
    state = r.record_batch(
        state, [], [{"id": "x", "reason": r.SKIP_UNREACHABLE}], today="2026-11-10")
    assert "x" in state["done"]
    assert "x" not in state["retry"]


def test_record_batch_clears_the_force_flag():
    state = {**r.new_state(), "force_next": True}
    assert r.record_batch(state, [_item(1)], today="2026-10-25")["force_next"] is False


def test_eligible_needs_a_site_and_an_address():
    assert r.eligible({"website": "x.com", "email": "a@b.co"}) is True
    assert r.eligible({"website": "", "email": "a@b.co"}) is False
    assert r.eligible({"website": "x.com", "email": ""}) is False


def test_the_batch_state_is_committed_in_the_cloud():
    """Every Actions job starts from a fresh checkout, so state that is not in
    the GitHub agent's whitelist silently resets. Leaving research_batch out
    meant the /next gate, the once-a-day guard and the done list were all lost,
    and the same ten businesses would be drafted again on the next run."""
    from src.agents.github_agent import STATE_FILES

    assert r.STATE_FILE in STATE_FILES


def test_classify_skip_defaults_to_unreachable():
    assert r.classify_skip("clean: no defect (/, /contact)") == r.SKIP_CLEAN
    assert r.classify_skip("nonsense") == r.SKIP_UNREACHABLE


# ---------- pipeline stage

class _Notifier:
    def __init__(self):
        self.messages: list[str] = []
        self.html: list[str] = []
        self.documents: list[tuple[str, str]] = []

    async def notify(self, text: str) -> bool:
        self.messages.append(text)
        return True

    async def notify_html(self, text: str) -> bool:
        self.html.append(text)
        return True

    async def notify_document(self, filename: str, content: str) -> bool:
        self.documents.append((filename, content))
        return True


def _pipe(tmp_path):
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings = SimpleNamespace(dry_run=False)
    pipe.state = StateStore(tmp_path)
    pipe.notifier = _Notifier()
    pipe._drafts_rows = []
    return pipe


def _lead(i: int) -> dict:
    return {"name": f"Shop {i}", "email": f"s{i}@x.test", "website": "https://x.test",
            "city": "Tampa", "vertical": "plumber"}


def test_stage_drafts_one_email_per_lead_and_asks_before_the_next(tmp_path, monkeypatch):
    async def fake_inspect(url, timeout=12.0):
        return r.PROBLEMS["no_booking"], "checked /, /contact: no booking link"

    monkeypatch.setattr(r, "inspect_site", fake_inspect)
    pipe = _pipe(tmp_path)
    leads = [_lead(i) for i in range(12)]
    report: dict = {}
    asyncio.run(pipe._stage_research_batch(leads, report))

    assert report["metrics"]["research_batch"] == 10
    assert len(pipe._drafts_rows) == 10
    assert pipe.state.load(r.STATE_FILE)["awaiting_confirm"] is True
    joined = "\n".join(pipe.notifier.html + pipe.notifier.messages)
    assert "Reply /next for the next 10" in "\n".join(pipe.notifier.messages)
    assert "Shop 0" in joined
    # Evidence reaches the owner and the sheet, so drafts can be checked.
    assert "Checked: /, /contact: no booking link" in joined
    assert "checked /, /contact" in str(pipe._drafts_rows[0][-1])
    # The CSV is attached as well.
    assert pipe.notifier.documents
    name, csv_text = pipe.notifier.documents[0]
    assert name.endswith(".csv") and "business" in csv_text

    # Second call must NOT draft anything: the batch is waiting on the owner.
    pipe._drafts_rows = []
    report2: dict = {}
    asyncio.run(pipe._stage_research_batch(leads, report2))
    assert pipe._drafts_rows == []
    assert report2["metrics"]["research_awaiting_confirm"] is True
    assert any("/next" in m for m in pipe.notifier.messages)


def test_one_batch_a_day_even_when_the_first_yielded_nothing(tmp_path, monkeypatch):
    async def clean(url, timeout=12.0):
        return None, f"{r.SKIP_CLEAN}: no defect in the checked list (/)"

    monkeypatch.setattr(r, "inspect_site", clean)
    pipe = _pipe(tmp_path)
    leads = [_lead(i) for i in range(3)]

    asyncio.run(pipe._stage_research_batch(leads, {}))
    assert "No emails were invented" in pipe.notifier.messages[-1]

    report: dict = {}
    asyncio.run(pipe._stage_research_batch(leads, report))
    assert report["metrics"]["research_already_ran"] is True
    assert "already went out today" in pipe.notifier.messages[-1]


def test_a_clean_block_does_not_block_the_next_run(tmp_path, monkeypatch):
    """Run one skips a block of clean sites; run two must reach the new leads."""
    async def clean(url, timeout=12.0):
        return None, f"{r.SKIP_CLEAN}: no defect in the checked list (/)"

    monkeypatch.setattr(r, "inspect_site", clean)
    pipe = _pipe(tmp_path)
    clean_leads = [_lead(i) for i in range(4)]
    asyncio.run(pipe._stage_research_batch(clean_leads, {}))

    async def found(url, timeout=12.0):
        return r.PROBLEMS["no_form"], "checked /: no contact form"

    monkeypatch.setattr(r, "inspect_site", found)
    pipe.state.set(r.STATE_FILE, "force_next", True)
    fresh = [_lead(i) for i in range(4, 6)]
    report: dict = {}
    asyncio.run(pipe._stage_research_batch(clean_leads + fresh, report))

    assert report["metrics"]["research_inspected"] == 2
    assert report["metrics"]["research_batch"] == 2
    assert [row[1] for row in pipe._drafts_rows] == ["Shop 4", "Shop 5"]


def test_the_batch_varies_its_wording(tmp_path, monkeypatch):
    async def fake_inspect(url, timeout=12.0):
        return r.PROBLEMS["no_booking"], "checked /: no booking link"

    monkeypatch.setattr(r, "inspect_site", fake_inspect)
    pipe = _pipe(tmp_path)
    asyncio.run(pipe._stage_research_batch([_lead(i) for i in range(4)], {}))

    bodies = [row[5] for row in pipe._drafts_rows]
    assert len(bodies) == 4
    # Four plumbers, three phrasings: no two consecutive bodies are identical,
    # and no phrasing repeats before the pool is exhausted.
    assert len(set(bodies[:3])) == 3
    assert bodies[3] == bodies[0].replace("Shop 0", "Shop 3")


def test_stage_reports_why_leads_were_skipped(tmp_path, monkeypatch):
    async def fake_inspect(url, timeout=12.0):
        if url.endswith("bad"):
            return None, f"{r.SKIP_UNREACHABLE}: could not fetch (HTTP 403)"
        return r.PROBLEMS["no_form"], "checked /: no contact form"

    monkeypatch.setattr(r, "inspect_site", fake_inspect)
    pipe = _pipe(tmp_path)
    leads = [
        {"name": "Good", "email": "g@x.test", "website": "https://x.test",
         "city": "Tampa"},
        {"name": "Bad", "email": "b@x.test", "website": "https://bad",
         "city": "Tampa"},
        {"name": "NoSite", "email": "n@x.test", "website": "", "city": "Tampa"},
    ]
    report: dict = {}
    asyncio.run(pipe._stage_research_batch(leads, report))

    joined = "\n".join(pipe.notifier.html + pipe.notifier.messages)
    assert "site did not answer (1)" in joined
    assert report["metrics"]["research_not_eligible"] == 1


def test_stage_invents_nothing_when_no_site_can_be_read(tmp_path, monkeypatch):
    async def fake_inspect(url, timeout=12.0):
        return None, f"{r.SKIP_UNREADABLE}: page not readable (blocked, empty or JavaScript-only)"

    monkeypatch.setattr(r, "inspect_site", fake_inspect)
    pipe = _pipe(tmp_path)
    leads = [_lead(0)]
    report: dict = {}
    asyncio.run(pipe._stage_research_batch(leads, report))

    assert pipe._drafts_rows == []
    assert report["metrics"]["research_batch"] == 0
    assert "No emails were invented" in "\n".join(pipe.notifier.messages)


def test_stage_says_so_when_the_pipeline_has_nothing_to_inspect(tmp_path):
    pipe = _pipe(tmp_path)
    report: dict = {}
    asyncio.run(pipe._stage_research_batch(
        [{"name": "NoEmail", "email": "", "website": "x.com", "city": "Tampa"}],
        report))
    assert report["metrics"]["research_batch"] == 0
    assert "nothing to inspect today" in pipe.notifier.messages[-1]


# ---------- telegram commands

class _GitHub:
    def __init__(self):
        self.modes: list[str] = []

    async def trigger_pipeline(self, mode: str) -> str:
        self.modes.append(mode)
        return f"Pipeline ({mode}) triggered."


def _agent(tmp_path):
    from src.agents.lead_agent import LeadAgent

    agent = LeadAgent.__new__(LeadAgent)
    agent.state = StateStore(tmp_path)
    agent.github = _GitHub()
    return agent


def test_next_confirms_the_batch_and_triggers_the_next_ten(tmp_path):
    agent = _agent(tmp_path)
    agent.state.save(r.STATE_FILE, {**r.new_state(), "awaiting_confirm": True, "batches": 1})

    assert parse_command("/next")[0] == "/next"
    out = asyncio.run(agent.delegate("/next", "", 0))

    assert agent.github.modes == ["batch"]
    stored = agent.state.load(r.STATE_FILE)
    assert stored["awaiting_confirm"] is False
    assert stored["force_next"] is True
    assert "batch" in out.lower()


def test_next_works_even_when_nothing_is_waiting(tmp_path):
    """A batch that drafted nothing left no confirmation to give; waiting for
    the next cron tick was the only option before."""
    agent = _agent(tmp_path)
    out = asyncio.run(agent.delegate("/next", "", 0))

    assert agent.github.modes == ["batch"]
    assert "runs now" in out


def test_batch_command_reports_what_was_skipped(tmp_path):
    agent = _agent(tmp_path)
    agent.state.save(r.STATE_FILE, {
        **r.new_state(), "batches": 4, "last_batch_date": "2026-10-25",
        "last_skips": {r.SKIP_CLEAN: 6, r.SKIP_UNREACHABLE: 2},
        "retry": {"abc": "2026-11-01"},
    })
    agent.state.save("last_run", {"metrics": {"research_batch": 2,
                                              "research_inspected": 10,
                                              "research_not_eligible": 3}})

    assert parse_command("/batch")[0] == "/batch"
    out = asyncio.run(agent.delegate("/batch", "", 0))

    assert "Research batches run: 4" in out
    assert "no visible defect (6)" in out
    assert "site did not answer (2)" in out
    assert "Waiting on a retry date: 1" in out
    assert "nothing will be" in out
