"""
Indian IPO Analyzer Dashboard

Streamlit dashboard that reads pre-computed JSON files and displays
composite scores, per-persona analysis, with filtering and sorting.
No API calls at runtime.
"""

import json
import os
import subprocess

import pandas as pd
import streamlit as st

from src.utils import load_json
from src.personas import PERSONAS

SCORES_PATH = "data/scores.json"
ANALYSES_DIR = "data/analyses"
STOCKS_DIR = "data/stocks"
AUTO_REFRESH_SECONDS = 60


st.set_page_config(
    page_title="Indian IPO Analyzer",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)


def recompute_scores():
    """Re-run score computation to pick up new analyses."""
    subprocess.run(["python3", "-m", "src.score"], capture_output=True, timeout=30)


def load_scores():
    """Load pre-computed scores (no cache — always fresh)."""
    recompute_scores()
    data = load_json(SCORES_PATH)
    if not data:
        return None
    return data


def load_analysis(symbol, persona_slug):
    """Load a single analysis file (no cache — always fresh)."""
    path = os.path.join(ANALYSES_DIR, symbol, f"{persona_slug}.json")
    return load_json(path)


def score_color(score):
    """Return color for score value."""
    if score >= 70:
        return "#22c55e"  # green
    elif score >= 50:
        return "#eab308"  # yellow
    elif score >= 30:
        return "#f97316"  # orange
    else:
        return "#ef4444"  # red


def rec_color(rec):
    """Return color for recommendation."""
    colors = {"BUY": "#22c55e", "HOLD": "#eab308", "AVOID": "#ef4444"}
    return colors.get(rec, "#6b7280")


def format_market_cap(mc_cr):
    """Format market cap in crores."""
    if mc_cr is None:
        return "N/A"
    if mc_cr >= 10000:
        return f"{mc_cr/1000:,.1f}K Cr"
    return f"{mc_cr:,.0f} Cr"


def main():
    scores_data = load_scores()

    if not scores_data or not scores_data.get("stocks"):
        st.title("Indian IPO Analyzer 2025")
        st.warning("No scores data found. Run the analysis pipeline first:")
        st.code("""
python3 -m src.fetch_ipo_list          # Stage 1: Scrape IPO list
python3 -m src.fetch_stock_data        # Stage 2: Fetch financial data
python3 -m src.analyze                 # Stage 3: Run AI persona analysis
python3 -m src.score                   # Stage 3b: Compute composite scores
        """)
        return

    stocks = scores_data["stocks"]
    df = pd.DataFrame(stocks)

    # ─── Header ───
    st.title("Indian IPO Analyzer 2025")
    st.caption(f"AI-powered analysis through 10 legendary investor personas | Each persona scores 0-10, total out of 100 | Last updated: {scores_data.get('computed_at', 'N/A')[:10]}")

    # ─── Top Metrics ───
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total IPOs Analyzed", len(stocks))
    with col2:
        avg_score = df["composite_score"].mean()
        st.metric("Avg Composite Score", f"{avg_score:.1f}")
    with col3:
        buy_count = sum(1 for s in stocks if s["consensus_recommendation"] == "BUY")
        st.metric("BUY Recommendations", buy_count)
    with col4:
        hold_count = sum(1 for s in stocks if s["consensus_recommendation"] == "HOLD")
        st.metric("HOLD", hold_count)
    with col5:
        avoid_count = sum(1 for s in stocks if s["consensus_recommendation"] == "AVOID")
        st.metric("AVOID", avoid_count)

    st.divider()

    # ─── Sidebar Filters ───
    with st.sidebar:
        st.header("Filters")

        # Score range
        score_range = st.slider(
            "Composite Score Range",
            min_value=0.0,
            max_value=100.0,
            value=(0.0, 100.0),
            step=5.0,
        )

        # Sectors
        all_sectors = sorted(df["sector"].dropna().unique().tolist())
        selected_sectors = st.multiselect(
            "Sectors",
            options=all_sectors,
            default=all_sectors,
        )

        # Recommendation
        rec_options = ["BUY", "HOLD", "AVOID"]
        selected_recs = st.multiselect(
            "Recommendation",
            options=rec_options,
            default=rec_options,
        )

        # Market cap range
        st.subheader("Market Cap (Cr)")
        mcap_values = df["market_cap_cr"].dropna()
        if not mcap_values.empty:
            min_mcap = st.number_input("Min Market Cap (Cr)", value=0, step=100)
            max_mcap = st.number_input("Max Market Cap (Cr)", value=int(mcap_values.max()) + 1, step=100)
        else:
            min_mcap, max_mcap = 0, 999999999

        # Data quality
        quality_options = df["data_quality"].dropna().unique().tolist()
        if quality_options:
            selected_quality = st.multiselect(
                "Data Quality",
                options=quality_options,
                default=quality_options,
            )
        else:
            selected_quality = quality_options

        # Persona filter
        st.subheader("Sort by Persona Score")
        persona_sort = st.selectbox(
            "Sort by",
            options=["Composite Score"] + [PERSONAS[s]["display_name"] for s in PERSONAS],
            index=0,
        )

    # ─── Apply Filters ───
    mask = (
        (df["composite_score"] >= score_range[0])
        & (df["composite_score"] <= score_range[1])
        & (df["sector"].isin(selected_sectors))
        & (df["consensus_recommendation"].isin(selected_recs))
    )

    if selected_quality:
        mask = mask & (df["data_quality"].isin(selected_quality))

    if "market_cap_cr" in df.columns:
        mcap_mask = df["market_cap_cr"].fillna(0)
        mask = mask & (mcap_mask >= min_mcap) & (mcap_mask <= max_mcap)

    filtered_df = df[mask].copy()

    # Sort
    if persona_sort == "Composite Score":
        filtered_df = filtered_df.sort_values("composite_score", ascending=False)
    else:
        # Find persona slug from display name
        slug = None
        for s, p in PERSONAS.items():
            if p["display_name"] == persona_sort:
                slug = s
                break
        if slug:
            filtered_df[f"_sort_{slug}"] = filtered_df["persona_scores"].apply(
                lambda ps: ps.get(slug, 0) if isinstance(ps, dict) else 0
            )
            filtered_df = filtered_df.sort_values(f"_sort_{slug}", ascending=False)

    st.subheader(f"IPO Rankings ({len(filtered_df)} stocks)")

    # ─── Main Table ───
    display_cols = {
        "symbol": "Symbol",
        "company_name": "Company",
        "composite_score": "Score",
        "consensus_recommendation": "Rec",
        "sector": "Sector",
        "market_cap_cr": "MCap (Cr)",
        "pe_ratio": "P/E",
        "ipo_return_pct": "IPO Return %",
        "analysis_coverage": "Analyses",
        "data_quality": "Quality",
    }

    table_df = filtered_df[list(display_cols.keys())].rename(columns=display_cols)

    st.dataframe(
        table_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score",
                min_value=0,
                max_value=100,
                format="%.1f",
            ),
            "MCap (Cr)": st.column_config.NumberColumn(format="%.0f"),
            "P/E": st.column_config.NumberColumn(format="%.1f"),
            "IPO Return %": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

    # ─── Detail Views ───
    st.divider()
    st.subheader("Detailed Analysis")

    for _, row in filtered_df.iterrows():
        symbol = row["symbol"]
        company = row["company_name"]
        score = row["composite_score"]
        rec = row["consensus_recommendation"]
        persona_scores = row["persona_scores"] if isinstance(row["persona_scores"], dict) else {}

        # Expander header with score badge
        with st.expander(f"{symbol} | {company} | Score: {score:.1f} | {rec}"):
            # Stock info columns
            info_col, chart_col = st.columns([1, 2])

            with info_col:
                st.markdown(f"**{company}**")
                st.markdown(f"Sector: {row.get('sector', 'N/A')}")
                st.markdown(f"Industry: {row.get('industry', 'N/A')}")
                if row.get("current_price"):
                    st.markdown(f"Price: INR {row['current_price']:,.2f}")
                if row.get("market_cap_cr"):
                    st.markdown(f"Market Cap: {format_market_cap(row['market_cap_cr'])}")
                if row.get("pe_ratio"):
                    st.markdown(f"P/E: {row['pe_ratio']:.1f}")
                if row.get("issue_price"):
                    st.markdown(f"IPO Price: INR {row['issue_price']}")
                if row.get("ipo_return_pct") is not None:
                    ret = row["ipo_return_pct"]
                    st.markdown(f"IPO Return: {ret:+.1f}%")
                st.markdown(f"Data Quality: {row.get('data_quality', 'N/A')}")

            with chart_col:
                # Score bar chart across personas
                if persona_scores:
                    chart_data = pd.DataFrame([
                        {"Investor": PERSONAS[slug]["display_name"], "Score": sc}
                        for slug, sc in persona_scores.items()
                        if slug in PERSONAS
                    ])
                    if not chart_data.empty:
                        st.bar_chart(
                            chart_data.set_index("Investor"),
                            y="Score",
                            use_container_width=True,
                        )

            # Per-persona analysis tabs
            if persona_scores:
                persona_tabs = st.tabs([
                    PERSONAS[slug]["display_name"]
                    for slug in persona_scores
                    if slug in PERSONAS
                ])

                for tab, slug in zip(persona_tabs, persona_scores):
                    if slug not in PERSONAS:
                        continue
                    with tab:
                        analysis_data = load_analysis(symbol, slug)
                        if analysis_data and "analysis" in analysis_data:
                            a = analysis_data["analysis"]

                            # Score and recommendation
                            s_col, r_col = st.columns(2)
                            with s_col:
                                st.metric("Score", a.get("score", "N/A"))
                            with r_col:
                                st.metric("Recommendation", a.get("recommendation", "N/A"))

                            # Thesis
                            st.markdown(f"**Investment Thesis:** {a.get('investment_thesis', 'N/A')}")

                            # Strengths and risks side by side
                            str_col, risk_col = st.columns(2)
                            with str_col:
                                st.markdown("**Key Strengths:**")
                                for s in a.get("key_strengths", []):
                                    st.markdown(f"- {s}")
                            with risk_col:
                                st.markdown("**Key Risks:**")
                                for r in a.get("key_risks", []):
                                    st.markdown(f"- {r}")

                            # Red flags
                            red_flags = a.get("red_flags", [])
                            if red_flags:
                                st.markdown("**Red Flags:**")
                                for rf in red_flags:
                                    st.markdown(f"- {rf}")

                            # Metrics
                            metrics = a.get("metrics_evaluated", {})
                            if metrics:
                                st.markdown("**Metrics Evaluated:**")
                                m_cols = st.columns(5)
                                for i, (k, v) in enumerate(metrics.items()):
                                    label = k.replace("_", " ").title()
                                    m_cols[i % 5].markdown(f"*{label}:* {v}")

                            # Full analysis (collapsed)
                            detailed = a.get("detailed_analysis", "")
                            if detailed:
                                with st.expander("Full Analysis"):
                                    st.markdown(detailed)
                        else:
                            st.info(f"No analysis available for {PERSONAS[slug]['display_name']}")

    # Auto-refresh while analyses are still running
    total_analyses = sum(len(os.listdir(os.path.join(ANALYSES_DIR, d))) for d in os.listdir(ANALYSES_DIR) if os.path.isdir(os.path.join(ANALYSES_DIR, d))) if os.path.isdir(ANALYSES_DIR) else 0
    if total_analyses < 2210:
        st.sidebar.divider()
        st.sidebar.caption(f"Analysis progress: {total_analyses}/2210")
        st.sidebar.caption(f"Auto-refreshing every {AUTO_REFRESH_SECONDS}s")
        import time
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()


if __name__ == "__main__":
    main()
