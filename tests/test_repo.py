"""Unit/DB tests for engine/repo.py — hashing, snapshot gating, stock identity."""

from datetime import datetime, timezone

from sqlalchemy import func, select

from db.models import Stock, StockSnapshot
from engine.repo import (
    add_snapshot,
    add_universe_tag,
    compute_content_hash,
    extract_columns,
    get_or_create_stock,
    universe_contains,
)
from factories import make_stock, yf_payload


# ---------- compute_content_hash ----------

class TestComputeContentHash:
    def test_stable_across_calls(self):
        payload = {"info": {"a": 1, "b": [1, 2, 3]}, "financials": {"x": 1.5}}
        assert compute_content_hash(payload) == compute_content_hash(payload)

    def test_key_order_independent(self):
        a = {"info": {"price": 10, "cap": 20}, "extra": {"n": {"y": 2, "x": 1}}}
        b = {"extra": {"n": {"x": 1, "y": 2}}, "info": {"cap": 20, "price": 10}}
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_different_payloads_differ(self):
        assert compute_content_hash({"a": 1}) != compute_content_hash({"a": 2})

    def test_non_json_values_serialized_via_str(self):
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        h1 = compute_content_hash({"captured": dt})
        h2 = compute_content_hash({"captured": dt})
        assert h1 == h2
        assert h1 != compute_content_hash({"captured": str(dt) + "x"})


# ---------- add_snapshot hash gating ----------

class TestAddSnapshot:
    def test_same_payload_twice_is_single_row(self, db_session):
        stock = make_stock(db_session, "HASHGATE")
        payload = yf_payload()

        snap1, created1 = add_snapshot(db_session, stock, payload,
                                       extract_columns(payload["info"]),
                                       data_quality="full")
        db_session.commit()
        snap2, created2 = add_snapshot(db_session, stock, payload,
                                       extract_columns(payload["info"]),
                                       data_quality="full")
        db_session.commit()

        assert created1 is True
        assert created2 is False
        assert snap2.id == snap1.id
        count = db_session.scalar(
            select(func.count(StockSnapshot.id)).where(StockSnapshot.stock_id == stock.id)
        )
        assert count == 1

    def test_changed_payload_creates_new_row(self, db_session):
        stock = make_stock(db_session, "HASHNEW")
        # vary a FUNDAMENTAL (revenue) — a price-only change no longer re-hashes
        p1, p2 = yf_payload(revenue=1000.0), yf_payload(revenue=2000.0)

        _, created1 = add_snapshot(db_session, stock, p1, extract_columns(p1["info"]))
        _, created2 = add_snapshot(db_session, stock, p2, extract_columns(p2["info"]))
        db_session.commit()

        assert created1 and created2
        count = db_session.scalar(
            select(func.count(StockSnapshot.id)).where(StockSnapshot.stock_id == stock.id)
        )
        assert count == 2

    def test_same_hash_allowed_on_different_stocks(self, db_session):
        s1 = make_stock(db_session, "TWIN1")
        s2 = make_stock(db_session, "TWIN2")
        payload = yf_payload()
        _, c1 = add_snapshot(db_session, s1, payload, extract_columns(payload["info"]))
        _, c2 = add_snapshot(db_session, s2, payload, extract_columns(payload["info"]))
        db_session.commit()
        assert c1 and c2

    def test_extracted_columns_persisted(self, db_session):
        stock = make_stock(db_session, "COLS")
        payload = yf_payload(price=250.0)
        snap, _ = add_snapshot(db_session, stock, payload,
                               extract_columns(payload["info"]), data_quality="full")
        db_session.commit()
        assert snap.current_price == 250.0
        assert snap.market_cap == 50_000_000_000.0
        assert snap.data_quality == "full"


# ---------- get_or_create_stock ----------

class TestGetOrCreateStock:
    def test_idempotent(self, db_session):
        s1 = get_or_create_stock(db_session, "IDEM", company_name="Idem Ltd")
        db_session.commit()
        s2 = get_or_create_stock(db_session, "IDEM")
        db_session.commit()
        assert s1.id == s2.id
        count = db_session.scalar(
            select(func.count(Stock.id)).where(Stock.symbol == "IDEM")
        )
        assert count == 1

    def test_defaults_fill_only_empty_fields(self, db_session):
        s = get_or_create_stock(db_session, "MERGE", company_name="Original Name")
        db_session.commit()
        s2 = get_or_create_stock(
            db_session, "MERGE",
            company_name="Should Not Overwrite",  # existing value wins
            sector="IT",                            # empty -> filled
        )
        db_session.commit()
        assert s2.id == s.id
        assert s2.company_name == "Original Name"
        assert s2.sector == "IT"

    def test_none_defaults_ignored(self, db_session):
        get_or_create_stock(db_session, "NONES", sector="Pharma")
        db_session.commit()
        s = get_or_create_stock(db_session, "NONES", sector=None)
        assert s.sector == "Pharma"

    def test_new_stock_gets_universe_and_active_status(self, db_session):
        s = get_or_create_stock(db_session, "FRESH", universe=["ipo_2026"])
        db_session.commit()
        assert s.status == "active"
        assert s.universe == ["ipo_2026"]


# ---------- add_universe_tag ----------

class TestAddUniverseTag:
    def test_no_duplicate_tag(self, db_session):
        stock = make_stock(db_session, "TAGME", universe=["ipo_2025"])
        add_universe_tag(db_session, stock, "ipo_2025")
        assert stock.universe == ["ipo_2025"]
        add_universe_tag(db_session, stock, "ipo_2026")
        add_universe_tag(db_session, stock, "ipo_2026")
        assert stock.universe == ["ipo_2025", "ipo_2026"]

    def test_handles_none_universe_and_empty_tag(self, db_session):
        stock = make_stock(db_session, "TAGNONE")
        stock.universe = None
        add_universe_tag(db_session, stock, "nse_smallcap")
        assert stock.universe == ["nse_smallcap"]
        add_universe_tag(db_session, stock, "")  # falsy tag is a no-op
        assert stock.universe == ["nse_smallcap"]

    def test_concurrent_different_tags_both_survive(self, db_session):
        """Two sessions tag the same stock with DIFFERENT tags at once — the atomic
        DB-side append means neither is lost (a Python read-modify-write would)."""
        import threading
        from db.base import SessionLocal
        stock = make_stock(db_session, "TAGRACE", universe=[])
        db_session.commit()
        sid = stock.id
        barrier = threading.Barrier(2)
        errors = []

        def worker(tag):
            s = SessionLocal()
            try:
                st = s.get(Stock, sid)
                barrier.wait()
                add_universe_tag(s, st, tag)
                s.commit()
            except Exception as e:  # pragma: no cover
                errors.append(e)
                s.rollback()
            finally:
                s.close()

        ts = [threading.Thread(target=worker, args=(t,)) for t in ("ipo_2026", "nse_smallcap")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, f"concurrent tag raised: {errors}"
        db_session.expire(stock, ["universe"])
        assert set(stock.universe) == {"ipo_2026", "nse_smallcap"}  # neither lost


# ---------- universe_contains ----------

class TestUniverseContains:
    def test_sql_predicate_filters_by_tag(self, db_session):
        make_stock(db_session, "UNIA", universe=["ipo_2025", "nse_smallcap"])
        make_stock(db_session, "UNIB", universe=["ipo_2026"])
        make_stock(db_session, "UNIC", universe=[])
        db_session.commit()

        got = db_session.scalars(select(Stock).where(universe_contains("ipo_2025"))).all()
        assert [s.symbol for s in got] == ["UNIA"]

        got = db_session.scalars(select(Stock).where(universe_contains("ipo_2026"))).all()
        assert [s.symbol for s in got] == ["UNIB"]

        got = db_session.scalars(select(Stock).where(universe_contains("missing"))).all()
        assert got == []


# ---------- extract_columns ----------

class TestExtractColumns:
    def test_none_info(self):
        cols = extract_columns(None)
        assert cols == {
            "current_price": None, "market_cap": None, "pe_ratio": None,
            "roe": None, "debt_to_equity": None, "revenue_growth": None,
        }

    def test_string_numbers_coerced_and_junk_dropped(self):
        cols = extract_columns({
            "currentPrice": "123.5",
            "marketCap": "not-a-number",
            "returnOnEquity": "0.15",
        })
        assert cols["current_price"] == 123.5
        assert cols["market_cap"] is None
        assert cols["roe"] == 0.15

    def test_trailing_pe_used_when_present(self):
        cols = extract_columns({"trailingPE": 18.2, "currentPrice": 100,
                                "epsTrailingTwelveMonths": 2})
        assert cols["pe_ratio"] == 18.2

    def test_pe_computed_from_eps_when_missing(self):
        cols = extract_columns({"currentPrice": 100, "epsTrailingTwelveMonths": 5})
        assert cols["pe_ratio"] == 20.0

    def test_pe_not_computed_for_zero_or_negative_eps(self):
        assert extract_columns(
            {"currentPrice": 100, "epsTrailingTwelveMonths": 0}
        )["pe_ratio"] is None
        assert extract_columns(
            {"currentPrice": 100, "epsTrailingTwelveMonths": -4.2}
        )["pe_ratio"] is None

    def test_price_fallback_chain(self):
        assert extract_columns({"regularMarketPrice": 55})["current_price"] == 55.0
        assert extract_columns({"previousClose": 44})["current_price"] == 44.0

    def test_boolean_values_not_treated_as_numbers(self):
        assert extract_columns({"marketCap": True})["market_cap"] is None
