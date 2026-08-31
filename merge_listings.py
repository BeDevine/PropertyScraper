#!/usr/bin/env python3
"""
Merge per-source listing files into one combined data/listings.json.

Each scraper (scraper.py for DanangMLS, scraper_fazwaz.py for FazWaz, and
any future ones) writes its own data/listings_<source>.json. This script
just concatenates whichever of those files exist into a single
data/listings.json, which is what viewer.html loads.

No cross-source de-duplication is done here -- if the same physical plot
is listed on two different sites, it will appear twice, tagged with two
different "source" values. That's a known limitation, not a bug: safely
matching listings across sites (different photos, different phrasing,
maybe slightly different prices) needs fuzzier logic than we have
confidence in yet.
"""
import json
import sys
from pathlib import Path

def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
    source_files = sorted(outdir.glob("listings_*.json"))
    # Exclude anything already inside history/ or a combined file if re-run
    source_files = [f for f in source_files if f.parent == outdir]

    if not source_files:
        print("No per-source listings_*.json files found -- nothing to merge.", file=sys.stderr)
        sys.exit(1)

    combined = []
    for f in source_files:
        with open(f, encoding="utf-8") as fh:
            records = json.load(fh)
        print(f"  {f.name}: {len(records)} listings")
        combined.extend(records)

    out_path = outdir / "listings.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(combined, fh, ensure_ascii=False, indent=2)

    print(f"\nMerged {len(combined)} total listings from {len(source_files)} source(s) -> {out_path}")

if __name__ == "__main__":
    main()
