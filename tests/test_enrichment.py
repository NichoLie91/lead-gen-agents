"""Enrichment tests: natural-query phrasing + website/contact-page fetch.

Live probe (2026-08): Tavily returns ZERO results for \"...contact email\"
phrasing but solid hits for natural business queries, so _find_email must
search the business name + city, then fetch the lead's website /contact page
(emails live there far more often than in search snippets).
"""
from __future__ import annotations

import asyncio

from src import enrichment


class FakeComposio:
    """Records search queries + fetched URLs; serves canned snippets/HTML."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fetch_urls: list[str] = []
        self.snippet_emails: dict[str, str] = {}
        self.no_email_urls: set[str] = set()

    async def search_web(self, query: str) -> list[dict]:
        self.queries.append(query)
        email = self.snippet_emails.get(query)
        if email:
            return [{"snippet": f"Reach us at {email}", "url": "https://s.example"}]
        return []

    async def fetch_url(self, url: str) -> str:
        self.fetch_urls.append(url)
        if any(url.startswith(u) for u in self.no_email_urls):
            return "<html><body>No contact details published.</body></html>"
        if "contact" in url:
            return "<html><body>Mail us: mailto:info@contact.example</body></html>"
        return "<html><body>No email on the homepage.</body></html>"


def test_find_email_uses_natural_query_not_contact_email():
    comp = FakeComposio()
    lead = {"name": "Plumbco", "city": "Houston", "vertical": "plumber",
            "website": "https://plumbco.example"}
    email = asyncio.run(enrichment._find_email(comp, lead))

    assert email == "info@contact.example"
    # The phrasing that killed Tavily must never be used.
    assert all("contact email" not in q.lower() for q in comp.queries)
    assert comp.queries[0] == "Plumbco Houston"
    assert comp.queries[1] == "Plumbco Houston plumber company"
    # Homepage first (no email), then the /contact page.
    assert comp.fetch_urls == ["https://plumbco.example", "https://plumbco.example/contact"]


def test_find_email_stops_at_first_snippet_email():
    comp = FakeComposio()
    comp.snippet_emails = {"Plumbco Houston": "info@snippet.example"}
    lead = {"name": "Plumbco", "city": "Houston"}
    email = asyncio.run(enrichment._find_email(comp, lead))

    assert email == "info@snippet.example"
    assert comp.fetch_urls == []  # found before any website fetch


def test_find_email_returns_none_when_nothing_found():
    comp = FakeComposio()
    comp.no_email_urls = {"https://no.example"}
    lead = {"name": "Noemail Co", "city": "Memphis", "website": "https://no.example"}
    assert asyncio.run(enrichment._find_email(comp, lead)) is None
    assert comp.fetch_urls == ["https://no.example", "https://no.example/contact"]
