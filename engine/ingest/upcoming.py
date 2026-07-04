"""
Upcoming-IPO (pre-listing) pipeline — track Indian IPOs BEFORE they list, then
auto-promote them into the live pipeline when they list.

Source: ipowatch.in upcoming-IPO calendar (static HTML, mainboard + SME tables).
See data/sources/upcoming_README.md for the source evaluation and fallbacks.

Lifecycle (all within the existing schema — no model changes):
  scrape_upcoming()   -> parse the calendar into normalized entry dicts
  register_upcoming() -> Stock rows with status="upcoming", tag "ipo_upcoming",
                         provisional symbol "UPC_<SLUG>", listing_date = expected
                         (tentative) listing date, issue_price = price-band upper.
                         Each run also appends a hash-gated StockSnapshot
                         (source="ipowatch", data_quality="pre_listing") holding
                         the full scraped payload for provenance.
  promote_listed()    -> once the expected listing date passes, either merge into
                         a listed twin already registered by the daily discovery
                         job (status -> "listed_merged"), resolve a real symbol
                         via screener.in search (status -> "new", so the normal
                         fetch loop takes over), or park as "withdrawn" after 60
                         days past the expected listing with no trace.

Safety: status="upcoming"/"listed_merged"/"withdrawn" rows are invisible to the
rest of the engine — yf_refresh targets ("active","new"), screener enrich targets
"active", and the analysis engine only considers stocks with a data_quality="full"
snapshot. This module is append-only on real (listed) rows: it only fills missing
IPO metadata, never overwrites.
"""

import re
import time
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select

from db.models import Stock
from engine.repo import add_snapshot, add_universe_tag, job_run

IPOWATCH_URL = "https://ipowatch.in/upcoming-ipo-calendar-ipo-list/"
SCREENER_SEARCH_URL = "https://www.screener.in/api/company/search/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
REQUEST_DELAY = 1.5          # polite pause between outbound requests
LISTING_LAG_DAYS = 6         # close -> listing is T+3 working days; ~6 calendar days
KEEP_PAST_CLOSE_DAYS = 7     # keep rows whose issue closed up to N days ago (not yet listed)
STALE_UNKNOWN_DAYS = 45      # unknown listing date: consider due after N days tracked
WITHDRAWN_AFTER_DAYS = 60    # days past expected listing before we park as withdrawn

UNIVERSE_TAG = "ipo_upcoming"
PROVISIONAL_PREFIX = "UPC_"

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


# ---------- normalization helpers ----------

def slugify_symbol(company_name: str) -> str:
    """'Knack Packaging Ltd' -> 'UPC_KNACK_PACKAGING' (fits the 64-char symbol column)."""
    base = _norm_name(company_name)
    slug = re.sub(r"[^A-Z0-9]+", "_", base.upper()).strip("_")
    return (PROVISIONAL_PREFIX + slug)[:60]


def _norm_name(name: str) -> str:
    """Normalize a company name for matching: casefold, strip punctuation + boilerplate suffixes."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    s = re.sub(r"\b(limited|ltd|pvt|private|ipo)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _name_ratio(a: str, b: str) -> float:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _month_num(word: str):
    return _MONTHS.get((word or "")[:3].lower())


def _pick_year(day: int, month: int, today: date):
    """The calendar omits the year; pick the year that puts the date nearest today."""
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def parse_date_range(text: str, today: date | None = None):
    """
    Parse ipowatch 'IPO Date' cells into (open_date, close_date) date objects.
    Formats seen: '8-10 July', '30-2 July' (cross-month: 30 Jun - 2 Jul),
    '31-2 Jan' (cross-year), '29 Jun - 01 Jul'. Returns (None, None) when TBA.
    """
    today = today or date.today()
    text = (text or "").strip()

    # '29 Jun - 01 Jul' (explicit months on both sides)
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)", text)
    if m:
        m1, m2 = _month_num(m.group(2)), _month_num(m.group(4))
        if m1 and m2:
            close = _pick_year(int(m.group(3)), m2, today)
            if close:
                open_year = close.year - 1 if (m1 == 12 and m2 == 1) else close.year
                try:
                    return date(open_year, m1, int(m.group(1))), close
                except ValueError:
                    return None, close
        return None, None

    # '8-10 July' / '30-2 July' (single month names the CLOSE month)
    m = re.match(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)", text)
    if m:
        d1, d2, mon = int(m.group(1)), int(m.group(2)), _month_num(m.group(3))
        if not mon:
            return None, None
        close = _pick_year(d2, mon, today)
        if close is None:
            return None, None
        if d1 <= d2:
            return date(close.year, mon, d1), close
        # cross-month: open falls in the previous month
        prev_last = close.replace(day=1) - timedelta(days=1)
        try:
            return prev_last.replace(day=d1), close
        except ValueError:
            return None, close

    # single date '14 July' — treat as close date
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)", text)
    if m and _month_num(m.group(2)):
        close = _pick_year(int(m.group(1)), _month_num(m.group(2)), today)
        return None, close

    return None, None


def parse_band_upper(text: str):
    """'₹161 to ₹170' -> 170.0; '₹110' -> 110.0; '₹[.] to ₹[.]' (TBA) -> None."""
    nums = re.findall(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(nums[-1]) if nums else None


# ---------- STEP 1/2: scrape ----------

def _classify_tables(soup):
    """Return [(table, segment)] — the SME table has a 'Platform' column, mainboard doesn't."""
    out = []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if not headers or not any("price" in h for h in headers):
            continue
        segment = "sme" if any("platform" in h for h in headers) else "mainboard"
        out.append((table, headers, segment))
    return out


def _col(headers, *needles):
    for i, h in enumerate(headers):
        if any(n in h for n in needles):
            return i
    return None


def scrape_upcoming(keep_past_close_days: int = KEEP_PAST_CLOSE_DAYS, verbose: bool = True):
    """
    Scrape the ipowatch upcoming-IPO calendar (mainboard + SME tables).

    Returns a list of dicts:
      company_name, expected_symbol (provisional 'UPC_<SLUG>'), open_date,
      close_date, price_band_upper, expected_listing_date (tentative,
      close + ~6 days), segment ('mainboard'/'sme'), exchange, source_url.

    Rows whose issue closed more than `keep_past_close_days` ago are dropped
    (the calendar page also lists the whole year's past IPOs).
    """
    today = date.today()
    try:
        resp = requests.get(IPOWATCH_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        if verbose:
            print(f"[upcoming] ERROR fetching {IPOWATCH_URL}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    entries, seen = [], set()

    for table, headers, segment in _classify_tables(soup):
        i_name = _col(headers, "company", "ipo") or 0
        i_date = _col(headers, "date")
        i_band = _col(headers, "price")
        i_platform = _col(headers, "platform")

        for tr in table.find_all("tr"):
            try:
                cells = tr.find_all("td")
                if len(cells) <= max(i for i in (i_name, i_date, i_band) if i is not None):
                    continue
                company = re.sub(r"\s+IPO$", "", cells[i_name].get_text(" ", strip=True), flags=re.I)
                if not company:
                    continue
                link = cells[i_name].find("a") or tr.find("a")
                open_d, close_d = parse_date_range(
                    cells[i_date].get_text(" ", strip=True) if i_date is not None else "", today)
                if close_d and (today - close_d).days > keep_past_close_days:
                    continue  # historical row; the listed-discovery job owns it now

                platform = (cells[i_platform].get_text(" ", strip=True)
                            if i_platform is not None and len(cells) > i_platform else "")
                exchange = ("NSE" if "nse" in platform.lower()
                            else "BSE" if "bse" in platform.lower() else None)

                symbol = slugify_symbol(company)
                if symbol in seen or symbol == PROVISIONAL_PREFIX.rstrip("_"):
                    continue
                seen.add(symbol)

                entries.append({
                    "company_name": company,
                    "expected_symbol": symbol,
                    "open_date": open_d.isoformat() if open_d else None,
                    "close_date": close_d.isoformat() if close_d else None,
                    "price_band_upper": parse_band_upper(
                        cells[i_band].get_text(" ", strip=True) if i_band is not None else ""),
                    "expected_listing_date": (
                        (close_d + timedelta(days=LISTING_LAG_DAYS)).isoformat() if close_d else None),
                    "segment": segment,
                    "exchange": exchange,
                    "source_url": link.get("href") if link else IPOWATCH_URL,
                })
            except Exception as e:  # one bad row never kills the scrape
                if verbose:
                    print(f"[upcoming] row parse error ({segment}): {e}")

    if verbose:
        n_main = sum(1 for e in entries if e["segment"] == "mainboard")
        print(f"[upcoming] scraped {len(entries)} upcoming IPOs "
              f"({n_main} mainboard, {len(entries) - n_main} SME)")
    return entries


# ---------- STEP 2: register ----------

def _match_upcoming(entry, upcoming_stocks):
    """Fuzzy-match a scraped entry to an existing status='upcoming' row (rename tolerance)."""
    best, best_r = None, 0.0
    for s in upcoming_stocks:
        r = _name_ratio(entry["company_name"], s.company_name or "")
        if r > best_r:
            best, best_r = s, r
    if best is None:
        return None
    if best_r >= 0.90:
        return best
    # weaker name match still accepted when the listing window agrees
    if best_r >= 0.80 and entry.get("expected_listing_date") and best.listing_date:
        try:
            delta = abs((date.fromisoformat(entry["expected_listing_date"])
                         - date.fromisoformat(best.listing_date)).days)
            if delta <= 21:
                return best
        except ValueError:
            pass
    return None


def register_upcoming(verbose: bool = True, entries: list | None = None):
    """
    Register scraped pre-listing IPOs as Stock(status='upcoming') rows, tagged
    'ipo_upcoming'. Idempotent: matches on the provisional symbol OR a fuzzy
    (company_name, listing-window) match, so re-runs update dates/price band
    instead of duplicating. Returns stats {scraped, new, updated, unchanged, ...}.
    """
    if entries is None:
        entries = scrape_upcoming(verbose=verbose)

    with job_run("discover_upcoming", target=UNIVERSE_TAG) as (session, stats):
        all_stocks = session.scalars(select(Stock)).all()
        by_symbol = {s.symbol: s for s in all_stocks}
        upcoming_stocks = [s for s in all_stocks if s.status == "upcoming"]
        listed_named = [s for s in all_stocks
                        if s.status not in ("upcoming", "listed_merged", "withdrawn")
                        and s.company_name]

        new = updated = unchanged = already_listed = 0
        for e in entries:
            stock = by_symbol.get(e["expected_symbol"]) or _match_upcoming(e, upcoming_stocks)

            if stock is None:
                # If the daily listed-discovery job already registered this company
                # (issue closed, listing done), don't create a provisional twin.
                if e["close_date"] and e["close_date"] < date.today().isoformat() and any(
                        _name_ratio(e["company_name"], s.company_name) >= 0.92
                        for s in listed_named):
                    already_listed += 1
                    continue
                stock = Stock(
                    symbol=e["expected_symbol"],
                    company_name=e["company_name"],
                    status="upcoming",
                    universe=[],
                    listing_date=e["expected_listing_date"],
                    issue_price=e["price_band_upper"],
                    exchange=e["exchange"],
                )
                session.add(stock)
                session.flush()
                by_symbol[stock.symbol] = stock
                upcoming_stocks.append(stock)
                new += 1
                if verbose:
                    print(f"  NEW upcoming: {stock.symbol} ({e['company_name']}) "
                          f"expected listing {e['expected_listing_date']} "
                          f"band ₹{e['price_band_upper']}")
            elif stock.status == "upcoming":
                changed = False
                for attr, key in (("listing_date", "expected_listing_date"),
                                  ("issue_price", "price_band_upper"),
                                  ("exchange", "exchange")):
                    val = e.get(key)
                    if val is not None and getattr(stock, attr) != val:
                        setattr(stock, attr, val)
                        changed = True
                if changed:
                    updated += 1
                    if verbose:
                        print(f"  UPDATED: {stock.symbol} -> listing {e['expected_listing_date']} "
                              f"band ₹{e['price_band_upper']}")
                else:
                    unchanged += 1
            else:
                already_listed += 1  # provisional symbol collided with a live row; leave it alone
                continue

            add_universe_tag(session, stock, UNIVERSE_TAG)
            # Provenance: hash-gated snapshot of the raw scraped payload. Invisible to
            # the analysis loop (which requires data_quality="full").
            add_snapshot(session, stock, payload={"upcoming": e}, extracted={},
                         source="ipowatch", fetch_status="success",
                         data_quality="pre_listing", ipo_data=e)

        session.commit()
        stats.update({"scraped": len(entries), "new": new, "updated": updated,
                      "unchanged": unchanged, "already_listed": already_listed})
        if verbose:
            print(f"[upcoming] scraped={len(entries)} new={new} updated={updated} "
                  f"unchanged={unchanged} already_listed={already_listed}")
    return stats


# ---------- STEP 2: promote ----------

def _resolve_via_screener(company_name: str, verbose: bool = True):
    """
    Search screener.in for a freshly-listed company (mirrors discover.py's
    resolution idea). Returns {'symbol', 'yf_symbol', 'nse_symbol', 'screener_url'}
    or None. A '/company/id/<n>/' result means screener knows the company but it
    has no exchange listing yet — i.e. NOT resolvable.
    """
    try:
        resp = requests.get(SCREENER_SEARCH_URL, params={"q": _norm_name(company_name)},
                            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        candidates = resp.json()
    except (requests.RequestException, ValueError) as e:
        if verbose:
            print(f"    screener search failed for {company_name!r}: {e}")
        return None

    best, best_r = None, 0.0
    for c in candidates or []:
        r = _name_ratio(company_name, c.get("name", ""))
        if r > best_r:
            best, best_r = c, r
    if best is None or best_r < 0.80:
        return None

    url = best.get("url") or ""
    if re.match(r"/company/id/\d+", url):
        return None  # known to screener, but not listed yet
    m = re.match(r"/company/([^/]+)/?", url)
    if not m:
        return None
    ident = m.group(1)
    if ident.isdigit():  # BSE-only listing (numeric scrip code)
        return {"symbol": ident, "nse_symbol": None, "yf_symbol": f"{ident}.BO",
                "screener_url": f"https://www.screener.in{url}"}
    return {"symbol": ident, "nse_symbol": ident, "yf_symbol": f"{ident}.NS",
            "screener_url": f"https://www.screener.in{url}"}


def _find_listed_twin(stock, listed_named):
    """Fuzzy company-name match against real (listed) rows registered by discovery."""
    best, best_r = None, 0.0
    for s in listed_named:
        r = _name_ratio(stock.company_name or "", s.company_name)
        if r > best_r:
            best, best_r = s, r
    return best if best_r >= 0.90 else None


def _merge_into(session, twin: Stock, upcoming: Stock, verbose: bool):
    """Copy IPO metadata onto the listed row (only where missing) and retire the shell."""
    if twin.issue_price is None and upcoming.issue_price is not None:
        twin.issue_price = upcoming.issue_price
    if not twin.listing_date and upcoming.listing_date:
        twin.listing_date = upcoming.listing_date
    add_universe_tag(session, twin, UNIVERSE_TAG)
    upcoming.status = "listed_merged"
    if verbose:
        print(f"  MERGED: {upcoming.symbol} -> {twin.symbol} ({twin.company_name})")


def promote_listed(verbose: bool = True):
    """
    Graduate status='upcoming' rows whose expected listing date has passed
    (or that have unknown dates and were registered >45 days ago):
      1. listed twin already in the DB (daily discovery beat us) -> merge,
         mark shell status='listed_merged';
      2. resolvable to a real symbol via screener search -> update
         symbol/yf_symbol, status='new' (the existing fetch loop takes over);
      3. neither, and >60 days past expected listing -> status='withdrawn'.
    Returns stats {checked, due, merged, promoted, withdrawn, pending, errors}.
    """
    with job_run("promote_upcoming", target=UNIVERSE_TAG) as (session, stats):
        now = datetime.now(timezone.utc)
        today = date.today()
        all_stocks = session.scalars(select(Stock)).all()
        by_symbol = {s.symbol: s for s in all_stocks}
        upcoming = [s for s in all_stocks if s.status == "upcoming"]
        listed_named = [s for s in all_stocks
                        if s.status not in ("upcoming", "listed_merged", "withdrawn")
                        and s.company_name]

        merged = promoted = withdrawn = pending = errors = due_count = 0
        promoted_symbols = []
        for s in upcoming:
            try:
                expected = None
                if s.listing_date:
                    try:
                        expected = date.fromisoformat(s.listing_date[:10])
                    except ValueError:
                        expected = None
                first_seen = s.first_seen
                if first_seen is not None and first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
                age_days = (now - first_seen).days if first_seen else 0

                due = (expected is not None and expected <= today) or \
                      (expected is None and age_days > STALE_UNKNOWN_DAYS)
                if not due:
                    pending += 1
                    continue
                due_count += 1

                # 1) listed twin already registered by the daily discovery job
                twin = _find_listed_twin(s, listed_named)
                if twin is not None:
                    _merge_into(session, twin, s, verbose)
                    merged += 1
                    continue

                # 2) resolve a fresh symbol via screener search
                resolved = _resolve_via_screener(s.company_name or "", verbose=verbose)
                time.sleep(REQUEST_DELAY)
                if resolved:
                    existing = by_symbol.get(resolved["symbol"])
                    if existing is not None and existing.id != s.id:
                        _merge_into(session, existing, s, verbose)  # discovery raced us
                        merged += 1
                        continue
                    old = s.symbol
                    s.symbol = resolved["symbol"]
                    s.nse_symbol = resolved["nse_symbol"]
                    s.yf_symbol = resolved["yf_symbol"]
                    s.screener_url = resolved["screener_url"]
                    s.exchange = "NSE" if resolved["yf_symbol"].endswith(".NS") else "BSE"
                    s.status = "new"
                    by_symbol[s.symbol] = s
                    promoted += 1
                    promoted_symbols.append(s.symbol)
                    if verbose:
                        print(f"  PROMOTED: {old} -> {s.symbol} ({s.company_name}), status=new")
                    continue

                # 3) long gone -> withdrawn
                overdue = (today - expected).days if expected else age_days - STALE_UNKNOWN_DAYS
                if overdue > WITHDRAWN_AFTER_DAYS:
                    s.status = "withdrawn"
                    withdrawn += 1
                    if verbose:
                        print(f"  WITHDRAWN: {s.symbol} ({s.company_name}), "
                              f"{overdue}d past expected listing")
                else:
                    pending += 1
            except Exception as e:  # one bad row never kills the run
                errors += 1
                if verbose:
                    print(f"  ERROR promoting {s.symbol}: {e}")

        session.commit()
        stats.update({"checked": len(upcoming), "due": due_count, "merged": merged,
                      "promoted": promoted, "withdrawn": withdrawn, "pending": pending,
                      "errors": errors, "promoted_symbols": promoted_symbols[:50]})
        if verbose:
            print(f"[promote] checked={len(upcoming)} due={due_count} merged={merged} "
                  f"promoted={promoted} withdrawn={withdrawn} pending={pending} errors={errors}")
    return stats
