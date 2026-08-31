# Da Nang Land Ledger

A small scraper + viewer for DanangMLS listings, so you can search by
district, price, and size instead of me hand-searching page by page.

## What's here

- `scraper.py` — crawls danangmls.com and saves listings to `data/listings.json` / `data/listings.csv`
- `viewer.html` — open this in any browser; filter by district/type/price/size, view as a table or a map
- `data/sample_listings.json` — the handful of real listings I already gathered by hand in this chat, so the viewer has something to show immediately

**Important: I can't run the scraper from this chat.** This sandbox has no
live internet access, so `scraper.py` is built and syntax-checked but not
run — you'll need Python on your own machine.

## 1. Run the scraper

```bash
pip install requests beautifulsoup4

# Land listings in Ngu Hanh Son (closest district to your target beaches)
python3 scraper.py --district ngu-hanh-son --type land --pages 8

# Everything, no filters, first 15 pages
python3 scraper.py --pages 15

# Land anywhere, under $190,000 (~5 billion VND)
python3 scraper.py --type land --max-price-usd 190000 --pages 15
```

This writes `data/listings.json` and `data/listings.csv`, plus a dated
snapshot in `data/history/` each time you run it — so if you re-run it
weekly, you'll build up a record of what's new and what's sold.

Options:

| Flag | What it does |
|---|---|
| `--district` | `hai-chau`, `thanh-khe`, `son-tra`, `ngu-hanh-son`, `cam-le`, `lien-chieu`, `hoi-an` |
| `--type` | `land`, `house`, `apartment`, `villa`, `townhouse` |
| `--max-price-usd` | skip anything above this price |
| `--pages` | how many index pages to crawl (each page ~10-15 listings) |
| `--delay` | seconds between requests, default 1.5 — keep this polite |
| `--outdir` | where to save output, default `./data` |

It's deliberately slow and polite: one request at a time with a delay, a
normal browser user-agent, and it stops itself if the site starts
rate-limiting rather than hammering it. Please don't strip the delay out.

## 2. Browse the results

Open `viewer.html` in a browser (just double-click it, no server needed).
Click **Load listings.json** and pick the file the scraper just made.

From there you can:
- Filter by district, property type, price range, size range
- Sort by price, price/m², size, or how recently it was listed
- Switch between a sortable **table** and a **map** view (map uses the
  coordinates DanangMLS embeds on each listing page, where available —
  not every listing has them)
- Click any listing to open the real page and contact the agent

It's a static HTML file — no data leaves your machine, nothing is uploaded
anywhere.

## Notes on data quality

- "Listed X ago" is DanangMLS's own relative date; the scraper converts it
  to an approximate ISO date. Treat "2 months ago" as roughly accurate,
  not exact.
- Coordinates only appear if the listing page embeds a map — plenty of
  listings won't have them, and those just won't show up in Map view.
- This only covers DanangMLS. Dot Property and AsiaVillas have more
  volume but messier/duplicated listings (I ran into the same plot priced
  three different ways there) — if you want, I can build a second scraper
  for one of those once this one's working for you, with de-duplication
  logic based on address + size.
- Always confirm price and availability directly with the agent before
  relying on anything here — scraped listings go stale.
