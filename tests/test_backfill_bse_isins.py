"""BSE ISIN backfill — scrip→ISIN mapping from the bhavcopy, and the safety
refusal of any scrip code that maps to two different ISINs.

The mapping logic is pure over its inputs (bhavcopy text + target list), so these
tests never touch the network or the DB — the wrong-ISIN risk is guarded here.
"""

from pathlib import Path

from scripts.backfill_bse_isins import (
    merge_bhavcopy_maps,
    parse_bse_scrip_isin,
    propose_mappings,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _bhav() -> str:
    return (FIXTURES / "bse_bhavcopy.csv").read_text()


def test_parse_scrip_and_ticker_maps():
    scrip, tsym = parse_bse_scrip_isin(_bhav())
    assert scrip["500325"] == "INE12345A01011"
    assert scrip["500327"] == "INE55555Y00000"
    # ticker map is keyed uppercase
    assert tsym["BSEONLY"] == "INE55555Y00000"


def test_numeric_symbol_resolves_via_scrip_code():
    scrip, tsym, conflicts = merge_bhavcopy_maps([_bhav()])
    proposals, ambiguous, unmatched = propose_mappings(
        [("500325", "Ather"), ("999999", "Nope")], scrip, tsym, {}, conflicts)
    assert [(p.symbol, p.isin, p.source) for p in proposals] == [
        ("500325", "INE12345A01011", "bhavcopy")]
    assert ambiguous == []
    assert unmatched == ["999999"]


def test_amfi_is_fallback_for_scrips_absent_from_bhavcopy():
    scrip, tsym, conflicts = merge_bhavcopy_maps([_bhav()])
    proposals, _, unmatched = propose_mappings(
        [("512345", "AmfiOnly")], scrip, tsym, {"512345": "INE777A01019"}, conflicts)
    assert [(p.symbol, p.isin, p.source) for p in proposals] == [
        ("512345", "INE777A01019", "amfi")]
    assert unmatched == []


def test_conflicting_isin_for_one_scrip_is_refused_not_guessed():
    # Same scrip code, two ISINs across two days (an ISIN revision on a split) →
    # the code is poisoned and must NEVER be proposed. Wrong ISIN = wrong company.
    day1 = ("TradDt,FinInstrmTp,FinInstrmId,ISIN,TckrSymb\n"
            "2026-07-20,STK,540000,INE111A01011,FOO\n")
    day2 = ("TradDt,FinInstrmTp,FinInstrmId,ISIN,TckrSymb\n"
            "2026-07-19,STK,540000,INE111A01029,FOO\n")
    scrip, tsym, conflicts = merge_bhavcopy_maps([day1, day2])
    assert "540000" in conflicts
    assert "540000" not in scrip
    proposals, ambiguous, unmatched = propose_mappings(
        [("540000", "Foo")], scrip, tsym, {}, conflicts)
    assert proposals == []
    assert ambiguous == ["540000"]           # refused, not silently dropped


def test_stable_scrip_across_days_is_not_a_conflict():
    day1 = ("TradDt,FinInstrmTp,FinInstrmId,ISIN,TckrSymb\n"
            "2026-07-20,STK,540001,INE222A01011,BAR\n")
    day2 = ("TradDt,FinInstrmTp,FinInstrmId,ISIN,TckrSymb\n"
            "2026-07-19,STK,540001,INE222A01011,BAR\n")
    scrip, _, conflicts = merge_bhavcopy_maps([day1, day2])
    assert conflicts == set()
    assert scrip["540001"] == "INE222A01011"
