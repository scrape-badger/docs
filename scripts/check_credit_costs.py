#!/usr/bin/env python3
"""Fail if a documented credit cost disagrees with live production pricing.

SB-001084: `vinted/overview.mdx` advertised "1 credit" for every endpoint while
production charged 5 (search), 10 (item detail) and 3 (user/brands). The numbers
are hand-copied into two places per endpoint — the `*/overview.mdx` Credit Costs
table and the `api-reference/endpoint/**` page — so a repricing that updates the
admin UI silently leaves both stale.

Source of truth: GET /api/public/pricing, the same rows the billing middleware
charges from. Cost resolution mirrors `ScraperConfigService.get_endpoint_config`:
exact pattern match first, then SQL-LIKE with `*` -> `%`, longest pattern wins.

Usage: python3 scripts/check_credit_costs.py [--selftest]
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

PRICING_URL = "https://scrapebadger.com/api/public/pricing"
DOCS = Path(__file__).resolve().parent.parent

# Docs slug -> scraper_name in the pricing API, where they differ.
SLUG_TO_SCRAPER = {"web-scraping": "web"}

# ponytail: these price add-ons live in the web scraper's Redis config, not in
# /api/public/pricing, so no automated source exists to check them against.
# Verify by hand against `scrapers/web/src/web_scraper/pricing.py` defaults and
# the live `web:pricing:config` Redis key. Drop this once the public pricing
# endpoint exposes them.
UNCHECKABLE = {"web"}


def fetch_pricing(url: str = PRICING_URL) -> dict[str, dict]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.load(resp)
    return {s["scraper_name"]: s for s in payload["scraper_costs"]}


def resolve_cost(scraper: dict, endpoint: str) -> int | None:
    """Cost production charges for `endpoint`, or None if nothing matches."""
    endpoint = endpoint.rstrip("/")
    endpoints = scraper["endpoints"]

    for ep in endpoints:
        if ep["endpoint_pattern"].rstrip("/") == endpoint:
            return ep["base_cost"]

    # `*` is a SQL `%`: it spans `/`, so `users/*` also matches `users/1/items`.
    matches = [
        ep
        for ep in endpoints
        if re.fullmatch(
            ".*".join(re.escape(part) for part in ep["endpoint_pattern"].rstrip("/").split("*")),
            endpoint,
        )
    ]
    if not matches:
        return None
    return max(matches, key=lambda ep: len(ep["endpoint_pattern"]))["base_cost"]


def parse_endpoint_page(path: Path) -> tuple[str, str, list[int]] | None:
    """-> (scraper, endpoint pattern, documented costs), or None if not applicable.

    A page may legitimately quote a sibling endpoint's price too — the transcript
    page prices `/captions` alongside its own cost — so every claim is collected
    and the page passes if any one of them is right.
    """
    text = path.read_text()

    route = re.search(r'^openapi:\s*"[A-Z]+\s+(\S+)"', text, re.M)
    claims = re.findall(r"costs?\s+\*\*(\d+)\s+credits?\*\*", text)
    if not route or not claims:
        return None

    parts = route.group(1).strip("/").split("/")
    if len(parts) < 3 or parts[0] != "v1":
        return None

    # /v1/vinted/users/{user_id}/items -> ("vinted", "users/*/items")
    pattern = re.sub(r"\{[^}]+\}", "*", "/".join(parts[2:]))
    return parts[1], pattern, [int(c) for c in claims]


def parse_overview_costs(path: Path) -> list[int] | None:
    """Distinct credit values quoted in the page's Credit Costs table."""
    section = re.search(r"##+\s*Credit Costs(.*?)(?=\n##[^#]|\Z)", path.read_text(), re.S)
    if not section:
        return None

    costs = set()
    for line in section.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cell = line.strip().strip("|").split("|")[-1]
        found = re.search(r"(\d+)\s*credit", cell, re.I)
        if found:
            costs.add(int(found.group(1)))
    return sorted(costs) if costs else None


def check(pricing: dict[str, dict]) -> list[dict]:
    problems: list[dict] = []

    for path in sorted(DOCS.glob("api-reference/endpoint/**/*.mdx")):
        parsed = parse_endpoint_page(path)
        if not parsed:
            continue
        scraper_name, pattern, documented = parsed
        scraper = pricing.get(scraper_name)
        if not scraper or scraper_name in UNCHECKABLE:
            continue

        actual = resolve_cost(scraper, pattern)
        if actual is not None and actual not in documented:
            problems.append(
                {
                    "file": str(path.relative_to(DOCS)),
                    "endpoint": f"{scraper_name}/{pattern}",
                    "documented": documented,
                    "actual": actual,
                }
            )

    for path in sorted(DOCS.glob("*/overview.mdx")):
        slug = path.parent.name
        scraper_name = SLUG_TO_SCRAPER.get(slug, slug)
        scraper = pricing.get(scraper_name)
        if not scraper or scraper_name in UNCHECKABLE:
            continue

        documented = parse_overview_costs(path)
        if documented is None:
            continue

        # A table listing only 1s while production charges 5 and 10 is the
        # SB-001084 failure; compare the value sets, since the row labels are
        # prose and cannot be mapped back to endpoint patterns reliably.
        actual = sorted({ep["base_cost"] for ep in scraper["endpoints"]})
        if set(documented) != set(actual):
            problems.append(
                {
                    "file": str(path.relative_to(DOCS)),
                    "endpoint": f"{scraper_name} (Credit Costs table)",
                    "documented": documented,
                    "actual": actual,
                }
            )

    return problems


def selftest() -> None:
    """Pin the cost-resolution rules that mirror the billing middleware."""
    vinted = {
        "endpoints": [
            {"endpoint_pattern": "search", "base_cost": 5},
            {"endpoint_pattern": "markets", "base_cost": 0},
            {"endpoint_pattern": "items/*", "base_cost": 10},
            {"endpoint_pattern": "users/*", "base_cost": 3},
        ]
    }
    assert resolve_cost(vinted, "search") == 5, "exact match"
    assert resolve_cost(vinted, "markets") == 0, "a 0-credit endpoint is not 'no match'"
    assert resolve_cost(vinted, "items/123") == 10, "wildcard match"
    # `*` becomes a SQL `%`, which spans `/` — this is why users/*/items bills 3.
    assert resolve_cost(vinted, "users/9/items") == 3, "wildcard spans a path separator"
    assert resolve_cost(vinted, "brands") is None, "unknown endpoint"

    youtube = {
        "endpoints": [
            {"endpoint_pattern": "videos/*", "base_cost": 10},
            {"endpoint_pattern": "videos/*/transcript", "base_cost": 5},
        ]
    }
    assert resolve_cost(youtube, "videos/abc/transcript") == 5, "longest pattern wins"
    assert resolve_cost(youtube, "videos/abc") == 10

    # The transcript page prices /captions alongside itself; quoting a sibling's
    # cost must not read as drift (regression: it did, on first run).
    page = DOCS / "api-reference/endpoint/youtube/video-transcript.mdx"
    if page.exists():
        parsed = parse_endpoint_page(page)
        assert parsed and 5 in parsed[2], f"transcript page should quote 5 credits, got {parsed}"

    print("selftest ok")


def main() -> int:
    if "--selftest" in sys.argv:
        selftest()
        return 0

    try:
        pricing = fetch_pricing()
    except Exception as exc:
        print(f"Could not reach {PRICING_URL}: {exc}", file=sys.stderr)
        return 2

    problems = check(pricing)

    if problems:
        print(f"{len(problems)} documented credit cost(s) disagree with production:\n")
        for p in problems:
            print(f"  {p['file']}")
            print(f"    {p['endpoint']}: docs say {p['documented']}, production charges {p['actual']}\n")
    else:
        print("All documented credit costs match production pricing.")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
