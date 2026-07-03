# Upcoming-IPO (pre-listing) data source

Consumed by `engine/ingest/upcoming.py` (jobs `discover_upcoming` / `promote_upcoming`).
Evaluated 2026-07-03 by fetching each candidate and inspecting the served HTML.

## Chosen source: ipowatch.in

- **URL:** https://ipowatch.in/upcoming-ipo-calendar-ipo-list/
- **Why:** plain server-rendered WordPress HTML — two clean `<table>` elements
  (mainboard + SME), no JavaScript rendering, no anti-bot, fetches fine with a
  desktop User-Agent (HTTP 200, ~570 KB). Covers both segments on one page and
  is updated within hours of DRHP/price-band announcements.

### Tables and fields

| Table | Headers | Segment |
|---|---|---|
| 1 | `Company, IPO Date, IPO Size, IPO Price Band, Application` | mainboard |
| 2 | `IPO, Date, IPO Size, IPO Price Band, Platform, Application` | SME (`Platform` = "NSE SME" / "BSE SME") |

Fields extracted per row:
- **company_name** — cell text (trailing " IPO" stripped); the row link
  (`https://ipowatch.in/<slug>-ipo/`) is kept as `source_url`.
- **open/close dates** — "IPO Date" cell, e.g. `8-10 July`, cross-month
  `30-2 July` (= 30 Jun – 2 Jul), cross-year `31-2 Jan`. **The year is omitted**
  — the parser picks the year that puts the close date nearest today.
- **price band upper** — `₹161 to ₹170` → 170; single `₹110` → 110;
  placeholder `₹[.] to ₹[.]` (band not yet announced) → None.
- **expected listing date** — NOT a column. Derived as close + 6 calendar days
  (SEBI T+3 working days). Always tentative; refreshed on every scrape.
- **exchange** — SME `Platform` column → NSE/BSE; unknown for mainboard.

### Robustness caveats

- The page lists the **whole year's IPOs** (past ones included) — the scraper
  drops rows whose issue closed more than 7 days ago; the listed-IPO discovery
  job (`engine/ingest/discover.py`, screener.in) owns those.
- No symbol/ISIN pre-listing anywhere — we mint provisional `UPC_<SLUG>`
  symbols and resolve real symbols at promotion time via the screener.in
  search API (`https://www.screener.in/api/company/search/?q=<name>`; a
  `/company/id/<n>/` result URL means "known but not listed yet").
- Company names can differ from the eventual listed name (brand vs legal name,
  e.g. "Waterways Leisure Tourism" lists as Cordelia Cruises) — promotion uses
  fuzzy name matching (difflib, threshold 0.90) with suffix stripping.
- Header names/column order may change — the scraper maps columns by header
  keywords ("company"/"ipo", "date", "price", "platform"), not fixed indices.
- Year inference around New Year relies on the "nearest to today" rule; rows
  more than ~6 months stale are filtered out anyway.

## Rejected / fallback sources

1. **Chittorgarh** (the classic retail source) — the rich report pages, e.g.
   `https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/`,
   migrated to Next.js: data ships inside the React Server Component flight
   payload, **zero HTML tables** — parsing it means reverse-engineering
   serialized RSC chunks (very fragile). The old JSON API
   (`webnodejs.chittorgarh.com/cloud/report/data-read/...`) now rejects
   unauthenticated param formats. **Usable fallback:** the legacy dashboard
   `https://www.chittorgarh.com/ipo/ipo_dashboard.asp` still serves a real
   HTML table (Company + Issue Date + per-IPO detail links `/ipo/<slug>/<id>/`),
   but lacks price band / listing date at list level — a fallback would need a
   per-IPO detail-page fetch. There is a separate SME dashboard.
2. **NSE** (`https://www.nseindia.com/market-data/all-upcoming-issues-ipo`) —
   authoritative but the page is fully JS-rendered (empty table skeletons in
   HTML) and the backing API requires a cookie handshake plus rotating
   anti-bot headers; breaks constantly from cron. Not worth it for a daily job.
3. **Moneycontrol** — heavy markup, interstitial ads/consent walls, frequent
   layout churn; no advantage over ipowatch.

**Fallback order if ipowatch breaks:** Chittorgarh dashboard (+ detail pages),
then Moneycontrol. The scraper returns `[]` on fetch failure (never raises), so
a broken source shows up as `scraped=0` in the `discover_upcoming` job stats —
alert on that.

## How the data lands in the DB (no schema changes)

- `Stock(symbol="UPC_<SLUG>", status="upcoming", universe=["ipo_upcoming"],
  listing_date=<expected, tentative ISO date>, issue_price=<band upper>)`
- Full scraped payload appended as a hash-gated `StockSnapshot`
  (`source="ipowatch"`, `data_quality="pre_listing"`, payload in `ipo_data`)
  — invisible to the analysis loop, which requires `data_quality="full"`.
- Post-listing: `promote_listed()` merges into the discovery-registered row
  (shell → `status="listed_merged"`) or resolves a real symbol
  (→ `status="new"`) or parks the row (`status="withdrawn"`).
