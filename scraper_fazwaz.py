#!/usr/bin/env python3
"""
FazWaz.vn scraper
==================

Crawls fazwaz.vn district category pages for Da Nang and extracts listing
cards directly from the category page (no need to visit each individual
listing page -- FazWaz's category pages already show price, size, and
sub-district for every card).

Usage examples
--------------
    # All land listings across every Da Nang district we know about
    python3 scraper_fazwaz.py

    # Just Lien Chieu, land under $190,000
    python3 scraper_fazwaz.py --district lien-chieu --max-price-usd 190000

Notes
-----
- Unlike DanangMLS, FazWaz doesn't expose a clean JSON-LD block per card, so
  this scraper anchors on each listing's <a href="/property-sales/..."> link
  and walks UP the DOM to find the smallest ancestor element that contains
  that card's full text (title, sub-district, price, and the canned
  "This property is a X SqM land plot..." sentence). This is more resilient
  to markup changes than guessing a specific CSS class, but it has NOT been
  validated against FazWaz's real live HTML in this session (only against
  rendered/converted page content) -- so the first live run is the real
  test. If it comes back with 0 listings or garbled fields, that's the
  first thing to debug.
- Same politeness rules as the DanangMLS scraper: one request at a time,
  a delay between requests, normal browser headers.
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

BASE = "https://www.fazwaz.vn"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# FazWaz's Da Nang district slugs (as used in /land-for-sale/vietnam/da-nang/<slug>)
DISTRICT_SLUGS = [
    "hai-chau", "thanh-khe", "son-tra", "ngu-hanh-son",
    "cam-le", "lien-chieu", "hoa-vang",
]

# The fixed SEO sentence FazWaz generates for every land listing card.
# Anchoring on this exact template is more reliable than guessing CSS
# classes, since it's plain visible text unlikely to change even if the
# page's markup/classes get redesigned.
CARD_SENTENCE_RE = re.compile(
    r"This property is an?\s+([\d,]+(?:\.\d+)?)\s*SqM land plot that is available for sale\.\s*"
    r"It is located in ([^,.]+),\s*Da Nang\.\s*"
    r"You can buy this land for a base price of (\$|\u20ab)?\s*([\d,]+)"
    r"(?:\s*\((\$|\u20ab)?\s*([\d,]+)/SqM\))?",
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


def find_card_container(anchor, sentence_re=CARD_SENTENCE_RE, max_levels: int = 8):
    """Walk up from a listing's <a> tag to find the smallest ancestor whose
    text contains exactly one match of the card sentence -- this localizes
    extraction to a single card without needing to know FazWaz's actual
    class names."""
    node = anchor
    for _ in range(max_levels):
        node = node.find_parent()
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        matches = sentence_re.findall(text)
        if len(matches) == 1:
            return node
        if len(matches) > 1:
            # We've walked up too far and now span multiple cards -- the
            # previous (smaller) node was the right one, but we already
            # moved past it, so just bail and let the caller fall back.
            return None
    return None


def extract_title(container, anchor) -> str:
    # The card's own title is usually the anchor's text, or a nearby
    # heading/link with the same href repeated.
    text = anchor.get_text(strip=True)
    if text and len(text) > 5:
        return text
    # fallback: first reasonably long line of text in the container
    for line in container.get_text("\n", strip=True).split("\n"):
        if len(line) > 15 and "SqM" not in line and "$" not in line:
            return line
    return ""


def parse_district_page(soup: BeautifulSoup, url: str) -> list[Listing]:
    listings = []
    seen_urls = set()

    for a in soup.find_all("a", href=re.compile(r"/property-sales/[a-z0-9-]+-u\d+")):
        href = urljoin(BASE, a["href"])
        if href in seen_urls:
            continue

        container = find_card_container(a)
        if container is None:
            continue  # couldn't isolate this card cleanly -- skip rather than guess

        text = container.get_text(" ", strip=True)
        m = CARD_SENTENCE_RE.search(text)
        if not m:
            continue

        seen_urls.add(href)
        size_str, locality, price_currency, price_str, ppm2_currency, ppm2_str = m.groups()

        listing = Listing(url=href)
        listing.title = extract_title(container, a)
        listing.district = locality.strip() if locality else ""
        try:
            listing.size_m2 = float(size_str.replace(",", ""))
        except (ValueError, TypeError):
            pass

        # Only trust the price as USD if it's actually marked with $ --
        # some FazWaz listings show VND (\u20ab) instead, and treating those
        # numbers as dollars would be a huge, silent error (a listing
        # priced "180,000,000,000 \u20ab" is NOT $180 billion).
        if price_currency == "$":
            try:
                listing.price_usd = float(price_str.replace(",", ""))
            except (ValueError, TypeError):
                pass
        else:
            listing.price_vnd_text = f"{price_str} \u20ab" if price_str else ""

        if ppm2_currency == "$" and ppm2_str:
            try:
                listing.price_per_m2_usd = float(ppm2_str.replace(",", ""))
            except ValueError:
                pass
        elif listing.price_usd and listing.size_m2:
            listing.price_per_m2_usd = round(listing.price_usd / listing.size_m2, 2)

        listing.description = text[:800]
        listings.append(listing)

    return listings


def crawl(args) -> list[Listing]:
    session = requests.Session()
    all_listings: dict[str, Listing] = {}

    districts = [args.district] if args.district else DISTRICT_SLUGS
    print(f"Starting FazWaz crawl: districts={districts}")

    for district in districts:
        url = f"{BASE}/land-for-sale/vietnam/da-nang/{district}"
        print(f"[district] {url}")
        soup = polite_get(session, url, args.delay)
        if soup == "RATE_LIMITED":
            break
        if soup is None:
            continue

        page_listings = parse_district_page(soup, url)
        print(f"  found {len(page_listings)} listings")

        for listing in page_listings:
            if args.max_price_usd and listing.price_usd and listing.price_usd > args.max_price_usd:
                continue
            all_listings[listing.url] = listing

    return list(all_listings.values())


def save_outputs(listings: list[Listing], outdir: Path, source: str = "fazwaz"):
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
    parser = argparse.ArgumentParser(description="Scrape FazWaz.vn Da Nang land listings.")
    parser.add_argument("--district", choices=DISTRICT_SLUGS, default=None,
                         help="Limit to one district, e.g. lien-chieu. Default: all districts.")
    parser.add_argument("--max-price-usd", type=float, default=None,
                         help="Skip listings priced above this (USD)")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="Seconds to wait between requests (default 1.5, be polite)")
    parser.add_argument("--outdir", type=Path, default=Path("data"),
                         help="Output directory (default ./data)")
    args = parser.parse_args()

    listings = crawl(args)
    save_outputs(listings, args.outdir)


if __name__ == "__main__":
    main()
