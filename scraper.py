#!/usr/bin/env python3
"""
DanangMLS scraper
==================

Crawls danangmls.com for-sale listings and saves structured data (price,
size, district, listing age, coordinates, contact info, URL) to JSON + CSV.

Usage examples
--------------
    # Everything for sale, first 10 pages
    python3 scraper.py --pages 10

    # Just land listings in Ngu Hanh Son
    python3 scraper.py --district ngu-hanh-son --type land --pages 5

    # Land under 5 billion VND (~190,000 USD) anywhere
    python3 scraper.py --type land --max-price-usd 190000 --pages 15

Notes
-----
- This script makes real HTTP requests. It is NOT bundled with network
  access in this chat -- run it on your own machine (`pip install requests
  beautifulsoup4` first).
- It is polite by design: one request at a time, a delay between requests,
  a normal browser User-Agent, and it stops if it gets blocked or rate
  limited rather than hammering the site. Please don't remove the delay.
- Re-run it whenever you want fresh data -- it always overwrites
  data/listings.json and data/listings.csv with the latest crawl, and also
  keeps a dated snapshot in data/history/ so you can compare over time.
"""

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

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

BASE = "https://www.danangmls.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DISTRICT_SLUGS = [
    "hai-chau", "thanh-khe", "son-tra", "ngu-hanh-son",
    "cam-le", "lien-chieu", "hoi-an",
]
TYPE_SLUGS = ["land", "house", "apartment", "villa", "townhouse"]


@dataclass
class Listing:
    title: str = ""
    url: str = ""
    price_usd: float | None = None
    price_vnd_text: str = ""
    size_m2: float | None = None
    price_per_m2_usd: float | None = None
    bedrooms: int | None = None
    property_type: str = ""
    district: str = ""
    listed_text: str = ""       # e.g. "2 months ago"
    listed_approx_date: str = ""  # ISO date, computed from listed_text
    latitude: float | None = None
    longitude: float | None = None
    agent: str = ""
    contact: str = ""
    description: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


def polite_get(session: requests.Session, url: str, delay: float) -> BeautifulSoup | None:
    """Fetch a URL with a delay, return parsed soup or None on failure."""
    time.sleep(delay)
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        print(f"  ! request failed for {url}: {e}", file=sys.stderr)
        return None
    if resp.status_code == 429:
        print("  ! got 429 (rate limited) -- stopping early. Try again later "
              "with a longer --delay.", file=sys.stderr)
        return "RATE_LIMITED"
    if resp.status_code != 200:
        print(f"  ! got HTTP {resp.status_code} for {url}", file=sys.stderr)
        return None
    return BeautifulSoup(resp.text, "html.parser")


def parse_relative_date(text: str) -> str:
    """Convert '2 months ago' / '5 days ago' style text into an ISO date."""
    text = text.lower().strip()
    m = re.search(r"(\d+)\s+(day|week|month|year)s?\s+ago", text)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    days_per_unit = {"day": 1, "week": 7, "month": 30, "year": 365}
    delta = timedelta(days=n * days_per_unit[unit])
    return (datetime.utcnow() - delta).date().isoformat()


def extract_coords_from_map(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    """DanangMLS embeds an OpenStreetMap iframe with a marker=lat,lon param."""
    iframe = soup.find("iframe", src=re.compile("openstreetmap.org"))
    if not iframe or not iframe.get("src"):
        return None, None
    qs = parse_qs(urlparse(iframe["src"]).query)
    marker = qs.get("marker", [None])[0]
    if marker and "," in marker:
        try:
            lat, lon = marker.split(",")
            return float(lat), float(lon)
        except ValueError:
            pass
    return None, None


def parse_listing_page(soup: BeautifulSoup, url: str) -> Listing:
    listing = Listing(url=url)

    h1 = soup.find("h1")
    if h1:
        listing.title = h1.get_text(strip=True)

    # Price like "$114,000"
    price_tag = soup.find(string=re.compile(r"^\$[\d,]+$"))
    if price_tag:
        listing.price_usd = float(price_tag.strip().replace("$", "").replace(",", ""))

    # District: look for a link back to /for-sale/<district>
    district_link = soup.find("a", href=re.compile(r"/for-sale/[a-z-]+$"))
    if district_link:
        listing.district = district_link.get_text(strip=True)

    # "Listed 🗓 2 months ago"
    listed_tag = soup.find(string=re.compile(r"ago$"))
    if listed_tag:
        listing.listed_text = listed_tag.strip()
        listing.listed_approx_date = parse_relative_date(listing.listed_text)

    # Bedrooms
    bed_tag = soup.find(string=re.compile(r"\d+\s+Bedrooms?"))
    if bed_tag:
        m = re.search(r"(\d+)", bed_tag)
        if m:
            listing.bedrooms = int(m.group(1))

    # Size in m^2 -- scan description text for e.g. "204m²" or "180 m2"
    body_text = soup.get_text(" ", strip=True)
    size_match = re.search(r"(\d{2,4}(?:[.,]\d+)?)\s*m2|(\d{2,4}(?:[.,]\d+)?)\s*m\u00b2", body_text)
    if size_match:
        raw = (size_match.group(1) or size_match.group(2)).replace(",", "")
        try:
            listing.size_m2 = float(raw)
        except ValueError:
            pass

    if listing.price_usd and listing.size_m2:
        listing.price_per_m2_usd = round(listing.price_usd / listing.size_m2, 2)

    # Property type from breadcrumb-ish link e.g. /for-sale/land
    type_link = soup.find("a", href=re.compile(r"/for-sale/(land|house|apartment|villa|townhouse)"))
    if type_link:
        listing.property_type = type_link.get_text(strip=True)

    # Contact block
    contact_block = soup.find(string=re.compile(r"Zalo|WhatsApp"))
    if contact_block:
        parent = contact_block.find_parent()
        if parent:
            listing.contact = parent.get_text(" ", strip=True)

    agent_tag = soup.find(string=re.compile("Agent"))
    if agent_tag:
        parent = agent_tag.find_parent()
        if parent:
            sib_text = parent.get_text(" ", strip=True).replace("Agent", "").strip()
            listing.agent = sib_text

    # Description
    desc_header = soup.find(["h2", "h3"], string=re.compile("Description", re.I))
    if desc_header:
        desc_parts = []
        for sib in desc_header.find_next_siblings():
            if sib.name in ("h2", "h3"):
                break
            desc_parts.append(sib.get_text(" ", strip=True))
        listing.description = " ".join(desc_parts)[:1000]

    listing.latitude, listing.longitude = extract_coords_from_map(soup)

    return listing


def build_listing_index_url(page: int, district: str | None, ptype: str | None) -> str:
    path = "/for-sale"
    if district:
        path += f"/{district}"
    if ptype:
        path += f"/{ptype}" if not district else f"?type={ptype}"
    sep = "&" if "?" in path else "?"
    return f"{BASE}{path}{sep}page={page}" if page > 1 else f"{BASE}{path}"


def find_listing_links(soup: BeautifulSoup) -> list[str]:
    links = set()
    for a in soup.find_all("a", href=re.compile(r"^/listing/")):
        links.add(urljoin(BASE, a["href"]))
    return sorted(links)


def crawl(args) -> list[Listing]:
    session = requests.Session()
    all_listings: dict[str, Listing] = {}

    print(f"Starting crawl: district={args.district or 'all'} "
          f"type={args.type or 'all'} pages={args.pages} delay={args.delay}s")

    for page in range(1, args.pages + 1):
        index_url = build_listing_index_url(page, args.district, args.type)
        print(f"[page {page}] {index_url}")
        soup = polite_get(session, index_url, args.delay)
        if soup == "RATE_LIMITED":
            break
        if soup is None:
            continue

        links = find_listing_links(soup)
        if not links:
            print("  no more listings found, stopping pagination early.")
            break

        for link in links:
            if link in all_listings:
                continue
            print(f"  -> {link}")
            lsoup = polite_get(session, link, args.delay)
            if lsoup == "RATE_LIMITED":
                return list(all_listings.values())
            if lsoup is None:
                continue
            listing = parse_listing_page(lsoup, link)

            if args.max_price_usd and listing.price_usd and listing.price_usd > args.max_price_usd:
                continue
            if args.type and listing.property_type and args.type.lower() not in listing.property_type.lower():
                continue

            all_listings[link] = listing

    return list(all_listings.values())


def save_outputs(listings: list[Listing], outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    hist_dir = outdir / "history"
    hist_dir.mkdir(exist_ok=True)

    records = [asdict(l) for l in listings]

    json_path = outdir / "listings.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    csv_path = outdir / "listings.csv"
    if records:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    snapshot_path = hist_dir / f"listings_{stamp}.json"
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(records)} listings:")
    print(f"  {json_path}")
    print(f"  {csv_path}")
    print(f"  {snapshot_path}  (dated snapshot, for tracking changes over time)")


def main():
    parser = argparse.ArgumentParser(description="Scrape DanangMLS for-sale listings.")
    parser.add_argument("--district", choices=DISTRICT_SLUGS, default=None,
                         help="Limit to one district, e.g. ngu-hanh-son")
    parser.add_argument("--type", choices=TYPE_SLUGS, default=None,
                         help="Limit to one property type, e.g. land")
    parser.add_argument("--max-price-usd", type=float, default=None,
                         help="Skip listings priced above this (USD)")
    parser.add_argument("--pages", type=int, default=5,
                         help="How many index pages to crawl (default 5)")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="Seconds to wait between requests (default 1.5, be polite)")
    parser.add_argument("--outdir", type=Path, default=Path("data"),
                         help="Output directory (default ./data)")
    args = parser.parse_args()

    listings = crawl(args)
    save_outputs(listings, args.outdir)


if __name__ == "__main__":
    main()
