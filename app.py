"""
WisdomInvest Dashboard

Multi-page Streamlit dashboard for AI-powered Indian IPO stock analysis:
  - Landing: introduction and overview of the platform
  - Home: sortable/filterable rankings table with composite scores
  - Detail: full per-persona analysis breakdown for a selected stock
"""

import math
import os
import subprocess
import time

import pandas as pd
import streamlit as st

from src.utils import load_json
from src.personas import PERSONAS

SCORES_PATH = "data/scores.json"
ANALYSES_DIR = "data/analyses"
STOCKS_DIR = "data/stocks"
AUTO_REFRESH_SECONDS = 60
TOTAL_TARGET = 3750  # 375 stocks x 10 personas


st.set_page_config(
    page_title="WisdomInvest",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Global CSS ───
st.markdown("""
<style>
/* Mobile responsive */
@media (max-width: 768px) {
    .block-container { padding: 1rem 0.5rem !important; }
    [data-testid="stMetric"] {
        padding: 0.5rem !important;
        min-width: 0 !important;
        overflow: hidden;
    }
    [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
    [data-testid="stMetricLabel"] { font-size: 0.7rem !important; }
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
        flex-wrap: wrap;
    }
    .stTabs [data-baseweb="tab"] { padding: 4px 8px; font-size: 0.75rem; }
    .landing-hero h1 { font-size: 1.8rem !important; }
    .landing-hero .subtitle { font-size: 1rem !important; }
    .landing-features { grid-template-columns: 1fr !important; }
    .persona-grid { grid-template-columns: repeat(2, 1fr) !important; }
}

/* Landing page styles */
.landing-hero {
    text-align: center;
    padding: 3rem 1rem;
    max-width: 800px;
    margin: 0 auto;
}
.landing-hero h1 {
    font-size: 2.5rem;
    margin-bottom: 0.5rem;
}
.landing-hero .subtitle {
    font-size: 1.2rem;
    color: #666;
    margin-bottom: 2rem;
}
.landing-features {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1.5rem;
    max-width: 900px;
    margin: 2rem auto;
    padding: 0 1rem;
}
.feature-card {
    background: #f8f9fa;
    border-radius: 8px;
    padding: 1.5rem;
    text-align: center;
}
.feature-card h3 {
    font-size: 1rem;
    margin-bottom: 0.5rem;
}
.feature-card p {
    font-size: 0.85rem;
    color: #555;
}
.persona-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.75rem;
    max-width: 900px;
    margin: 1rem auto;
    padding: 0 1rem;
}
.persona-chip {
    background: #eef;
    border-radius: 6px;
    padding: 0.5rem 0.75rem;
    text-align: center;
    font-size: 0.85rem;
    font-weight: 500;
}

/* Footer */
.app-footer {
    text-align: center;
    padding: 2rem 1rem 1rem;
    color: #888;
    font-size: 0.8rem;
    border-top: 1px solid #e0e0e0;
    margin-top: 3rem;
}
</style>
""", unsafe_allow_html=True)


def _is_valid_number(val):
    """Return True if val is a finite number (not None, not NaN, not inf)."""
    if val is None:
        return False
    try:
        return math.isfinite(val)
    except (TypeError, ValueError):
        return False


def recompute_scores():
    """Re-run score computation to pick up new analyses."""
    try:
        subprocess.run(
            ["python3", "-m", "src.score"],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def load_scores():
    """Load pre-computed scores (no cache -- always fresh)."""
    recompute_scores()
    data = load_json(SCORES_PATH)
    if not data:
        return None
    return data


def load_analysis(symbol, persona_slug):
    """Load a single analysis JSON file for a given stock and persona."""
    path = os.path.join(ANALYSES_DIR, symbol, f"{persona_slug}.json")
    return load_json(path)


def format_market_cap(mc_cr):
    """Format market cap value in crores with appropriate scale suffix."""
    if not _is_valid_number(mc_cr):
        return "N/A"
    if mc_cr >= 100_000:
        return f"{mc_cr / 100_000:,.2f}L Cr"
    if mc_cr >= 10_000:
        return f"{mc_cr / 1_000:,.1f}K Cr"
    return f"{mc_cr:,.0f} Cr"


def format_price(price):
    """Format price in INR with commas and two decimal places."""
    if not _is_valid_number(price):
        return "N/A"
    return f"INR {price:,.2f}"


def format_pe(pe):
    """Format P/E ratio with one decimal place."""
    if not _is_valid_number(pe):
        return "N/A"
    return f"{pe:.1f}"


def format_return(ret):
    """Format percentage return with sign and one decimal place."""
    if not _is_valid_number(ret):
        return "N/A"
    return f"{ret:+.1f}%"


def render_footer():
    """Render a consistent footer across all pages."""
    st.markdown(
        '<div class="app-footer">'
        "Built with Python, Streamlit, and Claude AI. "
        "Financial data sourced from yfinance and screener.in."
        "</div>",
        unsafe_allow_html=True,
    )


def render_detail_page(symbol, scores_data):
    """Render the detailed analysis page for a single stock."""
    # Find the stock in scores
    stock = None
    for s in scores_data.get("stocks", []):
        if s.get("symbol") == symbol:
            stock = s
            break

    if not stock:
        st.error(f"Stock '{symbol}' not found in scores data.")
        if st.button("Back to Rankings"):
            st.query_params.clear()
            st.query_params["page"] = "app"
            st.rerun()
        return

    persona_scores = stock.get("persona_scores") or {}

    # Navigation header
    nav_left, nav_right = st.columns([1, 4])
    with nav_left:
        if st.button("WisdomInvest", type="tertiary"):
            st.query_params.clear()
            st.rerun()
    with nav_right:
        if st.button("< Back to Rankings"):
            st.query_params.clear()
            st.query_params["page"] = "app"
            st.rerun()

    st.title(stock.get("company_name", symbol))
    st.caption(
        f"{symbol} | "
        f"{stock.get('sector', 'N/A')} | "
        f"{stock.get('industry', 'N/A')}"
    )

    # Top metrics row
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        score_val = stock.get("composite_score", 0)
        st.metric("Composite Score", f"{score_val}/100")
    with col2:
        st.metric("Recommendation", stock.get("consensus_recommendation", "N/A"))
    with col3:
        st.metric("Price", format_price(stock.get("current_price")))
    with col4:
        st.metric("Market Cap", format_market_cap(stock.get("market_cap_cr")))
    with col5:
        st.metric("P/E", format_pe(stock.get("pe_ratio")))
    with col6:
        st.metric("IPO Return", format_return(stock.get("ipo_return_pct")))

    st.divider()

    # Score bar chart across all personas
    if persona_scores:
        st.subheader("Scores by Investor Persona")
        chart_rows = []
        for slug, sc in persona_scores.items():
            if slug in PERSONAS and _is_valid_number(sc):
                chart_rows.append({
                    "Investor": PERSONAS[slug]["display_name"],
                    "Score": sc,
                })
        if chart_rows:
            import altair as alt
            chart_data = pd.DataFrame(chart_rows)
            # Color-code: green (7+), yellow (4-6), red (0-3)
            chart_data["Color"] = chart_data["Score"].apply(
                lambda s: "green" if s >= 7 else ("yellow" if s >= 4 else "red")
            )
            color_scale = alt.Scale(
                domain=["green", "yellow", "red"],
                range=["#22c55e", "#eab308", "#ef4444"],
            )
            chart = alt.Chart(chart_data).mark_bar().encode(
                x=alt.X("Score:Q", scale=alt.Scale(domain=[0, 10]), title="Score (out of 10)"),
                y=alt.Y("Investor:N", sort="-x", title=""),
                color=alt.Color("Color:N", scale=color_scale, legend=None),
                tooltip=["Investor", "Score"],
            ).properties(height=350)
            st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No persona scores available for this stock.")

    st.divider()

    # Per-persona detailed analysis in tabs
    valid_slugs = [slug for slug in persona_scores if slug in PERSONAS]
    if valid_slugs:
        st.subheader("Detailed Persona Analysis")
        tab_labels = [
            f"{PERSONAS[slug]['display_name']} ({persona_scores.get(slug, '?')})"
            for slug in valid_slugs
        ]
        persona_tabs = st.tabs(tab_labels)

        for tab, slug in zip(persona_tabs, valid_slugs):
            with tab:
                analysis_data = load_analysis(symbol, slug)
                if not analysis_data or "analysis" not in analysis_data:
                    st.info(
                        f"No analysis available for "
                        f"{PERSONAS[slug]['display_name']}."
                    )
                    continue

                a = analysis_data["analysis"]

                # Score and recommendation
                s_col, r_col, spacer = st.columns([1, 1, 3])
                with s_col:
                    st.metric("Score", f"{a.get('score', 'N/A')}/10")
                with r_col:
                    st.metric("Recommendation", a.get("recommendation", "N/A"))

                # Thesis
                thesis = a.get("investment_thesis", "")
                if thesis:
                    st.markdown(f"**Investment Thesis:** {thesis}")

                # Strengths and risks side by side
                strengths = a.get("key_strengths", [])
                risks = a.get("key_risks", [])
                if strengths or risks:
                    str_col, risk_col = st.columns(2)
                    with str_col:
                        st.markdown("**Key Strengths:**")
                        if strengths:
                            for item in strengths:
                                st.markdown(f"- {item}")
                        else:
                            st.markdown("_None identified_")
                    with risk_col:
                        st.markdown("**Key Risks:**")
                        if risks:
                            for item in risks:
                                st.markdown(f"- {item}")
                        else:
                            st.markdown("_None identified_")

                # Red flags
                red_flags = a.get("red_flags", [])
                if red_flags:
                    st.markdown("**Red Flags:**")
                    for rf in red_flags:
                        st.markdown(f"- {rf}")

                # Metrics evaluated
                metrics = a.get("metrics_evaluated") or {}
                if metrics:
                    st.markdown("**Metrics Evaluated:**")
                    num_cols = min(len(metrics), 5)
                    m_cols = st.columns(num_cols)
                    for i, (k, v) in enumerate(metrics.items()):
                        label = k.replace("_", " ").title()
                        m_cols[i % num_cols].markdown(f"*{label}:* {v}")

                # Full detailed analysis
                detailed = a.get("detailed_analysis", "")
                if detailed:
                    with st.expander("Full Analysis"):
                        st.markdown(detailed)
    elif not persona_scores:
        pass  # Already showed info message above
    else:
        st.info("No matching persona analyses found.")

    render_footer()


def render_home_page(scores_data):
    """Render the main rankings table page with filters and sortable data."""
    stocks = scores_data.get("stocks", [])
    if not stocks:
        st.warning("No stock data available.")
        return

    df = pd.DataFrame(stocks)

    # ─── Header ───
    computed_at = scores_data.get("computed_at", "")
    display_date = computed_at[:10] if computed_at and len(computed_at) >= 10 else "N/A"
    st.caption(
        f"AI-powered analysis through 10 legendary investor personas | "
        f"Each persona scores 0-10 | Last updated: {display_date}"
    )

    # ─── Top Metrics ───
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Total IPOs Analyzed", f"{len(stocks):,}")
    with col2:
        avg_score = df["composite_score"].mean() if "composite_score" in df.columns else 0
        st.metric("Avg Composite Score", f"{avg_score:.1f}")
    with col3:
        buy_count = sum(
            1 for s in stocks
            if s.get("consensus_recommendation") == "BUY"
        )
        st.metric("BUY Recommendations", f"{buy_count:,}")
    with col4:
        hold_count = sum(
            1 for s in stocks
            if s.get("consensus_recommendation") == "HOLD"
        )
        st.metric("HOLD", f"{hold_count:,}")
    with col5:
        avoid_count = sum(
            1 for s in stocks
            if s.get("consensus_recommendation") == "AVOID"
        )
        st.metric("AVOID", f"{avoid_count:,}")

    st.divider()

    # ─── Sidebar ───
    with st.sidebar:
        st.markdown(
            "<h2 style='text-align:center; cursor:pointer;'>WisdomInvest</h2>",
            unsafe_allow_html=True,
        )
        if st.button("Home", width="stretch", type="tertiary"):
            st.query_params.clear()
            st.rerun()
        st.divider()
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
            max_mcap = st.number_input(
                "Max Market Cap (Cr)",
                value=int(mcap_values.max()) + 1,
                step=100,
            )
        else:
            min_mcap, max_mcap = 0, 999_999_999

        # Data quality
        quality_col_exists = "data_quality" in df.columns
        quality_options = df["data_quality"].dropna().unique().tolist() if quality_col_exists else []
        if quality_options:
            selected_quality = st.multiselect(
                "Data Quality",
                options=quality_options,
                default=quality_options,
            )
        else:
            selected_quality = []

        # Persona selector
        st.subheader("Investor Personas")
        all_persona_names = [PERSONAS[s]["display_name"] for s in PERSONAS]
        selected_persona_names = st.multiselect(
            "Include in score",
            options=all_persona_names,
            default=all_persona_names,
            help="Select which investors' scores to include. "
                 "Score recalculates as sum of selected personas.",
        )
        selected_persona_slugs = [
            s for s, p in PERSONAS.items()
            if p["display_name"] in selected_persona_names
        ]

        # Sort by
        st.subheader("Sort by")
        sort_options = ["Composite Score"] + [
            PERSONAS[s]["display_name"] for s in PERSONAS
        ]
        persona_sort = st.selectbox(
            "Sort by",
            options=sort_options,
            index=0,
            label_visibility="collapsed",
        )

    # ─── Recompute scores based on selected personas ───
    max_possible = len(selected_persona_slugs) * 10
    if selected_persona_slugs and len(selected_persona_slugs) < len(PERSONAS):
        df["composite_score"] = df["persona_scores"].apply(
            lambda ps: sum(ps.get(slug, 0) for slug in selected_persona_slugs)
            if isinstance(ps, dict) else 0
        )
        score_label = (
            f"Score (out of {max_possible}, "
            f"{len(selected_persona_slugs)} personas)"
        )
    else:
        max_possible = len(PERSONAS) * 10
        score_label = "Score (out of 100)"

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
        sort_slug = None
        for s, p in PERSONAS.items():
            if p["display_name"] == persona_sort:
                sort_slug = s
                break
        if sort_slug:
            sort_col = f"_sort_{sort_slug}"
            filtered_df[sort_col] = filtered_df["persona_scores"].apply(
                lambda ps: ps.get(sort_slug, 0) if isinstance(ps, dict) else 0
            )
            filtered_df = filtered_df.sort_values(sort_col, ascending=False)

    # Subheader with count
    if len(selected_persona_slugs) < len(PERSONAS):
        st.subheader(
            f"IPO Rankings ({len(filtered_df):,} stocks) -- "
            f"{len(selected_persona_slugs)} personas selected, "
            f"max score {max_possible}"
        )
    else:
        st.subheader(f"IPO Rankings ({len(filtered_df):,} stocks)")

    # ─── Sortable Table with clickable Symbol/Company ───
    required_cols = [
        "symbol", "company_name", "composite_score", "consensus_recommendation",
        "sector", "market_cap_cr", "pe_ratio", "ipo_return_pct", "analysis_coverage",
    ]
    for col in required_cols:
        if col not in filtered_df.columns:
            filtered_df[col] = None

    table_df = filtered_df[required_cols].copy().reset_index(drop=True)

    # Build display dataframe with clean data
    display_df = pd.DataFrame({
        "Symbol": table_df["symbol"],
        "Company": table_df["company_name"],
        "Score": table_df["composite_score"],
        "Rec": table_df["consensus_recommendation"],
        "Sector": table_df["sector"],
        "MCap (Cr)": table_df["market_cap_cr"],
        "P/E": table_df["pe_ratio"],
        "IPO Return %": table_df["ipo_return_pct"],
        "Analyses": table_df["analysis_coverage"],
    })

    st.caption("Select a stock below to view detailed analysis")

    # Sortable table
    st.dataframe(
        display_df,
        hide_index=True,
        height=735,
        column_config={
            "Score": st.column_config.ProgressColumn(
                score_label,
                min_value=0,
                max_value=max_possible,
                format="%.0f",
            ),
            "MCap (Cr)": st.column_config.NumberColumn(format="%,.0f"),
            "P/E": st.column_config.NumberColumn(format="%.1f"),
            "IPO Return %": st.column_config.NumberColumn(format="%.1f%%"),
            "Analyses": st.column_config.NumberColumn(format="%d/10"),
        },
    )

    # Stock selector for navigation
    symbol_list = table_df["symbol"].tolist()
    company_list = table_df["company_name"].tolist()
    options = [""] + [f"{s} — {c}" for s, c in zip(symbol_list, company_list)]
    selected = st.selectbox("Open detailed analysis", options=options, label_visibility="collapsed")
    if selected:
        sym = selected.split(" — ")[0]
        st.query_params["stock"] = sym
        st.rerun()

    # Auto-refresh while analyses are still running
    total_analyses = 0
    if os.path.isdir(ANALYSES_DIR):
        for d in os.listdir(ANALYSES_DIR):
            dirpath = os.path.join(ANALYSES_DIR, d)
            if os.path.isdir(dirpath):
                total_analyses += len(os.listdir(dirpath))

    if total_analyses < TOTAL_TARGET:
        st.sidebar.divider()
        st.sidebar.caption(
            f"Analysis progress: {total_analyses:,}/{TOTAL_TARGET:,}"
        )
        st.sidebar.caption(f"Auto-refreshing every {AUTO_REFRESH_SECONDS}s")
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()

    render_footer()


def render_landing_page():
    """Render the landing/intro page with platform overview."""
    st.markdown("""
    <div class="landing-hero">
        <h1>WisdomInvest</h1>
        <p class="subtitle">
            AI-powered stock analysis of 370+ Indian IPOs through the lenses
            of 10 legendary investors
        </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("#### Data-Driven")
        st.markdown(
            "Financial data from yfinance and screener.in -- "
            "P&L, balance sheet, cash flow, and ratios for every IPO."
        )
    with col2:
        st.markdown("#### Multi-Persona AI")
        st.markdown(
            "Each stock analyzed by 10 investor personas with distinct "
            "philosophies, from deep value to growth to small-cap hunting."
        )
    with col3:
        st.markdown("#### Actionable Scores")
        st.markdown(
            "Composite scoring (0-100) with per-persona breakdown. "
            "Filter by sector, market cap, and recommendation."
        )

    st.markdown("---")
    st.markdown("#### The 10 Investor Personas")

    # Build persona grid dynamically from PERSONAS dict
    persona_chips = "".join(
        f'<div class="persona-chip">{p["display_name"]}</div>'
        for p in PERSONAS.values()
    )
    st.markdown(
        f'<div class="persona-grid">{persona_chips}</div>',
        unsafe_allow_html=True,
    )

    st.markdown("")
    st.markdown("")

    _, center, _ = st.columns([1, 2, 1])
    with center:
        if st.button("Open Analyzer", width="stretch", type="primary"):
            st.query_params["page"] = "app"
            st.rerun()

    render_footer()


def main():
    """Main entry point: route to the appropriate page based on query params."""
    scores_data = load_scores()

    # Route based on query params
    page = st.query_params.get("page")
    selected_stock = st.query_params.get("stock")

    if selected_stock and scores_data and scores_data.get("stocks"):
        render_detail_page(selected_stock, scores_data)
    elif page == "app" and scores_data and scores_data.get("stocks"):
        render_home_page(scores_data)
    elif page == "app":
        st.title("WisdomInvest")
        st.warning("No scores data found. Run the analysis pipeline first.")
        st.code("python run_pipeline.py --stages 1,2,3,4")
    else:
        render_landing_page()


if __name__ == "__main__":
    main()
