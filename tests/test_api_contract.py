"""API contract tests — FastAPI read-layer over the test database."""

import pytest
from fastapi.testclient import TestClient

import api.routers.admin as admin_router
from api.deps import PERSONA_ORDER
from api.main import app
from engine.repo import add_snapshot, extract_columns, recompute_scores_for_stock
from factories import make_analysis, make_stock, utc, yf_payload


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seeded(db_session):
    """ALPHA: fully analyzed. BETA: snapshot only (unanalyzed). GAMMA: bare."""
    alpha = make_stock(db_session, "ALPHA", universe=["ipo_2025"],
                       sector="IT", company_name="Alpha Ltd", issue_price=50.0)
    payload = yf_payload(price=100.0)
    snap, _ = add_snapshot(db_session, alpha, payload,
                           extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc(-5))
    for i, slug in enumerate(PERSONA_ORDER):
        make_analysis(db_session, alpha, slug, 5 + (i % 3), "HOLD",
                      data_hash=snap.content_hash, snapshot_id=snap.id)
    recompute_scores_for_stock(db_session, alpha)
    db_session.commit()

    beta = make_stock(db_session, "BETA", universe=["ipo_2026"],
                      sector="Pharma", company_name="Beta Ltd")
    bp = yf_payload(price=40.0)
    add_snapshot(db_session, beta, bp, extract_columns(bp["info"]),
                 data_quality="full", captured_at=utc(-3))
    db_session.commit()

    gamma = make_stock(db_session, "GAMMA", universe=["ipo_2026"])
    db_session.commit()
    return {"alpha": alpha, "beta": beta, "gamma": gamma}


# ---------- /health ----------

def test_health(client, db_session):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": True}


# ---------- /api/meta ----------

def test_meta_shape(client, seeded):
    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"stocks_total", "analyzed", "universes", "sectors",
                         "last_job_at", "personas"}
    assert body["stocks_total"] == 3
    assert body["analyzed"] == 1
    assert body["universes"] == {"ipo_2026": 2, "ipo_2025": 1}
    assert body["sectors"] == ["IT", "Pharma"]
    assert [p["slug"] for p in body["personas"]] == PERSONA_ORDER
    assert all({"slug", "display_name", "nationality", "philosophy"} <= set(p)
               for p in body["personas"])


# ---------- /api/stocks ----------

def test_stocks_list_includes_unanalyzed_with_null_score(client, seeded):
    r = client.get("/api/stocks")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    by_symbol = {i["symbol"]: i for i in body["items"]}
    assert set(by_symbol) == {"ALPHA", "BETA", "GAMMA"}
    assert by_symbol["ALPHA"]["composite_score"] is not None
    assert by_symbol["BETA"]["composite_score"] is None
    assert by_symbol["GAMMA"]["composite_score"] is None
    # default sort: composite desc, NULLs last
    assert body["items"][0]["symbol"] == "ALPHA"
    # quote fields flow through from the latest snapshot
    assert by_symbol["BETA"]["current_price"] == 40.0
    assert by_symbol["ALPHA"]["ipo_return_pct"] == 100.0  # 50 -> 100


def test_stocks_universe_filter(client, seeded):
    r = client.get("/api/stocks", params={"universe": "ipo_2026"})
    body = r.json()
    assert body["total"] == 2
    assert {i["symbol"] for i in body["items"]} == {"BETA", "GAMMA"}


def test_stocks_analyzed_only_filter(client, seeded):
    r = client.get("/api/stocks", params={"analyzed_only": "true"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["symbol"] == "ALPHA"


def test_stocks_pagination(client, seeded):
    r1 = client.get("/api/stocks", params={"page": 1, "page_size": 2, "sort": "symbol", "order": "asc"})
    r2 = client.get("/api/stocks", params={"page": 2, "page_size": 2, "sort": "symbol", "order": "asc"})
    b1, b2 = r1.json(), r2.json()
    assert b1["total"] == b2["total"] == 3
    assert [i["symbol"] for i in b1["items"]] == ["ALPHA", "BETA"]
    assert [i["symbol"] for i in b2["items"]] == ["GAMMA"]
    assert b1["page"] == 1 and b1["page_size"] == 2


# ---------- /api/stocks/{symbol} ----------

def test_stock_detail_council_order(client, seeded):
    r = client.get("/api/stocks/ALPHA")
    assert r.status_code == 200
    body = r.json()
    council = body["council"]
    assert [m["persona"] for m in council] == PERSONA_ORDER
    assert all(m["score"] is not None for m in council)
    assert body["identity"]["symbol"] == "ALPHA"
    assert body["composite"]["composite_score"] is not None
    assert body["composite"]["total_personas"] == 10
    assert len(body["conviction_history"]) == 1


def test_stock_detail_unanalyzed_has_empty_council_slots(client, seeded):
    r = client.get("/api/stocks/beta")  # lowercase: route upper-cases
    assert r.status_code == 200
    council = r.json()["council"]
    assert [m["persona"] for m in council] == PERSONA_ORDER
    assert all(m["score"] is None for m in council)


def test_stock_detail_unknown_404(client, seeded):
    r = client.get("/api/stocks/NOPE")
    assert r.status_code == 404
    assert "NOPE" in r.json()["detail"]


# ---------- /api/admin/overview ----------

def test_admin_overview_shape(client, seeded):
    r = client.get("/api/admin/overview")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"stocks_total", "by_status", "snapshots", "analyses_total",
                         "scored", "analysis_backlog", "coverage", "freshness",
                         "recent_jobs"}
    assert body["stocks_total"] == 3
    assert body["by_status"] == {"active": 3}
    assert body["snapshots"] == {"yfinance": 2, "screener": 0}
    assert body["analyses_total"] == 10
    assert body["scored"] == 1
    assert body["analysis_backlog"] == 2
    assert set(body["freshness"]) == {"fresh_24h", "stale_7d", "older"}
    assert body["freshness"]["fresh_24h"] == 2
    coverage = {c["universe"]: c for c in body["coverage"]}
    assert coverage["ipo_2025"]["analyzed"] == 1
    assert coverage["ipo_2026"]["backlog"] == 2


# ---------- POST /api/admin/jobs/run ----------

@pytest.fixture()
def admin_auth(monkeypatch):
    """Token auth (TestClient's host is 'testclient', not localhost) and a
    Popen guard so no test can ever launch a real engine subprocess."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(admin_router.subprocess, "Popen",
                        lambda *a, **kw: calls.append((a, kw)))
    return {"headers": {"X-Admin-Token": "test-token"}, "calls": calls}


def test_jobs_run_rejects_bare_analyze(client, db_session, admin_auth):
    r = client.post("/api/admin/jobs/run", json={"job": "analyze"},
                    headers=admin_auth["headers"])
    assert r.status_code == 400
    assert "limit" in r.json()["detail"]
    assert admin_auth["calls"] == []


def test_jobs_run_rejects_oversized_analyze_limit(client, db_session, admin_auth):
    r = client.post("/api/admin/jobs/run",
                    json={"job": "analyze", "args": {"limit": 9999}},
                    headers=admin_auth["headers"])
    assert r.status_code == 400
    assert admin_auth["calls"] == []


def test_jobs_run_rejects_unknown_job(client, db_session, admin_auth):
    r = client.post("/api/admin/jobs/run", json={"job": "drop_all_tables"},
                    headers=admin_auth["headers"])
    assert r.status_code == 400
    assert "Unknown job" in r.json()["detail"]
    assert admin_auth["calls"] == []


def test_jobs_run_requires_valid_token(client, db_session, admin_auth):
    r = client.post("/api/admin/jobs/run", json={"job": "status"},
                    headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    assert admin_auth["calls"] == []


def test_jobs_run_launches_allowed_job(client, db_session, admin_auth):
    r = client.post("/api/admin/jobs/run", json={"job": "status"},
                    headers=admin_auth["headers"])
    assert r.status_code == 200
    assert r.json() == {"launched": True, "job": "status"}
    assert len(admin_auth["calls"]) == 1
    cmd = admin_auth["calls"][0][0][0]
    assert cmd[-1] == "status" and "engine.run" in cmd
