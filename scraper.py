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
    """Convert 'X hours/days/weeks/months/years ago' style text into an ISO date."""
    text = text.lower().strip()
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", text)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    days_per_unit = {"minute": 1/1440, "hour": 1/24, "day": 1, "week": 7, "month": 30, "year": 365}
    delta = timedelta(days=n * days_per_unit[unit])
    return (datetime.utcnow() - delta).date().isoformat()


def parse_json_ld(soup: BeautifulSoup) -> dict:
    """DanangMLS embeds a <script type="application/ld+json"> RealEstateListing
    block on every listing page -- this is far more reliable than guessing at
    CSS selectors, since it's structured data meant to be machine-read."""
    tag = soup.find("script", type="application/ld+json")
    if not tag or not tag.string:
        return {}
    try:
        return json.loads(tag.string)
    except (json.JSONDecodeError, TypeError):
        return {}


def extract_contact_phone(description: str) -> str:
    """Pull a phone/Zalo number out of the free-text description instead of
    keeping the raw JSON-LD blob. Falls back to '' if nothing found."""
    m = re.search(r"(?:Zalo|WhatsApp)[^\d]{0,15}(\+?\d[\d\s.]{7,})", description)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(0\d{2,3}[\s.]?\d{3}[\s.]?\d{3,4})\b", description)
    return m.group(1).strip() if m else ""


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
    ld = parse_json_ld(soup)

    # --- Title ---
    h1 = soup.find("h1")
    listing.title = h1.get_text(strip=True) if h1 else ld.get("name", "")

    # --- Price: prefer JSON-LD ("$114,000" or a "$X - $Y" range -> take the low end) ---
    price_str = ld.get("price", "")
    if not price_str:
        price_tag = soup.find(string=re.compile(r"^\$[\d,]+"))
        price_str = price_tag.strip() if price_tag else ""
    price_match = re.search(r"\$([\d,]+)", price_str)
    if price_match:
        listing.price_usd = float(price_match.group(1).replace(",", ""))

    # --- District: JSON-LD addressLocality is authoritative and structured ---
    address = ld.get("address", {}) if isinstance(ld.get("address"), dict) else {}
    locality = address.get("addressLocality", "")
    listing.district = locality if locality and locality.lower() != "not provided" else ""

    # --- Bedrooms: JSON-LD numberOfRooms (land correctly shows 0, not a stray "1") ---
    rooms_str = ld.get("numberOfRooms")
    if rooms_str is not None:
        try:
            listing.bedrooms = int(rooms_str)
        except (ValueError, TypeError):
            pass

    # --- Description: prefer JSON-LD description (full text), fallback to page ---
    listing.description = (ld.get("description") or "").strip()
    if not listing.description:
        desc_header = soup.find(["h2", "h3"], string=re.compile("Description", re.I))
        if desc_header:
            parts = []
            for sib in desc_header.find_next_siblings():
                if sib.name in ("h2", "h3"):
                    break
                parts.append(sib.get_text(" ", strip=True))
            listing.description = " ".join(parts)[:1500]

    # --- Size in m^2: check description for m2/m²/sqm/"square meters", prefer an
    # explicit "Area: Xm2" style line over the first stray number in the text ---
    size_pattern = r"(\d{1,3}(?:[.,]\d{3})*(?:\.\d+)?)\s*(?:m2|m\u00b2|sqm|square meters?)"
    area_line = re.search(r"(?:Area|Certified Area)[:\s]+" + size_pattern, listing.description, re.I)
    size_match = area_line or re.search(size_pattern, listing.description, re.I)
    if size_match:
        raw = size_match.group(1).replace(",", "")
        try:
            val = float(raw)
            if val > 0:
                listing.size_m2 = val
        except ValueError:
            pass

    if listing.price_usd and listing.size_m2:
        listing.price_per_m2_usd = round(listing.price_usd / listing.size_m2, 2)

    # --- Property type from breadcrumb link e.g. /for-sale/land ---
    type_link = soup.find("a", href=re.compile(r"/for-sale/(land|house|apartment|villa|townhouse)$"))
    if type_link:
        listing.property_type = type_link.get_text(strip=True)
    elif "type" in url or True:
        # fallback: infer from URL path segment used when crawling
        for t in TYPE_SLUGS:
            if f"/{t}" in url or (ld.get("name", "").lower().find(t) != -1):
                listing.property_type = t.capitalize()
                break

    # --- Contact: extract a real phone/Zalo number from the description text,
    # not the raw JSON-LD blob ---
    listing.contact = extract_contact_phone(listing.description)

    listing.latitude, listing.longitude = extract_coords_from_map(soup)

    return listing


def dedupe_listings(listings: list[Listing]) -> list[Listing]:
    """Several agents on this site repost the same physical plot under
    different titles/URLs. Collapse obvious duplicates: same price + same
    size + same district. Keeps the most recently scraped copy."""
    seen: dict[tuple, Listing] = {}
    for l in listings:
        key = (l.price_usd, l.size_m2, l.district)
        if key == (None, None, "") :
            seen[l.url] = l  # not enough info to dedupe safely, keep as-is
            continue
        seen[key] = l
    return list(seen.values())


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
    before = len(listings)
    listings = dedupe_listings(listings)
    if before != len(listings):
        print(f"\nDe-duplicated: {before} -> {len(listings)} listings "
              f"(removed {before - len(listings)} likely reposts)")
    save_outputs(listings, args.outdir)


if __name__ == "__main__":
    main()
