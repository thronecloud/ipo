"""
WisdomInvest Dashboard

Multi-page Streamlit dashboard:
  - Home: lightweight sortable/filterable table
  - Detail: full per-persona analysis for a selected stock
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
TOTAL_TARGET = 3740  # 374 stocks x 10 personas


st.set_page_config(
    page_title="WisdomInvest",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Mobile-responsive CSS ───
st.markdown("""
<style>
/* Mobile responsive */
@media (max-width: 768px) {
    .block-container { padding: 1rem 0.5rem !important; }
    [data-testid="stMetric"] { padding: 0.5rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.2rem !important; }
    [data-testid="stMetricLabel"] { font-size: 0.7rem !important; }
    .stTabs [data-baseweb="tab-list"] { gap: 2px; }
    .stTabs [data-baseweb="tab"] { padding: 4px 8px; font-size: 0.75rem; }
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

/* Stock row styling */
.stock-row {
    border-bottom: 1px solid #f0f0f0;
    padding: 4px 0;
}
</style>
""", unsafe_allow_html=True)


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
    """Load a single analysis file."""
    path = os.path.join(ANALYSES_DIR, symbol, f"{persona_slug}.json")
    return load_json(path)


def format_market_cap(mc_cr):
    """Format market cap in crores."""
    if mc_cr is None:
        return "N/A"
    if mc_cr >= 10000:
        return f"{mc_cr/1000:,.1f}K Cr"
    return f"{mc_cr:,.0f} Cr"


def render_detail_page(symbol, scores_data):
    """Render the detailed analysis page for a single stock."""
    # Find the stock in scores
    stock = None
    for s in scores_data["stocks"]:
        if s["symbol"] == symbol:
            stock = s
            break

    if not stock:
        st.error(f"Stock {symbol} not found in scores data.")
        return

    persona_scores = stock.get("persona_scores", {})

    # Header with logo + back
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

    st.title(f"{stock['company_name']}")
    st.caption(f"{symbol} | {stock.get('sector', 'N/A')} | {stock.get('industry', 'N/A')}")

    # Top metrics row
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.metric("Composite Score", f"{stock['composite_score']}/100")
    with col2:
        st.metric("Recommendation", stock["consensus_recommendation"])
    with col3:
        price = stock.get("current_price")
        st.metric("Price", f"INR {price:,.2f}" if price else "N/A")
    with col4:
        st.metric("Market Cap", format_market_cap(stock.get("market_cap_cr")))
    with col5:
        pe = stock.get("pe_ratio")
        st.metric("P/E", f"{pe:.1f}" if pe else "N/A")
    with col6:
        ret = stock.get("ipo_return_pct")
        st.metric("IPO Return", f"{ret:+.1f}%" if ret is not None else "N/A")

    st.divider()

    # Score bar chart across all personas
    if persona_scores:
        st.subheader("Scores by Investor Persona")
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

    st.divider()

    # Per-persona detailed analysis in tabs
    if persona_scores:
        st.subheader("Detailed Persona Analysis")
        persona_tabs = st.tabs([
            f"{PERSONAS[slug]['display_name']} ({persona_scores.get(slug, '?')})"
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
                    s_col, r_col, spacer = st.columns([1, 1, 3])
                    with s_col:
                        st.metric("Score", f"{a.get('score', 'N/A')}/10")
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

                    # Metrics evaluated
                    metrics = a.get("metrics_evaluated", {})
                    if metrics:
                        st.markdown("**Metrics Evaluated:**")
                        m_cols = st.columns(5)
                        for i, (k, v) in enumerate(metrics.items()):
                            label = k.replace("_", " ").title()
                            m_cols[i % 5].markdown(f"*{label}:* {v}")

                    # Full detailed analysis
                    detailed = a.get("detailed_analysis", "")
                    if detailed:
                        with st.expander("Full Analysis"):
                            st.markdown(detailed)
                else:
                    st.info(f"No analysis available for {PERSONAS[slug]['display_name']}")


def render_home_page(scores_data):
    """Render the main rankings table page."""
    stocks = scores_data["stocks"]
    df = pd.DataFrame(stocks)

    # ─── Header ───
    st.caption(f"AI-powered analysis through 10 legendary investor personas | Each persona scores 0-10 | Last updated: {scores_data.get('computed_at', 'N/A')[:10]}")

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

    # ─── Sidebar ───
    with st.sidebar:
        st.markdown("<h2 style='text-align:center; cursor:pointer;'>WisdomInvest</h2>", unsafe_allow_html=True)
        if st.button("Home", use_container_width=True, type="tertiary"):
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

        # Persona selector
        st.subheader("Investor Personas")
        all_persona_names = [PERSONAS[s]["display_name"] for s in PERSONAS]
        selected_persona_names = st.multiselect(
            "Include in score",
            options=all_persona_names,
            default=all_persona_names,
            help="Select which investors' scores to include. Score recalculates as sum of selected personas.",
        )
        selected_persona_slugs = [
            s for s, p in PERSONAS.items()
            if p["display_name"] in selected_persona_names
        ]

        # Sort by
        st.subheader("Sort by")
        sort_options = ["Composite Score"] + [PERSONAS[s]["display_name"] for s in PERSONAS]
        persona_sort = st.selectbox(
            "Sort by",
            options=sort_options,
            index=0,
        )

    # ─── Recompute scores based on selected personas ───
    max_possible = len(selected_persona_slugs) * 10
    if selected_persona_slugs and len(selected_persona_slugs) < len(PERSONAS):
        df["composite_score"] = df["persona_scores"].apply(
            lambda ps: sum(ps.get(slug, 0) for slug in selected_persona_slugs)
            if isinstance(ps, dict) else 0
        )
        score_label = f"Score (out of {max_possible}, {len(selected_persona_slugs)} personas)"
    else:
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

    if len(selected_persona_slugs) < len(PERSONAS):
        st.subheader(f"IPO Rankings ({len(filtered_df)} stocks) -- {len(selected_persona_slugs)} personas selected, max score {max_possible}")
    else:
        st.subheader(f"IPO Rankings ({len(filtered_df)} stocks)")

    # ─── Sortable Table with clickable Symbol/Company ───
    table_df = filtered_df[[
        "symbol", "company_name", "composite_score", "consensus_recommendation",
        "sector", "market_cap_cr", "pe_ratio", "ipo_return_pct", "analysis_coverage",
    ]].copy().reset_index(drop=True)

    # Create link columns pointing to detail page
    # Format: ?stock=SYMBOL~~CompanyName so we can extract both with regex
    table_df["Symbol"] = table_df["symbol"].apply(lambda s: f"?stock={s}")
    table_df["Company"] = table_df.apply(
        lambda r: f"?stock={r['symbol']}~~{r['company_name']}", axis=1
    )

    # Build display dataframe
    display_df = pd.DataFrame({
        "Symbol": table_df["Symbol"],
        "Company": table_df["Company"],
        "Score": table_df["composite_score"],
        "Rec": table_df["consensus_recommendation"],
        "Sector": table_df["sector"],
        "MCap (Cr)": table_df["market_cap_cr"],
        "P/E": table_df["pe_ratio"],
        "IPO Return %": table_df["ipo_return_pct"],
        "Analyses": table_df["analysis_coverage"],
    })

    st.caption("Click Symbol or Company name to view detailed analysis")
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Symbol": st.column_config.LinkColumn(
                "Symbol",
                display_text=r"\?stock=(.*)",
            ),
            "Company": st.column_config.LinkColumn(
                "Company",
                display_text=r"~~(.+)$",
            ),
            "Score": st.column_config.ProgressColumn(
                score_label,
                min_value=0,
                max_value=max_possible,
                format="%.0f",
            ),
            "MCap (Cr)": st.column_config.NumberColumn(format="%.0f"),
            "P/E": st.column_config.NumberColumn(format="%.1f"),
            "IPO Return %": st.column_config.NumberColumn(format="%.1f%%"),
            "Analyses": st.column_config.NumberColumn(format="%d/10"),
        },
    )

    # Auto-refresh while analyses are still running
    total_analyses = sum(
        len(os.listdir(os.path.join(ANALYSES_DIR, d)))
        for d in os.listdir(ANALYSES_DIR)
        if os.path.isdir(os.path.join(ANALYSES_DIR, d))
    ) if os.path.isdir(ANALYSES_DIR) else 0

    if total_analyses < TOTAL_TARGET:
        st.sidebar.divider()
        st.sidebar.caption(f"Analysis progress: {total_analyses}/{TOTAL_TARGET}")
        st.sidebar.caption(f"Auto-refreshing every {AUTO_REFRESH_SECONDS}s")
        import time
        time.sleep(AUTO_REFRESH_SECONDS)
        st.rerun()


def render_landing_page():
    """Render the landing/intro page."""
    st.markdown("""
    <div class="landing-hero">
        <h1>WisdomInvest</h1>
        <p class="subtitle">
            AI-powered stock analysis of 370+ Indian IPOs through the lenses of 10 legendary investors
        </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("#### Data-Driven")
        st.markdown("Financial data from yfinance and screener.in -- P&L, balance sheet, cash flow, ratios for every IPO.")
    with col2:
        st.markdown("#### Multi-Persona AI")
        st.markdown("Each stock analyzed by 10 investor personas with distinct philosophies, from deep value to growth to small-cap hunting.")
    with col3:
        st.markdown("#### Actionable Scores")
        st.markdown("Composite scoring (0-100) with per-persona breakdown. Filter by sector, market cap, recommendation.")

    st.markdown("---")
    st.markdown("#### The 10 Investor Personas")

    st.markdown("""
    <div class="persona-grid">
        <div class="persona-chip">Warren Buffett</div>
        <div class="persona-chip">Charlie Munger</div>
        <div class="persona-chip">Benjamin Graham</div>
        <div class="persona-chip">Peter Lynch</div>
        <div class="persona-chip">Philip Fisher</div>
        <div class="persona-chip">Joel Greenblatt</div>
        <div class="persona-chip">Howard Marks</div>
        <div class="persona-chip">Rakesh Jhunjhunwala</div>
        <div class="persona-chip">Radhakishan Damani</div>
        <div class="persona-chip">Vijay Kedia</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("")
    st.markdown("")

    _, center, _ = st.columns([1, 2, 1])
    with center:
        if st.button("Open Analyzer", use_container_width=True, type="primary"):
            st.query_params["page"] = "app"
            st.rerun()

    st.markdown("---")
    st.caption("Built with Python, Streamlit, Claude AI, yfinance, and screener.in data.")


def main():
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
