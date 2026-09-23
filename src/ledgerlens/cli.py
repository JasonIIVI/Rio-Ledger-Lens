"""Command line entry point.

Three verbs for now:

    ledgerlens generate  - build a labelled synthetic ledger
    ledgerlens test      - run the journal-entry tests over a ledger
    ledgerlens benford   - run digit analysis, optionally segmented

Week 2 adds ``score`` (ML layer) and ``report`` (Excel workpaper).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import evaluate, jets
from .benford import benford_test, segmented_benford
from .env import load_dotenv
from .generate import generate_ledger
from .ingest import load_csv, load_labels
from .model import combine, score_ledger
from .report import build_workpaper


def _parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"dates must look like YYYY-MM-DD, got {text!r}"
        ) from exc


def cmd_generate(args: argparse.Namespace) -> int:
    lines, labels = generate_ledger(
        start=args.start,
        end=args.end,
        entries_per_day=args.entries_per_day,
        anomaly_rate=args.anomaly_rate,
        seed=args.seed,
    )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ledger_path = out / "ledger.csv"
    labels_path = out / "labels.csv"
    lines.to_csv(ledger_path, index=False)
    labels.to_csv(labels_path, index=False)

    print("Wrote {:,} lines / {:,} entries to {}".format(
        len(lines), lines["entry_id"].nunique(), ledger_path))
    print("Wrote {:,} labels ({} anomalies, {:.2%}) to {}".format(
        len(labels), int(labels["is_anomaly"].sum()),
        labels["is_anomaly"].mean(), labels_path))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    df = load_csv(args.ledger)
    only: list[str] | None = args.only.split(",") if args.only else None
    flags = jets.run_all(df, only=only)

    print("Ran {} test(s) over {:,} entries".format(
        len(only) if only else len(jets.REGISTRY), df["entry_id"].nunique()))
    print("Raised {:,} flag(s) on {:,} entrie(s)\n".format(len(flags), flags["entry_id"].nunique()))

    if not flags.empty:
        summary = flags.groupby(["test_id", "test_name", "severity"]).size()
        summary = summary.reset_index(name="flags")
        print(summary.to_string(index=False))

    scored = jets.score_entries(df, flags)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        scored.to_csv(args.out, index=False)
        print(f"\nScored entries written to {args.out}")

    top = scored[scored["risk_score"] > 0].head(args.top)
    if not top.empty:
        print(f"\nTop {len(top)} by risk score:")
        for r in top.itertuples():
            print(f"  {r.entry_id}  score {r.risk_score:>4.1f}  {r.tests_fired}  ${r.entry_amount:,.2f}")

    if args.labels:
        labels = load_labels(args.labels)
        metrics = evaluate.evaluate(flags, labels, df["entry_id"].unique())
        print("\n--- evaluation against ground truth ---")
        print(evaluate.format_report(metrics))
        print("\nRecall by archetype:")
        print(evaluate.recall_by_archetype(flags, labels).to_string(index=False))
        print("\nPrecision by test:")
        print(evaluate.precision_by_test(flags, labels).to_string(index=False))
    return 0


def cmd_benford(args: argparse.Namespace) -> int:
    df = load_csv(args.ledger)
    if args.by:
        result = segmented_benford(df, by=args.by, min_n=args.min_n)
        if result.empty:
            print(f"No segment of '{args.by}' had at least {args.min_n} entries.")
            return 0
        print(f"Benford first-digit test, segmented by {args.by} (>= {args.min_n} rows):\n")
        print(result.to_string(index=False))
    else:
        r = benford_test(df["abs_amount"], label="ALL")
        print("Benford first-digit test over {:,} amounts".format(r["n"]))
        if not r["sufficient_sample"]:
            print("WARNING: sample below 300, result is not meaningful")
        print("  MAD         {:.5f}  ({})".format(r["mad"], r["conformity"]))
        print("  chi-square  {:.2f}  (critical 15.507 at 5%, 8 df) -> {}".format(
            r["chi_square"], "exceeds" if r["exceeds_critical"] else "within"))
        print("\n  digit  observed  expected")
        for d in range(1, 10):
            print("    {}      {:>6.2%}    {:>6.2%}".format(
                d, r["observed_prop"][d], r["expected_prop"][d]))
        print("\nNote: non-conformity is a pointer, not a finding. Populations with "
              "price points,\nthresholds or a narrow value range fail this test for "
              "innocent reasons.")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Run both tiers and show how they agree."""
    df = load_csv(args.ledger)
    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)
    model_scores, report = score_ledger(df, contamination=args.contamination)
    combined = combine(scored, model_scores, model_top_pct=args.model_top_pct)

    print(report.describe())
    print()
    print(f"Tier agreement across {len(combined):,} entries:")
    print(combined["agreement"].value_counts().to_string())

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        combined.to_csv(args.out, index=False)
        print(f"\nCombined scores written to {args.out}")

    interesting = combined[combined["agreement"] == "model only"].head(args.top)
    if not interesting.empty:
        print(f"\nUnusual to the model but matching no rule (top {len(interesting)}):")
        for r in interesting.itertuples():
            print(f"  {r.entry_id}  model {r.model_score:.3f}  ${r.entry_amount:,.2f}  {r.description}")

    if args.labels:
        labels = load_labels(args.labels)
        print("\n--- tier comparison ---")
        print(evaluate.compare_tiers(combined, labels).to_string(index=False))
        print("\n--- model lift over random selection ---")
        print(evaluate.model_lift(model_scores, labels).to_string(index=False))
        print("\n--- which archetypes the model can perceive ---")
        print(evaluate.score_by_archetype(model_scores, labels).to_string(index=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Write the Excel workpaper."""
    df = load_csv(args.ledger)
    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)

    model_report = None
    if not args.no_model:
        model_scores, report = score_ledger(df)
        scored = combine(scored, model_scores)
        model_report = report.describe()

    metrics = None
    if args.labels:
        metrics = evaluate.evaluate(flags, load_labels(args.labels), df["entry_id"].unique())

    path = build_workpaper(
        scored, flags, args.out,
        benford=segmented_benford(df, by="account_code"),
        metrics=metrics, model_report=model_report, top_n=args.top,
    )
    print(f"Workpaper written to {path}")
    print("  {:,} entries in population, {:,} flagged".format(
        len(scored), int((scored["risk_score"] > 0).sum())))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledgerlens",
        description="Audit analytics over general ledger data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="build a labelled synthetic ledger")
    g.add_argument("--start", type=_parse_date, default=date(2024, 1, 1))
    g.add_argument("--end", type=_parse_date, default=date(2025, 12, 31))
    g.add_argument("--entries-per-day", type=int, default=10)
    g.add_argument("--anomaly-rate", type=float, default=0.015)
    g.add_argument("--seed", type=int, default=20260922)
    g.add_argument("--out-dir", default="data")
    g.set_defaults(func=cmd_generate)

    t = sub.add_parser("test", help="run journal-entry tests")
    t.add_argument("ledger", help="path to a GL csv")
    t.add_argument("--labels", help="ground-truth csv, enables precision/recall output")
    t.add_argument("--only", help="comma-separated test ids, e.g. JET-01,JET-05")
    t.add_argument("--out", help="write scored entries to this csv")
    t.add_argument("--top", type=int, default=10)
    t.set_defaults(func=cmd_test)

    b = sub.add_parser("benford", help="first-digit digit analysis")
    b.add_argument("ledger", help="path to a GL csv")
    b.add_argument("--by", help="segment column, e.g. account_code or created_by")
    b.add_argument("--min-n", type=int, default=300)
    b.set_defaults(func=cmd_benford)

    sc = sub.add_parser("score", help="run both tiers and compare them")
    sc.add_argument("ledger", help="path to a GL csv")
    sc.add_argument("--labels", help="ground-truth csv, enables tier comparison")
    sc.add_argument("--contamination", type=float, default=0.02)
    sc.add_argument("--model-top-pct", type=float, default=0.02,
                    help="proportion of the population the model may flag")
    sc.add_argument("--out", help="write combined scores to this csv")
    sc.add_argument("--top", type=int, default=10)
    sc.set_defaults(func=cmd_score)

    rp = sub.add_parser("report", help="write the Excel workpaper")
    rp.add_argument("ledger", help="path to a GL csv")
    rp.add_argument("--labels", help="ground-truth csv, adds measured quality to the summary")
    rp.add_argument("--out", default="out/workpaper.xlsx")
    rp.add_argument("--top", type=int, default=250, help="exceptions to include")
    rp.add_argument("--no-model", action="store_true", help="rule tier only")
    rp.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    pd.set_option("display.width", 120)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
