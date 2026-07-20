"""The single boundary where yfinance-native scales become canonical units.

yfinance is inconsistent: returnOnEquity and revenueGrowth are fractions
(0.39 = 39%), while debtToEquity is already a percentage (637.09 = 6.37x).
Storing all three raw and letting each consumer guess produced three
separate display bugs. Convert once, here.

Canonical units:
  roe             percent  (39.4 means 39.4%)
  revenue_growth  percent  (18.5 means 18.5%)
  debt_to_equity  ratio    (0.098 means 0.098x)
"""

FRACTION_TO_PERCENT = ("roe", "revenue_growth")
PERCENT_TO_RATIO = ("debt_to_equity",)


def normalize_quote(raw: dict) -> dict:
    out = dict(raw)
    for key in FRACTION_TO_PERCENT:
        v = out.get(key)
        if v is not None:
            out[key] = v * 100.0
    for key in PERCENT_TO_RATIO:
        v = out.get(key)
        if v is not None:
            out[key] = v / 100.0
    return out
