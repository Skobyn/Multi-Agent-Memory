"""Analysis script for publication benchmark results.

Reads all JSON files from tests/results/, aggregates across runs,
computes statistics with proper confidence intervals, and produces
a publication-quality text report plus CSV summary.

Statistical methods:
    - Wilson score intervals for proportion metrics (correct for small n near 0/1)
    - t-distribution CIs for latency metrics (appropriate for n=5)
    - Linear regression: recall_rate ~ log2(window_size) for degradation curve
    - Cohen's h effect size for with-memory vs without-memory comparisons
    - McNemar's test for paired binary outcomes

Usage:
    PYTHONPATH=src python tests/benchmark_analysis.py
    PYTHONPATH=src python tests/benchmark_analysis.py --results-dir tests/results/
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _wilson_ci(successes: int, trials: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval for a proportion.

    Returns (proportion, lower, upper). Correct for small n and near 0/1.
    """
    if trials == 0:
        return 0.0, 0.0, 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denom
    return p, max(0.0, centre - margin), min(1.0, centre + margin)


def _t_ci(values: list[float], confidence: float = 0.95) -> tuple[float, float, float]:
    """t-distribution confidence interval for a mean.

    Returns (mean, lower, upper). Uses t-critical values for small n.
    """
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    if n == 1:
        return values[0], values[0], values[0]

    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    std = math.sqrt(variance)
    se = std / math.sqrt(n)

    # t-critical values for common small n (two-tailed, 95%)
    t_crit = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
              7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}
    t = t_crit.get(n, 1.96)  # fall back to z for large n

    margin = t * se
    return mean, mean - margin, mean + margin


def _cohens_h(p1: float, p2: float) -> float:
    """Cohen's h effect size for comparing two proportions.

    h = 2 * arcsin(sqrt(p1)) - 2 * arcsin(sqrt(p2))
    """
    return 2 * math.asin(math.sqrt(max(0, min(1, p1)))) - 2 * math.asin(math.sqrt(max(0, min(1, p2))))


def _mcnemar_chi2(b: int, c: int) -> tuple[float, float]:
    """McNemar's test chi-squared statistic and p-value approximation.

    b = cases where only method 1 succeeds
    c = cases where only method 2 succeeds

    Returns (chi2, p_value_approx).
    Uses continuity correction.
    """
    if b + c == 0:
        return 0.0, 1.0
    chi2 = (abs(b - c) - 1) ** 2 / (b + c)
    # Approximate p-value from chi2(1) using standard formula
    # For chi2(1): p = erfc(sqrt(chi2/2))
    p = math.erfc(math.sqrt(chi2 / 2))
    return chi2, p


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Simple linear regression y = a + b*x.

    Returns (slope, intercept, r_squared).
    """
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_yy = sum((y - mean_y) ** 2 for y in ys)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))

    if ss_xx < 1e-12:
        return 0.0, mean_y, 0.0

    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_yy > 1e-12 else 0.0

    return slope, intercept, r_squared


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_results(results_dir: Path) -> dict[int, list[dict]]:
    """Load all JSON result files, grouped by window_size."""
    grouped: dict[int, list[dict]] = defaultdict(list)

    json_files = sorted(results_dir.glob("run_*.json"))
    if not json_files:
        print(f"ERROR: No result files found in {results_dir}/")
        sys.exit(1)

    for path in json_files:
        with open(path) as f:
            data = json.load(f)
        ws = data["metadata"]["window_size"]
        grouped[ws].append(data)

    return dict(sorted(grouped.items()))


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(grouped: dict[int, list[dict]], results_dir: Path) -> None:
    """Generate the full analysis report and CSV summary."""

    lines: list[str] = []
    csv_rows: list[dict] = []

    def out(s: str = "") -> None:
        lines.append(s)

    # ------------------------------------------------------------------
    # 1. Header and metadata
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  PUBLICATION BENCHMARK ANALYSIS REPORT")
    out("=" * 78)
    out()

    # Use metadata from first run as representative
    first_data = next(iter(next(iter(grouped.values()))))
    meta = first_data["metadata"]
    out("Reproducibility Metadata:")
    out(f"  Git commit:         {meta['git_commit']}")
    out(f"  Python version:     {meta['python_version']}")
    out(f"  Platform:           {meta['platform']}")
    out(f"  SQLite version:     {meta['sqlite_version']}")
    out(f"  sqlite-vec:         {'available' if meta['sqlite_vec_available'] else 'not available'}")
    if meta.get('system_memory_mb'):
        out(f"  System memory:      {meta['system_memory_mb']} MB")
    out()

    total_runs = sum(len(runs) for runs in grouped.values())
    window_sizes = sorted(grouped.keys())
    out(f"  Window sizes tested: {window_sizes}")
    out(f"  Runs per window:     {len(next(iter(grouped.values())))}")
    out(f"  Total runs:          {total_runs}")
    out()

    # ------------------------------------------------------------------
    # 2. Per-window retention results
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  SECTION 2: RETENTION RESULTS BY WINDOW SIZE")
    out("=" * 78)
    out()

    header = (
        f"  {'Window':>6}  {'N':>3}  {'Decs':>4}  "
        f"{'Recall':>14}  {'Accuracy':>14}  "
        f"{'ContraDet':>14}  {'FP Rate':>14}  "
        f"{'Briefing':>14}"
    )
    out(header)
    out("  " + "-" * (len(header) - 2))

    for ws in window_sizes:
        runs = grouped[ws]
        n_runs = len(runs)
        n_decisions = runs[0]["config"]["n_decisions"]

        # Aggregate successes across all runs for Wilson CI
        total_decisions_tested = sum(r["config"]["n_decisions"] for r in runs)
        total_recalled = sum(r["retention"]["with_memory"]["decisions_recalled"] for r in runs)
        total_accurate = sum(r["retention"]["with_memory"]["decisions_accurate"] for r in runs)
        total_contra = sum(r["retention"]["with_memory"]["contradictions_detected"] for r in runs)
        total_fp_flagged = sum(r["retention"]["with_memory"]["false_positives_flagged"] for r in runs)
        total_fp_tested = sum(len(r["false_positive_detail"]) for r in runs)
        total_briefing = sum(r["retention"]["with_memory"]["decisions_in_briefing"] for r in runs)

        recall_p, recall_lo, recall_hi = _wilson_ci(total_recalled, total_decisions_tested)
        acc_p, acc_lo, acc_hi = _wilson_ci(total_accurate, total_decisions_tested)
        contra_p, contra_lo, contra_hi = _wilson_ci(total_contra, total_decisions_tested)
        fp_p, fp_lo, fp_hi = _wilson_ci(total_fp_flagged, total_fp_tested)
        brief_p, brief_lo, brief_hi = _wilson_ci(total_briefing, total_decisions_tested)

        out(
            f"  {ws:>6}  {n_runs:>3}  {n_decisions:>4}  "
            f"{recall_p:>5.1%} [{recall_lo:.1%},{recall_hi:.1%}]  "
            f"{acc_p:>5.1%} [{acc_lo:.1%},{acc_hi:.1%}]  "
            f"{contra_p:>5.1%} [{contra_lo:.1%},{contra_hi:.1%}]  "
            f"{fp_p:>5.1%} [{fp_lo:.1%},{fp_hi:.1%}]  "
            f"{brief_p:>5.1%} [{brief_lo:.1%},{brief_hi:.1%}]"
        )

        # Baseline comparison
        baseline_prop_rates = [r["retention"]["without_memory_proportional"]["recall_rate"] for r in runs]
        baseline_fix_rates = [r["retention"]["without_memory_fixed10"]["recall_rate"] for r in runs]

        csv_rows.append({
            "window_size": ws,
            "n_runs": n_runs,
            "n_decisions": n_decisions,
            "recall_rate": f"{recall_p:.4f}",
            "recall_ci_lo": f"{recall_lo:.4f}",
            "recall_ci_hi": f"{recall_hi:.4f}",
            "accuracy_rate": f"{acc_p:.4f}",
            "accuracy_ci_lo": f"{acc_lo:.4f}",
            "accuracy_ci_hi": f"{acc_hi:.4f}",
            "contradiction_rate": f"{contra_p:.4f}",
            "contradiction_ci_lo": f"{contra_lo:.4f}",
            "contradiction_ci_hi": f"{contra_hi:.4f}",
            "fp_rate": f"{fp_p:.4f}",
            "fp_ci_lo": f"{fp_lo:.4f}",
            "fp_ci_hi": f"{fp_hi:.4f}",
            "briefing_coverage": f"{brief_p:.4f}",
            "briefing_ci_lo": f"{brief_lo:.4f}",
            "briefing_ci_hi": f"{brief_hi:.4f}",
            "baseline_proportional_recall": f"{sum(baseline_prop_rates)/len(baseline_prop_rates):.4f}" if baseline_prop_rates else "0",
            "baseline_fixed10_recall": f"{sum(baseline_fix_rates)/len(baseline_fix_rates):.4f}" if baseline_fix_rates else "0",
            "search_latency_mean_ms": "",
            "search_latency_ci_lo_ms": "",
            "search_latency_ci_hi_ms": "",
            "archive_latency_mean_ms": "",
            "chunks_in_archive": "",
            "db_size_kb": "",
        })

    out()

    # Baseline comparison table
    out("  Baseline comparison (no memory system):")
    out(f"  {'Window':>6}  {'Proportional':>14}  {'Fixed-10':>14}  {'With Memory':>14}  {'Cohen h (prop)':>14}  {'Cohen h (fix)':>14}")
    out("  " + "-" * 80)

    for ws in window_sizes:
        runs = grouped[ws]
        total_decisions_tested = sum(r["config"]["n_decisions"] for r in runs)
        total_recalled = sum(r["retention"]["with_memory"]["decisions_recalled"] for r in runs)
        recall_p = total_recalled / total_decisions_tested if total_decisions_tested else 0

        prop_recalled = sum(r["retention"]["without_memory_proportional"]["decisions_recalled"] for r in runs)
        fix_recalled = sum(r["retention"]["without_memory_fixed10"]["decisions_recalled"] for r in runs)
        prop_rate = prop_recalled / total_decisions_tested if total_decisions_tested else 0
        fix_rate = fix_recalled / total_decisions_tested if total_decisions_tested else 0

        h_prop = _cohens_h(recall_p, prop_rate)
        h_fix = _cohens_h(recall_p, fix_rate)

        out(
            f"  {ws:>6}  {prop_rate:>13.1%}  {fix_rate:>13.1%}  "
            f"{recall_p:>13.1%}  {h_prop:>13.2f}  {h_fix:>13.2f}"
        )

    out()
    out("  Cohen's h interpretation: |h|<0.2 small, 0.2-0.8 medium, >0.8 large")
    out()

    # ------------------------------------------------------------------
    # 3. Per-window latency results
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  SECTION 3: PERFORMANCE (LATENCY) RESULTS")
    out("=" * 78)
    out()

    out("  Search latency (ms):")
    out(f"  {'Window':>6}  {'Mean':>10}  {'95% CI':>18}  {'P95':>10}  {'P99':>10}")
    out("  " + "-" * 60)

    for ws in window_sizes:
        runs = grouped[ws]
        means = [r["performance"]["search_latency_s"]["mean"] * 1000 for r in runs]
        p95s = [r["performance"]["search_latency_s"]["p95"] * 1000 for r in runs]
        p99s = [r["performance"]["search_latency_s"]["p99"] * 1000 for r in runs]

        mean_val, ci_lo, ci_hi = _t_ci(means)
        p95_mean = sum(p95s) / len(p95s) if p95s else 0
        p99_mean = sum(p99s) / len(p99s) if p99s else 0

        out(
            f"  {ws:>6}  {mean_val:>8.2f}ms  "
            f"[{ci_lo:>6.2f}, {ci_hi:>6.2f}]  "
            f"{p95_mean:>8.2f}ms  {p99_mean:>8.2f}ms"
        )

        # Update CSV row
        for row in csv_rows:
            if row["window_size"] == ws:
                row["search_latency_mean_ms"] = f"{mean_val:.4f}"
                row["search_latency_ci_lo_ms"] = f"{ci_lo:.4f}"
                row["search_latency_ci_hi_ms"] = f"{ci_hi:.4f}"

    out()

    out("  Archive latency (ms):")
    out(f"  {'Window':>6}  {'Mean':>10}  {'95% CI':>18}")
    out("  " + "-" * 40)

    for ws in window_sizes:
        runs = grouped[ws]
        archive_lats = [r["performance"]["archive_latency_s"] * 1000 for r in runs]
        mean_val, ci_lo, ci_hi = _t_ci(archive_lats)
        out(f"  {ws:>6}  {mean_val:>8.1f}ms  [{ci_lo:>6.1f}, {ci_hi:>6.1f}]")

        for row in csv_rows:
            if row["window_size"] == ws:
                row["archive_latency_mean_ms"] = f"{mean_val:.4f}"

    out()

    out("  on_assistant_message latency (ms):")
    out(f"  {'Window':>6}  {'Mean':>10}  {'95% CI':>18}")
    out("  " + "-" * 40)

    for ws in window_sizes:
        runs = grouped[ws]
        oam_lats = [r["performance"]["on_assistant_latency_s"]["mean"] * 1000 for r in runs]
        mean_val, ci_lo, ci_hi = _t_ci(oam_lats)
        out(f"  {ws:>6}  {mean_val:>8.2f}ms  [{ci_lo:>6.2f}, {ci_hi:>6.2f}]")

    out()

    out("  Briefing generation latency (ms):")
    out(f"  {'Window':>6}  {'Mean':>10}  {'95% CI':>18}")
    out("  " + "-" * 40)

    for ws in window_sizes:
        runs = grouped[ws]
        brief_lats = [r["performance"]["briefing_latency_s"] * 1000 for r in runs]
        mean_val, ci_lo, ci_hi = _t_ci(brief_lats)
        out(f"  {ws:>6}  {mean_val:>8.2f}ms  [{ci_lo:>6.2f}, {ci_hi:>6.2f}]")

    out()

    # ------------------------------------------------------------------
    # 4. Degradation curve (recall vs window size)
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  SECTION 4: DEGRADATION CURVE")
    out("=" * 78)
    out()
    out("  Linear regression: recall_rate ~ log2(window_size)")
    out()

    log_ws = []
    recall_rates = []

    for ws in window_sizes:
        runs = grouped[ws]
        total_decisions = sum(r["config"]["n_decisions"] for r in runs)
        total_recalled = sum(r["retention"]["with_memory"]["decisions_recalled"] for r in runs)
        rate = total_recalled / total_decisions if total_decisions else 0
        log_ws.append(math.log2(ws))
        recall_rates.append(rate)

    slope, intercept, r_sq = _linear_regression(log_ws, recall_rates)

    out(f"  slope     = {slope:+.4f}  (change in recall per doubling of window size)")
    out(f"  intercept = {intercept:.4f}")
    out(f"  R-squared = {r_sq:.4f}")
    out()

    if abs(slope) < 0.01:
        out("  Interpretation: Recall is essentially flat across window sizes (no degradation).")
    elif slope < 0:
        pct_per_doubling = abs(slope) * 100
        out(f"  Interpretation: Recall decreases ~{pct_per_doubling:.1f}pp per doubling of window size.")
    else:
        pct_per_doubling = slope * 100
        out(f"  Interpretation: Recall increases ~{pct_per_doubling:.1f}pp per doubling of window size.")

    out()

    # Also run regression on accuracy
    acc_rates = []
    for ws in window_sizes:
        runs = grouped[ws]
        total_decisions = sum(r["config"]["n_decisions"] for r in runs)
        total_accurate = sum(r["retention"]["with_memory"]["decisions_accurate"] for r in runs)
        rate = total_accurate / total_decisions if total_decisions else 0
        acc_rates.append(rate)

    slope_acc, intercept_acc, r_sq_acc = _linear_regression(log_ws, acc_rates)
    out(f"  Accuracy degradation: slope={slope_acc:+.4f}, R^2={r_sq_acc:.4f}")
    out()

    # ------------------------------------------------------------------
    # 5. Cross-window comparison (effect sizes, McNemar)
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  SECTION 5: CROSS-WINDOW STATISTICAL TESTS")
    out("=" * 78)
    out()

    # McNemar's test: for each decision, did memory recall it vs did baseline?
    # We aggregate per-decision binary outcomes across all runs.
    out("  McNemar's test (memory vs proportional baseline):")
    out(f"  {'Window':>6}  {'b (only mem)':>12}  {'c (only base)':>13}  {'chi2':>8}  {'p-value':>10}")
    out("  " + "-" * 55)

    for ws in window_sizes:
        runs = grouped[ws]
        b_total = 0  # only memory succeeds
        c_total = 0  # only baseline succeeds

        for r in runs:
            # For each decision, check if memory recalled it
            keep_recent = r["config"]["compaction_keep_proportional"]
            decisions = r["per_decision_detail"]

            for d in decisions:
                mem_success = d["recalled"]
                # Check if baseline would have recalled this decision
                # Decisions near the end of the window are in recent turns
                baseline_success = d["turn"] >= (r["config"]["window_size"] - keep_recent)

                if mem_success and not baseline_success:
                    b_total += 1
                elif not mem_success and baseline_success:
                    c_total += 1

        chi2, p_val = _mcnemar_chi2(b_total, c_total)
        sig = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else "ns"
        out(f"  {ws:>6}  {b_total:>12}  {c_total:>13}  {chi2:>8.2f}  {p_val:>8.4f} {sig}")

    out()
    out("  Significance: *** p<0.001, ** p<0.01, * p<0.05, ns = not significant")
    out()

    # ------------------------------------------------------------------
    # 6. Scaling characteristics
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  SECTION 6: SCALING CHARACTERISTICS")
    out("=" * 78)
    out()

    out("  Archive size and search latency scaling:")
    out(f"  {'Window':>6}  {'Chunks':>8}  {'DB Size':>10}  {'Search ms':>10}")
    out("  " + "-" * 42)

    chunk_counts = []
    search_means = []

    for ws in window_sizes:
        runs = grouped[ws]
        avg_chunks = sum(r["scaling"]["chunks_in_archive"] for r in runs) / len(runs)
        avg_db_kb = sum(r["scaling"]["db_size_bytes"] for r in runs) / len(runs) / 1024
        avg_search = sum(r["performance"]["search_latency_s"]["mean"] * 1000 for r in runs) / len(runs)

        chunk_counts.append(avg_chunks)
        search_means.append(avg_search)

        out(f"  {ws:>6}  {avg_chunks:>8.0f}  {avg_db_kb:>8.1f}KB  {avg_search:>8.2f}ms")

        for row in csv_rows:
            if row["window_size"] == ws:
                row["chunks_in_archive"] = f"{avg_chunks:.0f}"
                row["db_size_kb"] = f"{avg_db_kb:.1f}"

    out()

    # Linearity of search latency vs archive size
    if len(chunk_counts) >= 2:
        slope_s, intercept_s, r_sq_s = _linear_regression(chunk_counts, search_means)
        out(f"  Search latency vs chunks: slope={slope_s:.4f} ms/chunk, R^2={r_sq_s:.4f}")

        if r_sq_s > 0.9:
            out("  Interpretation: Search latency scales linearly with archive size.")
        elif r_sq_s > 0.7:
            out("  Interpretation: Search latency shows roughly linear scaling.")
        else:
            out("  Interpretation: Search latency scaling is sub/super-linear (check data).")

        # Also check log scaling
        log_chunks = [math.log2(c) if c > 0 else 0 for c in chunk_counts]
        slope_log, _, r_sq_log = _linear_regression(log_chunks, search_means)
        out(f"  Search latency vs log2(chunks): slope={slope_log:.4f}, R^2={r_sq_log:.4f}")

    out()

    # Archive latency scaling
    out("  Archive latency scaling:")
    archive_window = []
    archive_lats = []
    for ws in window_sizes:
        runs = grouped[ws]
        avg_lat = sum(r["performance"]["archive_latency_s"] * 1000 for r in runs) / len(runs)
        archive_window.append(ws)
        archive_lats.append(avg_lat)

    if len(archive_window) >= 2:
        slope_a, _, r_sq_a = _linear_regression(archive_window, archive_lats)
        out(f"  Archive latency vs window size: slope={slope_a:.4f} ms/turn, R^2={r_sq_a:.4f}")

    out()

    # ------------------------------------------------------------------
    # 7. Limitations
    # ------------------------------------------------------------------
    out("=" * 78)
    out("  SECTION 7: LIMITATIONS AND CAVEATS")
    out("=" * 78)
    out()
    out("  1. EMBEDDING MODEL: Uses deterministic hash-based embeddings, not a")
    out("     learned model. Recall rates with a real embedding model (e.g.,")
    out("     sentence-transformers) would likely differ. The hash embedder has")
    out("     no semantic understanding -- it can only match on lexical overlap")
    out("     via the keyword component of hybrid search.")
    out()
    out("  2. DECISION DISTRIBUTION: Decisions are uniformly distributed across")
    out("     the conversation window. Real conversations may cluster decisions")
    out("     around specific topics or time periods.")
    out()
    out("  3. BASELINE SIMPLICITY: The no-memory baseline uses a fixed lossy")
    out("     summary. Real context compaction (e.g., Claude Code's) may retain")
    out("     more or less information depending on the compaction algorithm.")
    out()
    out("  4. SINGLE-MACHINE TIMING: All latency measurements are from a single")
    out("     machine. Results vary with hardware, OS scheduling, and load.")
    out()
    out("  5. NO LLM EVALUATION: Contradiction detection and confidence scoring")
    out("     use heuristic pattern matching, not LLM judgment. False positive/")
    out("     negative rates may differ with an LLM-based evaluator.")
    out()
    out("  6. SYNTHETIC CONVERSATIONS: Generated conversations are simpler and")
    out("     more structured than real human-AI interactions. Real conversations")
    out("     have more ambiguity, topic switching, and implicit references.")
    out()
    out("  7. sqlite-vec AVAILABILITY: Vector search performance depends on")
    out("     whether sqlite-vec is installed. Without it, search falls back to")
    out("     keyword-only matching.")
    out()

    # ------------------------------------------------------------------
    # Write report
    # ------------------------------------------------------------------
    report_text = "\n".join(lines)

    report_path = results_dir / "analysis_report.txt"
    with open(report_path, "w") as f:
        f.write(report_text)

    # ------------------------------------------------------------------
    # Write CSV
    # ------------------------------------------------------------------
    csv_path = results_dir / "summary.csv"
    if csv_rows:
        fieldnames = list(csv_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)

    # ------------------------------------------------------------------
    # Stdout summary
    # ------------------------------------------------------------------
    print(report_text)
    print()
    print(f"  Report written to: {report_path}")
    print(f"  CSV written to:    {csv_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze publication benchmark results")
    parser.add_argument("--results-dir", type=str, default=None,
                        help="Directory containing run_*.json files (default: tests/results/)")
    args = parser.parse_args()

    results_dir = Path(args.results_dir) if args.results_dir else Path(__file__).parent / "results"

    if not results_dir.exists():
        print(f"ERROR: Results directory does not exist: {results_dir}")
        sys.exit(1)

    grouped = load_results(results_dir)
    generate_report(grouped, results_dir)


if __name__ == "__main__":
    main()
