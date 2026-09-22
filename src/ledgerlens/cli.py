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
from .generate import generate_ledger
from .ingest import load_csv, load_labels


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pd.set_option("display.width", 120)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
