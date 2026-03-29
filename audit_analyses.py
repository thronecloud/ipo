#!/usr/bin/env python3
"""
Comprehensive data quality audit of AI-generated investor persona analyses.
Checks 3740 analysis files across 374 stocks and 10 personas.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
import statistics

ANALYSES_DIR = Path("/workspace/india/data/analyses")

EXPECTED_PERSONAS = [
    "benjamin_graham",
    "charlie_munger",
    "howard_marks",
    "joel_greenblatt",
    "peter_lynch",
    "philip_fisher",
    "radhakishan_damani",
    "rakesh_jhunjhunwala",
    "vijay_kedia",
    "warren_buffett",
]

VALID_RECOMMENDATIONS = {"BUY", "HOLD", "AVOID"}

VALID_METRICS = {
    "moat_strength": {"strong", "moderate", "weak", "none", "insufficient_data"},
    "management_quality": {"excellent", "good", "adequate", "poor", "insufficient_data"},
    "financial_health": {"excellent", "good", "adequate", "poor", "insufficient_data"},
    "valuation": {"undervalued", "fair", "overvalued", "extremely_overvalued", "insufficient_data"},
    "growth_potential": {"exceptional", "good", "moderate", "low", "insufficient_data"},
}

# Score thresholds for recommendation alignment (on 0-10 scale)
# 0-3 -> AVOID, 4-5 -> HOLD, 6-10 -> BUY
# We flag "extreme" mismatches only
def is_extreme_mismatch(score, recommendation):
    """Flag extreme score-recommendation inconsistencies."""
    if score <= 2 and recommendation == "BUY":
        return True
    if score <= 3 and recommendation == "BUY":
        return True  # score 0-3 with BUY
    if score >= 8 and recommendation == "AVOID":
        return True  # score 8-10 with AVOID
    if score >= 9 and recommendation == "HOLD":
        return True  # score 9-10 only HOLD
    if score <= 1 and recommendation == "HOLD":
        return True  # score 0-1 with HOLD
    return False


def audit_all():
    stock_dirs = sorted([d for d in ANALYSES_DIR.iterdir() if d.is_dir()])

    # Counters and collectors
    total_files_checked = 0
    total_files_expected = 0

    issues = {
        "json_parse_errors": [],        # 12. Data encoding / truncated JSON
        "missing_fields": [],            # 1. Missing/malformed fields
        "score_out_of_range": [],        # 2. Score not 0-10 int
        "invalid_recommendation": [],    # 3. Not BUY/HOLD/AVOID
        "invalid_metric_values": [],     # 4. Invalid metrics_evaluated values
        "empty_arrays": [],              # 5. key_strengths or key_risks with 0 items
        "empty_strings": [],             # 6. investment_thesis or detailed_analysis empty/<20 chars
        "score_recommendation_mismatch": [],  # 7. Extreme score-recommendation inconsistency
        "missing_persona_files": [],     # 11. Stocks missing some persona analyses
    }

    # For duplicate detection (8)
    thesis_to_files = defaultdict(list)

    # For persona consistency (9) and score distribution (10)
    persona_scores = defaultdict(list)  # persona -> list of scores
    persona_score_per_stock = defaultdict(dict)  # stock -> {persona: score}

    # Track all valid scores per persona for distribution analysis
    persona_all_scores = defaultdict(list)

    for stock_dir in stock_dirs:
        symbol = stock_dir.name
        existing_personas = set()

        for persona_file in stock_dir.iterdir():
            if not persona_file.name.endswith(".json"):
                continue

            persona_name = persona_file.stem
            existing_personas.add(persona_name)
            total_files_checked += 1
            file_label = f"{symbol}/{persona_file.name}"

            # --- Check 12: JSON parse / encoding errors ---
            try:
                raw = persona_file.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                issues["json_parse_errors"].append((file_label, f"UnicodeDecodeError: {e}"))
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                issues["json_parse_errors"].append((file_label, f"JSONDecodeError: {e}"))
                continue

            # --- Check 1: Missing/malformed fields ---
            analysis = data.get("analysis")
            if analysis is None:
                issues["missing_fields"].append((file_label, "missing 'analysis' top-level key"))
                continue

            required_fields = ["score", "recommendation", "investment_thesis",
                               "key_strengths", "key_risks", "red_flags",
                               "detailed_analysis", "metrics_evaluated"]
            missing = [f for f in required_fields if f not in analysis]
            if missing:
                issues["missing_fields"].append((file_label, f"missing fields: {missing}"))
                # Continue checking what we can

            # --- Check 2: Score out of range ---
            score = analysis.get("score")
            score_valid = False
            if score is not None:
                if not isinstance(score, (int, float)) or score != int(score) or int(score) < 0 or int(score) > 10:
                    issues["score_out_of_range"].append((file_label, f"score={score}"))
                else:
                    score = int(score)
                    score_valid = True
                    persona_all_scores[persona_name].append(score)
                    persona_score_per_stock[symbol][persona_name] = score

            # --- Check 3: Invalid recommendation ---
            rec = analysis.get("recommendation")
            rec_valid = False
            if rec is not None:
                if rec not in VALID_RECOMMENDATIONS:
                    issues["invalid_recommendation"].append((file_label, f"recommendation='{rec}'"))
                else:
                    rec_valid = True

            # --- Check 4: Invalid metrics_evaluated values ---
            metrics = analysis.get("metrics_evaluated")
            if metrics is not None:
                if not isinstance(metrics, dict):
                    issues["invalid_metric_values"].append((file_label, "metrics_evaluated is not a dict"))
                else:
                    for metric_key, valid_vals in VALID_METRICS.items():
                        val = metrics.get(metric_key)
                        if val is None:
                            issues["missing_fields"].append((file_label, f"missing metric: {metric_key}"))
                        elif val not in valid_vals:
                            issues["invalid_metric_values"].append(
                                (file_label, f"{metric_key}='{val}' (expected one of {valid_vals})")
                            )
                    # Check for unexpected extra metric keys
                    extra_keys = set(metrics.keys()) - set(VALID_METRICS.keys())
                    if extra_keys:
                        issues["invalid_metric_values"].append(
                            (file_label, f"unexpected metric keys: {extra_keys}")
                        )

            # --- Check 5: Empty arrays ---
            for arr_field in ["key_strengths", "key_risks"]:
                arr = analysis.get(arr_field)
                if arr is not None and isinstance(arr, list) and len(arr) == 0:
                    issues["empty_arrays"].append((file_label, f"{arr_field} is empty"))

            # --- Check 6: Empty/too-short strings ---
            for str_field in ["investment_thesis", "detailed_analysis"]:
                val = analysis.get(str_field)
                if val is not None:
                    if not isinstance(val, str) or len(val.strip()) < 20:
                        issues["empty_strings"].append(
                            (file_label, f"{str_field} is too short ({len(val.strip()) if isinstance(val, str) else 'not a string'} chars)")
                        )

            # --- Check 7: Score-recommendation extreme mismatch ---
            if score_valid and rec_valid:
                if is_extreme_mismatch(score, rec):
                    issues["score_recommendation_mismatch"].append(
                        (file_label, f"score={score}, recommendation={rec}")
                    )

            # --- Collect data for check 8 (duplicate thesis) ---
            thesis = analysis.get("investment_thesis", "")
            if isinstance(thesis, str) and len(thesis.strip()) > 20:
                thesis_normalized = thesis.strip().lower()
                thesis_to_files[thesis_normalized].append(file_label)

        # --- Check 11: Missing persona files ---
        total_files_expected += len(EXPECTED_PERSONAS)
        missing_personas = set(EXPECTED_PERSONAS) - existing_personas
        if missing_personas:
            issues["missing_persona_files"].append(
                (symbol, f"missing: {sorted(missing_personas)}")
            )

    # --- Check 8: Duplicate/identical theses across different stocks ---
    duplicate_theses = []
    for thesis_text, files in thesis_to_files.items():
        # Extract unique stock symbols
        stocks = set(f.split("/")[0] for f in files)
        if len(stocks) > 1:
            duplicate_theses.append((files, thesis_text[:100] + "..."))

    # --- Check 9: Persona consistency ---
    # Benjamin Graham should generally score lower than Peter Lynch
    persona_consistency_issues = []
    graham_avg = statistics.mean(persona_all_scores["benjamin_graham"]) if persona_all_scores["benjamin_graham"] else None
    lynch_avg = statistics.mean(persona_all_scores["peter_lynch"]) if persona_all_scores["peter_lynch"] else None

    # Compare each persona pair across all stocks where both exist
    # Focus on: Graham vs Lynch, Graham vs Buffett, Marks vs Kedia
    persona_avgs = {}
    for p, scores in persona_all_scores.items():
        if scores:
            persona_avgs[p] = {
                "mean": round(statistics.mean(scores), 2),
                "median": statistics.median(scores),
                "stdev": round(statistics.stdev(scores), 2) if len(scores) > 1 else 0,
                "min": min(scores),
                "max": max(scores),
                "count": len(scores),
            }

    # Check per-stock: Graham should usually score <= Lynch
    graham_higher_count = 0
    graham_lynch_comparisons = 0
    graham_much_higher = []  # Graham scores > Lynch by 3+
    for symbol, scores in persona_score_per_stock.items():
        g = scores.get("benjamin_graham")
        l = scores.get("peter_lynch")
        if g is not None and l is not None:
            graham_lynch_comparisons += 1
            if g > l:
                graham_higher_count += 1
                if g - l >= 3:
                    graham_much_higher.append((symbol, f"graham={g}, lynch={l}"))

    # --- Check 10: Score distribution per persona ---
    # Flag if a persona gives the exact same score to every stock (or nearly all)
    uniform_personas = []
    for persona, scores in persona_all_scores.items():
        if not scores:
            continue
        counter = Counter(scores)
        most_common_score, most_common_count = counter.most_common(1)[0]
        pct = most_common_count / len(scores) * 100
        unique_scores = len(counter)
        if unique_scores == 1:
            uniform_personas.append((persona, f"ALL {len(scores)} scores are {most_common_score}"))
        elif pct > 80:
            uniform_personas.append((persona, f"{pct:.1f}% of scores are {most_common_score} ({most_common_count}/{len(scores)})"))

    # ==================== REPORT ====================
    print("=" * 80)
    print("   DATA QUALITY AUDIT REPORT — Investor Persona Analyses")
    print("=" * 80)
    print()

    total_stocks = len(stock_dirs)
    print(f"Total stock directories:       {total_stocks}")
    print(f"Expected persona files/stock:  {len(EXPECTED_PERSONAS)}")
    print(f"Total files expected:          {total_files_expected}")
    print(f"Total files checked:           {total_files_checked}")
    print()

    # --- Summary table ---
    print("-" * 80)
    print(f"{'ISSUE CATEGORY':<45} {'COUNT':>8}")
    print("-" * 80)

    all_issue_counts = {}

    def report_section(title, items, show_examples=5):
        count = len(items)
        all_issue_counts[title] = count
        print(f"  {title:<43} {count:>8}")
        return count

    report_section("1.  Missing/malformed fields", issues["missing_fields"])
    report_section("2.  Score out of range", issues["score_out_of_range"])
    report_section("3.  Invalid recommendation", issues["invalid_recommendation"])
    report_section("4.  Invalid metrics_evaluated values", issues["invalid_metric_values"])
    report_section("5.  Empty arrays (strengths/risks)", issues["empty_arrays"])
    report_section("6.  Empty/short strings (thesis/analysis)", issues["empty_strings"])
    report_section("7.  Score-recommendation mismatch", issues["score_recommendation_mismatch"])
    report_section("8.  Duplicate theses across stocks", duplicate_theses)
    report_section("9.  Persona consistency anomalies (Graham>>Lynch)", graham_much_higher)
    report_section("10. Uniform score distribution (lazy output)", uniform_personas)
    report_section("11. Missing persona files", issues["missing_persona_files"])
    report_section("12. JSON parse / encoding errors", issues["json_parse_errors"])

    total_issues = sum(all_issue_counts.values())
    print("-" * 80)
    print(f"  {'TOTAL ISSUES':<43} {total_issues:>8}")
    print("-" * 80)
    print()

    # --- Detailed findings ---
    def print_examples(title, items, max_examples=10):
        if not items:
            return
        print(f"\n{'='*80}")
        print(f"  DETAILS: {title} ({len(items)} issues)")
        print(f"{'='*80}")
        for item in items[:max_examples]:
            if isinstance(item, tuple) and len(item) == 2:
                print(f"    - {item[0]}: {item[1]}")
            else:
                print(f"    - {item}")
        if len(items) > max_examples:
            print(f"    ... and {len(items) - max_examples} more")

    print_examples("1. Missing/malformed fields", issues["missing_fields"])
    print_examples("2. Score out of range", issues["score_out_of_range"])
    print_examples("3. Invalid recommendation", issues["invalid_recommendation"])
    print_examples("4. Invalid metrics_evaluated values", issues["invalid_metric_values"])
    print_examples("5. Empty arrays", issues["empty_arrays"])
    print_examples("6. Empty/short strings", issues["empty_strings"])
    print_examples("7. Score-recommendation mismatch (extreme)", issues["score_recommendation_mismatch"])

    if duplicate_theses:
        print(f"\n{'='*80}")
        print(f"  DETAILS: 8. Duplicate theses across stocks ({len(duplicate_theses)} groups)")
        print(f"{'='*80}")
        for files, snippet in duplicate_theses[:10]:
            print(f"    Files: {files}")
            print(f"    Thesis: \"{snippet}\"")
            print()
        if len(duplicate_theses) > 10:
            print(f"    ... and {len(duplicate_theses) - 10} more duplicate groups")

    if graham_much_higher:
        print(f"\n{'='*80}")
        print(f"  DETAILS: 9. Persona consistency — Graham scores much higher than Lynch")
        print(f"{'='*80}")
        for item in graham_much_higher[:10]:
            print(f"    - Stock {item[0]}: {item[1]}")
        if len(graham_much_higher) > 10:
            print(f"    ... and {len(graham_much_higher) - 10} more")

    print_examples("10. Uniform score distribution", uniform_personas)
    print_examples("11. Missing persona files", issues["missing_persona_files"])
    print_examples("12. JSON parse / encoding errors", issues["json_parse_errors"])

    # --- Persona score statistics ---
    print(f"\n{'='*80}")
    print("  PERSONA SCORE STATISTICS")
    print(f"{'='*80}")
    print(f"  {'Persona':<25} {'Mean':>6} {'Median':>7} {'StDev':>7} {'Min':>5} {'Max':>5} {'Count':>6}")
    print(f"  {'-'*25} {'-'*6} {'-'*7} {'-'*7} {'-'*5} {'-'*5} {'-'*6}")
    for persona in EXPECTED_PERSONAS:
        stats = persona_avgs.get(persona)
        if stats:
            print(f"  {persona:<25} {stats['mean']:>6.2f} {stats['median']:>7.1f} {stats['stdev']:>7.2f} {stats['min']:>5} {stats['max']:>5} {stats['count']:>6}")
        else:
            print(f"  {persona:<25} {'N/A':>6}")

    # Persona consistency summary
    print(f"\n  Graham vs Lynch per-stock comparison:")
    print(f"    Total stocks compared:    {graham_lynch_comparisons}")
    print(f"    Graham scored HIGHER:     {graham_higher_count} ({graham_higher_count/max(graham_lynch_comparisons,1)*100:.1f}%)")
    print(f"    Graham scored MUCH higher (by 3+): {len(graham_much_higher)}")

    # --- Score distribution per persona ---
    print(f"\n{'='*80}")
    print("  SCORE DISTRIBUTION PER PERSONA")
    print(f"{'='*80}")
    for persona in EXPECTED_PERSONAS:
        scores = persona_all_scores.get(persona, [])
        if not scores:
            continue
        counter = Counter(scores)
        dist = " ".join(f"{s}:{c}" for s, c in sorted(counter.items()))
        print(f"  {persona:<25} {dist}")

    # --- Recommendation distribution per persona ---
    print(f"\n{'='*80}")
    print("  RECOMMENDATION DISTRIBUTION (from all valid files)")
    print(f"{'='*80}")

    # Need to re-scan for recommendations per persona
    rec_by_persona = defaultdict(Counter)
    for stock_dir in stock_dirs:
        for persona_file in stock_dir.iterdir():
            if not persona_file.name.endswith(".json"):
                continue
            try:
                data = json.loads(persona_file.read_text(encoding="utf-8"))
                persona_name = persona_file.stem
                rec = data.get("analysis", {}).get("recommendation")
                if rec in VALID_RECOMMENDATIONS:
                    rec_by_persona[persona_name][rec] += 1
            except:
                pass

    print(f"  {'Persona':<25} {'BUY':>6} {'HOLD':>6} {'AVOID':>6} {'Total':>6}")
    print(f"  {'-'*25} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
    for persona in EXPECTED_PERSONAS:
        c = rec_by_persona.get(persona, Counter())
        total = sum(c.values())
        print(f"  {persona:<25} {c.get('BUY',0):>6} {c.get('HOLD',0):>6} {c.get('AVOID',0):>6} {total:>6}")

    # --- Overall Grade ---
    print(f"\n{'='*80}")
    print("  OVERALL DATA QUALITY GRADE")
    print(f"{'='*80}")

    critical_issues = (
        len(issues["json_parse_errors"]) +
        len(issues["missing_fields"]) +
        len(issues["score_out_of_range"]) +
        len(issues["invalid_recommendation"]) +
        len(issues["missing_persona_files"])
    )

    moderate_issues = (
        len(issues["invalid_metric_values"]) +
        len(issues["empty_arrays"]) +
        len(issues["empty_strings"]) +
        len(issues["score_recommendation_mismatch"])
    )

    minor_issues = (
        len(duplicate_theses) +
        len(graham_much_higher) +
        len(uniform_personas)
    )

    issue_rate = total_issues / max(total_files_checked, 1) * 100
    critical_rate = critical_issues / max(total_files_checked, 1) * 100

    print(f"\n  Files checked:            {total_files_checked}")
    print(f"  Critical issues:          {critical_issues}  (parse errors, missing fields, invalid values, missing files)")
    print(f"  Moderate issues:          {moderate_issues}  (invalid metrics, empty content, mismatches)")
    print(f"  Minor/informational:      {minor_issues}  (duplicates, persona consistency, distributions)")
    print(f"  Total issues:             {total_issues}")
    print(f"  Issue rate:               {issue_rate:.2f}% (issues per file)")
    print(f"  Critical issue rate:      {critical_rate:.2f}%")

    if critical_rate == 0 and moderate_issues == 0:
        grade = "A+"
        desc = "Excellent — no structural or content issues found"
    elif critical_rate == 0 and issue_rate < 2:
        grade = "A"
        desc = "Very Good — no critical issues, minor concerns only"
    elif critical_rate < 1 and issue_rate < 5:
        grade = "B+"
        desc = "Good — very few critical issues, some moderate concerns"
    elif critical_rate < 2 and issue_rate < 10:
        grade = "B"
        desc = "Acceptable — a few critical issues, worth investigating"
    elif critical_rate < 5:
        grade = "C"
        desc = "Concerning — notable quality issues requiring attention"
    elif critical_rate < 10:
        grade = "D"
        desc = "Poor — significant quality problems"
    else:
        grade = "F"
        desc = "Failing — fundamental data quality issues"

    print(f"\n  GRADE:  {grade}")
    print(f"  {desc}")
    print()
    print("=" * 80)


if __name__ == "__main__":
    audit_all()
