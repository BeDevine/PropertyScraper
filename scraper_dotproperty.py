#!/usr/bin/env python3
"""
Dot Property (dotproperty.com.vn) scraper
==========================================

Crawls Dot Property's Da Nang land listings. Unlike FazWaz (which blocked
every request with HTTP 403), Dot Property's pages are plain server-rendered
HTML with no blocking observed -- verified against a real live fetch before
writing this parser, not guessed at.

Usage examples
--------------
    python3 scraper_dotproperty.py
    python3 scraper_dotproperty.py --max-price-usd 190000 --pages 5
    python3 scraper_dotproperty.py --debug --pages 1   # diagnose parsing issues

Notes
-----
- Dot Property shows prices in VND (billions/millions or raw numbers), not
  USD. There's no live FX rate lookup here, so USD figures are computed
  using a fixed approximate rate (see VND_PER_USD below) -- treat price_usd
  from this source as an approximation, not exact. price_vnd_text always
  holds the original figure so you can judge for yourself.
- Dot Property doesn't show a relative "posted X ago" date the way
  DanangMLS does, so listed_text/listed_approx_date are left blank for
  every listing from this source -- they'll fall into the "Unknown" age
  bucket in the viewer, not filterable by recency.
- Known data quality issue (confirmed earlier in this project): the same
  physical plot sometimes appears under multiple listing IDs at different
  prices, likely from re-posting agents. The dedupe_listings() function
  below catches exact price+size+district matches, but near-duplicates
  with slightly different prices will still show up twice. Treat this
  source as lower-trust than DanangMLS.
"""

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print(
        "Missing dependencies. Install them first:\n"
        "    pip install requests beautifulsoup4\n",
        file=sys.stderr,
    )
    sys.exit(1)

BASE = "https://www.dotproperty.com.vn"
# URL-encoded Vietnamese for "đà-nẵng" (Da Nang) -- confirmed working via a
# real fetch of this exact URL.
DANANG_LAND_URL = f"{BASE}/en/land-for-sale/%C4%91%C3%A0-n%E1%BA%B5ng"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Fixed approximate exchange rate, since Dot Property shows VND and we have
# no live FX lookup here. Update this occasionally -- it will drift.
VND_PER_USD = 26000

# Matches the repeated card text pattern, e.g.:
# "Land for sale in Hoa Hiep Nam, Da Nang Lien Chieu District, Da Nang
#  ₫ 3 billion - 120 m2 - ..."
# Verified against 4 real examples pulled from a live fetch before writing
# this (see conversation notes) -- not guessed blind.
CARD_RE = re.compile(
    r"Land for sale in ([^,]+), Da Nang ([A-Za-zÀ-ỹ\s]+? District), Da Nang\s*"
    r"\u20ab\s*([\d,.]+)\s*(billion|million)?\s*-?\s*([\d.,]+)\s*m\s*2",
    re.I,
)


@dataclass
class Listing:
    title: str = ""
    url: str = ""
    price_usd: float | None = None
    price_vnd_text: str = ""
    size_m2: float | None = None
    price_per_m2_usd: float | None = None
    bedrooms: int | None = None
    property_type: str = "Land"
    district: str = ""
    listed_text: str = ""
    listed_approx_date: str = ""
    latitude: float | None = None
    longitude: float | None = None
    agent: str = ""
    contact: str = ""
    description: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


def polite_get(session: requests.Session, url: str, delay: float) -> BeautifulSoup | None:
    time.sleep(delay)
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        print(f"  ! request failed for {url}: {e}", file=sys.stderr)
        return None
    if resp.status_code == 429:
        print("  ! got 429 (rate limited) -- stopping early.", file=sys.stderr)
        return "RATE_LIMITED"
    if resp.status_code != 200:
        print(f"  ! got HTTP {resp.status_code} for {url}", file=sys.stderr)
        return None
    return BeautifulSoup(resp.text, "html.parser")


def find_card_container(anchor, max_levels: int = 8):
    """Walk up from a listing's <a> tag to find the smallest ancestor whose
    text contains exactly one match of the card pattern -- localizes
    extraction to a single card without needing exact class names."""
    node = anchor
    for _ in range(max_levels):
        node = node.find_parent()
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        matches = CARD_RE.findall(text)
        if len(matches) == 1:
            return node
        if len(matches) > 1:
            return None
    return None


def vnd_to_usd(amount_str: str, unit: str | None) -> float | None:
    try:
        amount = float(amount_str.replace(",", ""))
    except ValueError:
        return None
    if unit and unit.lower() == "billion":
        vnd = amount * 1_000_000_000
    elif unit and unit.lower() == "million":
        vnd = amount * 1_000_000
    else:
        vnd = amount  # already a raw VND figure, e.g. "950,000,000"
    return round(vnd / VND_PER_USD, 2)


def vnd_display_text(amount_str: str, unit: str | None) -> str:
    if unit:
        return f"\u20ab {amount_str} {unit}"
    return f"\u20ab {amount_str}"


def parse_page(soup: BeautifulSoup, debug: bool = False) -> list[Listing]:
    listings = []
    seen_urls = set()

    anchors = soup.find_all("a", href=re.compile(r"/en/land-for-sale-in-[a-z0-9-]+-da-nang_\d+"))
    if debug:
        print(f"    [debug] matching <a> tags found: {len(anchors)}")
        own_text_matches = sum(1 for a in anchors if CARD_RE.search(a.get_text(" ", strip=True)))
        print(f"    [debug] anchors whose OWN text matches CARD_RE: {own_text_matches}")
        for i, a in enumerate(anchors[:6]):
            t = a.get_text(" ", strip=True)
            print(f"    [debug] anchor {i} href={a.get('href','')[:70]} "
                  f"own_text_len={len(t)} own_text={t[:120]!r}")

    for a in anchors:
        href = urljoin(BASE, a["href"])
        if href in seen_urls:
            continue

        # The full listing text (title, district, price, size, description)
        # lives directly in the anchor's own text for the "title" anchor --
        # image-carousel anchors that share the same href have empty text
        # and simply won't match here, so they're naturally skipped without
        # needing a container walk-up.
        own_text = a.get_text(" ", strip=True)
        m = CARD_RE.search(own_text)
        text = own_text

        if not m:
            # Fallback for any cards where the markup differs.
            container = find_card_container(a)
            if container is None:
                continue
            text = container.get_text(" ", strip=True)
            m = CARD_RE.search(text)
            if not m:
                continue

        seen_urls.add(href)
        sub_district, district, price_amount, price_unit, size_str = m.groups()

        listing = Listing(url=href)
        listing.title = f"Land for sale in {sub_district.strip()}, Da Nang"
        listing.district = district.strip()
        try:
            listing.size_m2 = float(size_str.replace(",", ""))
        except ValueError:
            pass

        listing.price_vnd_text = vnd_display_text(price_amount, price_unit)
        listing.price_usd = vnd_to_usd(price_amount, price_unit)
        if listing.price_usd and listing.size_m2:
            listing.price_per_m2_usd = round(listing.price_usd / listing.size_m2, 2)

        listing.description = text[:800]
        listings.append(listing)

    return listings


def crawl(args) -> list[Listing]:
    session = requests.Session()
    all_listings: dict[str, Listing] = {}

    print(f"Starting Dot Property crawl: pages={args.pages}")

    for page in range(1, args.pages + 1):
        url = DANANG_LAND_URL if page == 1 else f"{DANANG_LAND_URL}?page={page}"
        print(f"[page {page}] {url}")
        soup = polite_get(session, url, args.delay)
        if soup == "RATE_LIMITED":
            break
        if soup is None:
            continue

        page_listings = parse_page(soup, debug=(args.debug and page == 1))
        new_count = sum(1 for l in page_listings if l.url not in all_listings)
        print(f"  found {len(page_listings)} listings ({new_count} new)")

        if new_count == 0 and page > 1:
            print("  no new listings -- likely reached the end or pagination "
                  "param didn't advance the page. Stopping.")
            break

        for listing in page_listings:
            if args.max_price_usd and listing.price_usd and listing.price_usd > args.max_price_usd:
                continue
            all_listings[listing.url] = listing

    return list(all_listings.values())


def dedupe_listings(listings: list[Listing]) -> list[Listing]:
    """Same price+size+district collapse as the other scrapers. Won't catch
    near-duplicates with slightly different prices (a known issue on this
    site) -- see module docstring."""
    seen: dict[tuple, Listing] = {}
    for l in listings:
        key = (l.price_usd, l.size_m2, l.district)
        if key == (None, None, ""):
            seen[l.url] = l
            continue
        seen[key] = l
    return list(seen.values())


def save_outputs(listings: list[Listing], outdir: Path, source: str = "dotproperty"):
    outdir.mkdir(parents=True, exist_ok=True)
    hist_dir = outdir / "history"
    hist_dir.mkdir(exist_ok=True)

    records = [asdict(l) for l in listings]
    for r in records:
        r["source"] = source

    json_path = outdir / f"listings_{source}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    csv_path = outdir / f"listings_{source}.csv"
    if records:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    snapshot_path = hist_dir / f"listings_{source}_{stamp}.json"
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(records)} listings:")
    print(f"  {json_path}")
    print(f"  {csv_path}")
    print(f"  {snapshot_path}")


def main():
    parser = argparse.ArgumentParser(description="Scrape Dot Property Da Nang land listings.")
    parser.add_argument("--max-price-usd", type=float, default=None,
                         help="Skip listings priced above this (USD, approximate)")
    parser.add_argument("--pages", type=int, default=10,
                         help="How many index pages to crawl (default 10)")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="Seconds to wait between requests")
    parser.add_argument("--outdir", type=Path, default=Path("data"),
                         help="Output directory (default ./data)")
    parser.add_argument("--debug", action="store_true",
                         help="Print diagnostic info about the first page's HTML structure")
    args = parser.parse_args()

    listings = crawl(args)
    before = len(listings)
    listings = dedupe_listings(listings)
    if before != len(listings):
        print(f"\nDe-duplicated: {before} -> {len(listings)} listings")

    save_outputs(listings, args.outdir)


if __name__ == "__main__":
    main()
