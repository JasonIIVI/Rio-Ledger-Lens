"""Command line entry point.

    ledgerlens generate  - build a labelled synthetic ledger
    ledgerlens test      - run the journal-entry tests over a ledger
    ledgerlens benford   - run digit analysis, optionally segmented
    ledgerlens score     - run both tiers and compare them
    ledgerlens report    - write the Excel workpaper
    ledgerlens narrate   - write Claude narratives for the riskiest entries
    ledgerlens eval-narratives - grade the narrative layer against the case set
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import evaluate, jets, narrative_eval
from .benford import benford_test, segmented_benford
from .env import load_dotenv
from .generate import generate_ledger
from .ingest import load_csv, load_labels
from .model import combine, score_ledger
from .narrate import DEFAULT_EFFORT, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, NarrativeError, Narrator
from .report import build_workpaper
from .review import ReviewStore


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

    # Only an existing store is attached, and read-only: a report must never
    # create an empty review database, or migrate one, as a side effect.
    store = None
    if args.db and Path(args.db).exists():
        try:
            store = ReviewStore.read_only(args.db)
        except RuntimeError as exc:
            print(f"error: {exc}")
            return 2
    elif args.db:
        print(f"No review database at {args.db}; workpaper written without review columns")

    path = build_workpaper(
        scored, flags, args.out,
        benford=segmented_benford(df, by="account_code"),
        metrics=metrics, model_report=model_report, top_n=args.top, store=store,
    )
    print(f"Workpaper written to {path}")
    print("  {:,} entries in population, {:,} flagged".format(
        len(scored), int((scored["risk_score"] > 0).sum())))
    return 0


def cmd_narrate(args: argparse.Namespace) -> int:
    """Write Claude narratives for the riskiest entries and cache them in the review store."""
    df = load_csv(args.ledger)
    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)
    if not args.no_model:
        model_scores, _ = score_ledger(df)
        scored = combine(scored, model_scores)

    try:
        store = ReviewStore(args.db)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    skip = set() if args.force else store.narrative_ids()
    narrator = Narrator(model=args.model, max_tokens=args.max_tokens, effort=args.effort)
    try:
        result = narrator.narrate(scored, flags, df, top_n=args.top, skip=skip)
    except NarrativeError as exc:
        print(f"error: {exc}")
        return 1

    for entry_id, narrative in result.narratives.items():
        store.save_narrative(entry_id, narrative, model=result.model)

    print(result.describe())
    if skip:
        print(f"Skipped {len(skip)} entries already narrated in {args.db} (--force redoes them)")
    for entry_id, why in result.failures.items():
        print(f"  FAILED {entry_id}: {why}")
    if result.narratives:
        print(f"\n{len(result.narratives)} narrative(s) saved to {args.db}")
    elif result.entries_requested == 0:
        print("Nothing to narrate: every flagged entry in range already has a narrative")
    return 0 if result.narratives or result.entries_requested == 0 else 1


DEFAULT_CASES = "evals/narratives/cases.json"


def _not_the_default_ledger(ledger_sha256: str, what: str) -> str:
    return (
        f"refused: {what} is committed and belongs to the default synthetic ledger only "
        f"(sha256 {narrative_eval.DEFAULT_LEDGER_SHA256[:12]}); this ledger hashes to "
        f"{ledger_sha256[:12]}. Name a path outside evals/ for it, and never commit rows or "
        "cases built from real data."
    )


def cmd_eval_narratives(args: argparse.Namespace) -> int:
    """Run the narrative eval and write the report, or select a fresh case skeleton."""
    df = load_csv(args.ledger)
    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)
    model_scores, _ = score_ledger(df)
    scored = combine(scored, model_scores)
    # Rule 1 at the point of writing: the committed case file and runs
    # directory are for the generator's default output, nothing else.
    ledger_sha256 = narrative_eval.ledger_digest(df)
    default_ledger = ledger_sha256 == narrative_eval.DEFAULT_LEDGER_SHA256

    if args.select:
        if not args.labels:
            print("--select needs --labels: the label file decides which entries to test")
            return 2
        if args.cases == DEFAULT_CASES and not default_ledger:
            print(_not_the_default_ledger(ledger_sha256, f"the case file {DEFAULT_CASES}"))
            return 2
        if Path(args.cases).exists() and not args.overwrite:
            print(f"{args.cases} exists; --overwrite replaces it (hand-written expectations "
                  "would be lost)")
            return 2
        cases = narrative_eval.select_cases(scored, flags, load_labels(args.labels))
        path = narrative_eval.save_cases(cases, args.cases, generator={"ledger": args.ledger})
        print(f"Wrote {len(cases)} case skeleton(s) to {path}. Add must_mention and "
              "expected_confidence by hand before running.")
        return 0

    cases = narrative_eval.load_cases(args.cases)
    problems = narrative_eval.check_cases(cases, scored)
    if problems:
        print("The case set is stale for this ledger:")
        for problem in problems:
            print("  " + problem)
        return 2
    if args.limit:
        cases = cases[:args.limit]
    cases_sha256 = narrative_eval.cases_digest(args.cases)
    if args.runs_dir is None and not default_ledger:
        print(_not_the_default_ledger(
            ledger_sha256, f"the default runs directory {narrative_eval.RUNS_ROOT}/"))
        return 2
    try:
        runs_dir = Path(args.runs_dir) if args.runs_dir else narrative_eval.default_runs_dir(
            args.model, regrade=args.regrade)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2

    narrator = Narrator(model=args.model, max_tokens=args.max_tokens, effort=args.effort)
    try:
        rows = narrative_eval.run_eval(
            cases, narrator, scored, flags, df, runs_dir, resume=not args.no_resume,
            regrade=args.regrade, cases_sha256=cases_sha256,
            allow_cases_change=args.allow_cases_change, ledger_sha256=ledger_sha256,
        )
    except narrative_eval.CasesChangedError as exc:
        print(f"refused: {exc}")
        return 2
    except (FileNotFoundError, FileExistsError, narrative_eval.RegradeError,
            narrative_eval.ResultsError) as exc:
        print(f"error: {exc}")
        return 2
    except NarrativeError as exc:
        print(f"error: {exc}")
        return 1

    summary = narrative_eval.aggregate(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        narrative_eval.render_markdown(rows, summary, runs_dir=runs_dir, cases=cases),
        encoding="utf-8",
    )

    print("Graded {graded}/{cases} cases ({invalid} invalid, {errors} errors kept out of the "
          "score)".format(**summary))
    for metric, rate in summary["rates"].items():
        print(f"  {metric:22s} {rate:.0%}")
    if summary["missing"]:
        print(f"{summary['missing']} case(s) have no stored row and were not narrated: a "
              "re-grade never calls the API. Run without --regrade to narrate them.")
    print(f"Case file sha256 {cases_sha256}; ledger sha256 {ledger_sha256}"
          + ("" if default_ledger else " (not the default synthetic ledger)"))
    print(f"Report written to {out}; per-case rows in {runs_dir}")
    return 0 if summary["graded"] and not summary["errors"] and not summary["missing"] else 1


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
    rp.add_argument("--db", help="review database; adds narrative and decision columns")
    rp.set_defaults(func=cmd_report)

    n = sub.add_parser("narrate", help="write Claude narratives for the riskiest entries")
    n.add_argument("ledger", help="path to a GL csv")
    n.add_argument("--top", type=int, default=25, help="entries to narrate, highest risk first")
    n.add_argument("--db", default="data/review.sqlite", help="review database to cache into")
    n.add_argument("--model", default=DEFAULT_MODEL)
    n.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    n.add_argument("--effort", choices=("low", "medium", "high"), default=DEFAULT_EFFORT)
    n.add_argument("--no-model", action="store_true", help="rank by the rule tier only")
    n.add_argument("--force", action="store_true", help="re-narrate entries already cached")
    n.set_defaults(func=cmd_narrate)

    ev = sub.add_parser("eval-narratives", help="grade the narrative layer against the case set")
    ev.add_argument("ledger", help="path to the GL csv the cases were selected from")
    ev.add_argument("--cases", default=DEFAULT_CASES)
    ev.add_argument("--out", default="docs/narrative-eval.md", help="markdown report")
    ev.add_argument("--runs-dir",
                    help="per-case jsonl rows (default: evals/narratives/runs/<utc-date>-<model>, "
                         "or the newest such directory with --regrade)")
    ev.add_argument("--model", default=DEFAULT_MODEL)
    ev.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ev.add_argument("--effort", choices=("low", "medium", "high"), default=DEFAULT_EFFORT)
    ev.add_argument("--limit", type=int, help="grade only the first N cases")
    mode = ev.add_mutually_exclusive_group()
    mode.add_argument("--no-resume", action="store_true",
                      help="re-run every case into a fresh --runs-dir (stored rows are never "
                           "deleted)")
    mode.add_argument("--regrade", action="store_true",
                      help="re-score stored narratives with the current grader; never calls "
                           "the API")
    ev.add_argument("--allow-cases-change", action="store_true",
                    help="with --regrade: re-score rows graded under a different case file "
                         "(the report discloses it)")
    ev.add_argument("--select", action="store_true",
                    help="write a case skeleton chosen from --labels instead of running")
    ev.add_argument("--labels", help="ground-truth csv, only used with --select")
    ev.add_argument("--overwrite", action="store_true", help="let --select replace an existing file")
    ev.set_defaults(func=cmd_eval_narratives)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    pd.set_option("display.width", 120)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
