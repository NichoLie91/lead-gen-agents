"""Composio connection pre-flight check (spec 6.3).

Usage:
    COMPOSIO_API_KEY=<key> python -m src.preflight
        -> print live status of every connection (googlesheets, gmail, instagram)

    COMPOSIO_API_KEY=<key> python -m src.preflight --connect gmail googlesheets instagram
        -> mint OAuth login links for the exact tools so they can be authorized

Works with the composio-core SDK when installed, otherwise falls back to the
Composio REST API via httpx. NOTE: an API key is REQUIRED in both cases — the
connection links must be signed against your Composio account.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from src.agents.composio_agent import ComposioAgent
from src.core.config import Settings

log = logging.getLogger(__name__)

REQUIRED = ("googlesheets", "gmail")
OPTIONAL = ("instagram", "github")


async def _check() -> None:
    settings = Settings.load()
    print(f"COMPOSIO_API_KEY present: {bool(settings.composio_api_key)}")
    composio = ComposioAgent(settings)
    if not composio.connected:
        print("\n-> Composio is NOT connected. Get a key at https://dashboard.composio.dev")
        print("   then:  COMPOSIO_API_KEY=<key> python -m src.preflight")
        return

    statuses = await composio.preflight()
    print("\nConnection status:")
    for conn, status in statuses.items():
        marker = "OK " if status == "ACTIVE" else "!! "
        print(f"  {marker}{conn}: {status}")
    required_ok = all(statuses.get(c) == "ACTIVE" for c in REQUIRED)
    print(f"\nRequired connections (googlesheets + gmail) ready: {required_ok}")
    if statuses.get("instagram") != "ACTIVE":
        print("Instagram: not ready — connect a Meta Business IG account via Composio OAuth.")


async def _rest_preflight(api_key: str) -> dict[str, str]:
    """Fallback REST check (v3 API) that does not require the composio-core SDK."""
    url = "https://backend.composio.dev/api/v3/connected_accounts"
    statuses = {c: "UNKNOWN" for c in REQUIRED + OPTIONAL}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"x-api-key": api_key})
        if resp.status_code != 200:
            return {c: f"ERROR {resp.status_code}" for c in statuses}
        accounts = resp.json().get("items", [])
        connected = {
            str((a.get("toolkit") or {}).get("slug", "")).lower()
            for a in accounts
        }
        for conn in list(statuses):
            matched = any(conn in app or app in conn for app in connected)
            statuses[conn] = "ACTIVE" if matched else "MISSING"
    except httpx.HTTPError as exc:
        log.warning("REST preflight failed: %s", exc)
        return {c: "ERROR" for c in statuses}
    return statuses


async def _check_with_rest(api_key: str) -> None:
    statuses = await _rest_preflight(api_key)
    print("\nConnection status (via Composio REST API):")
    for conn, status in statuses.items():
        marker = "OK " if status == "ACTIVE" else "!! "
        print(f"  {marker}{conn}: {status}")
    required_ok = all(statuses.get(c) == "ACTIVE" for c in REQUIRED)
    print(f"\nRequired connections (googlesheets + gmail) ready: {required_ok}")


# App name used to *initiate* connections (Composio slugs).
APP_SLUGS = {"googlesheets": "googlesheets", "gmail": "gmail", "instagram": "instagram_business"}


async def _make_connection_link(composio: ComposioAgent, app: str) -> str:
    """Mint an OAuth login link for one tool (SDK first, REST fallback)."""
    slug = APP_SLUGS.get(app, app)
    # 1) SDK path
    if composio.connected and composio._toolset is not None:
        try:
            result = await composio._toolset.initiate_connection(
                app=slug, entity_id="default"
            )
            for attr in ("connectionUrl", "redirectUrl", "url"):
                url = getattr(result, attr, None) or (result.get(attr) if isinstance(result, dict) else None)
                if url:
                    return str(url)
        except Exception as exc:
            log.warning("SDK initiate_connection(%s) failed: %s", app, exc)

    # 2) REST fallback
    async with httpx.AsyncClient(timeout=20) as client:
        headers = {"x-api-key": composio.settings.composio_api_key}
        try:
            resp = await client.get(
                "https://backend.composio.dev/api/v1/integrations",
                params={"appUniqueId": slug, "userUuid": "default"},
                headers=headers,
            )
            integrations = resp.json()
            integration_id = None
            if isinstance(integrations, list) and integrations:
                integration_id = integrations[0].get("id")
            elif isinstance(integrations, dict):
                items = integrations.get("items", [])
                integration_id = items[0].get("id") if items else None
            if not integration_id:
                return f"ERROR: no integration found for {app} (create one in the dashboard)"
            resp2 = await client.post(
                "https://backend.composio.dev/api/v1/connected_accounts/initiate",
                json={"integrationId": integration_id, "entityId": "default"},
                headers=headers,
            )
            payload = resp2.json()
            for key in ("connectionUrl", "redirectUrl", "url"):
                if payload.get(key):
                    return str(payload[key])
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("connectionUrl", "redirectUrl", "url"):
                    if data.get(key):
                        return str(data[key])
            return f"ERROR: unexpected initiate response: {str(payload)[:200]}"
        except httpx.HTTPError as exc:
            return f"ERROR: {exc}"


async def _catalog() -> None:
    """Scan the v3 tool catalog for maps / web-search actions.

    Used to verify whether Composio exposes a discovery source after the user
    connects a Maps/search app in the dashboard (2026-08: none is present, so
    live discovery returns 0 candidates).
    """
    settings = Settings.load()
    if not settings.composio_api_key:
        print("COMPOSIO_API_KEY required to inspect the catalog.")
        return
    headers = {"x-api-key": settings.composio_api_key}
    seen: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=60) as client:
        for term in ("google maps", "place", "serp", "tavily", "search web"):
            try:
                resp = await client.get(
                    "https://backend.composio.dev/api/v3/tools",
                    params={"search": term, "limit": 100},
                    headers=headers,
                )
                for item in resp.json().get("items", []):
                    slug = item.get("slug", "")
                    if any(k in slug.upper() for k in ("MAP", "PLACE", "SERP", "TAVILY")):
                        seen[slug] = (item.get("toolkit") or {}).get("slug", "")
            except httpx.HTTPError as exc:
                print(f"  search '{term}' failed: {exc}")
    print("\nMaps / web-search tools in the Composio catalog:")
    if not seen:
        print("  (none)")
        print("\nConnect a Maps or search app in the Composio dashboard, then re-run")
        print("  python -m src.preflight --catalog")
    for slug, toolkit in sorted(seen.items()):
        print(f"  {slug}  |  toolkit: {toolkit}")


async def _connect(apps: list[str]) -> None:
    settings = Settings.load()
    if not settings.composio_api_key:
        print("COMPOSIO_API_KEY is REQUIRED to generate connection login links.")
        print("Get one at https://dashboard.composio.dev -> API Keys, then:")
        print("  COMPOSIO_API_KEY=<key> python -m src.preflight --connect " + " ".join(apps))
        return
    composio = ComposioAgent(settings)
    print("Generating OAuth login links (expire in ~10 minutes):")
    for app in apps:
        url = await _make_connection_link(composio, app)
        print(f"\n  {app}: {url}")
    print("\nOpen each link in the browser and authorize. Then re-run:")
    print("  COMPOSIO_API_KEY=<key> python -m src.preflight")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Composio connection checks")
    parser.add_argument("--connect", nargs="*", default=None,
                        help="Generate OAuth login links for the given tools "
                             "(googlesheets, gmail, instagram)")
    parser.add_argument("--catalog", action="store_true",
                        help="Scan the Composio catalog for maps / web-search actions")
    args = parser.parse_args()

    import src.core.logging as logmod
    logmod.setup_logging()
    if args.catalog:
        asyncio.run(_catalog())
        return 0
    if args.connect is not None:
        asyncio.run(_connect(args.connect or list(APP_SLUGS)))
        return 0
    settings = Settings.load()
    if not settings.composio_api_key:
        print("No COMPOSIO_API_KEY found.")
        print("Add it via environment variable or a .env file, then re-run.")
        return 1
    try:
        import composio  # noqa: F401  # is the SDK installed?
        asyncio.run(_check())
    except ImportError:
        asyncio.run(_check_with_rest(settings.composio_api_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
