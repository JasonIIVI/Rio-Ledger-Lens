"""Command line entry point.

    ledgerlens generate  - build a labelled synthetic ledger
    ledgerlens test      - run the journal-entry tests over a ledger
    ledgerlens benford   - run digit analysis, optionally segmented
    ledgerlens score     - run both tiers and compare them
    ledgerlens report    - write the Excel workpaper
    ledgerlens narrate   - write Claude narratives for the riskiest entries
    ledgerlens eval-narratives - grade the narrative layer against the case set
    ledgerlens adopt-legacy - file review rows from before ledgers were keyed under a ledger
    ledgerlens qbo-auth  - sign in to QuickBooks Online; tokens are kept outside the repo
    ledgerlens pull-qbo  - pull a period from QuickBooks Online into a ledger CSV
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import evaluate, jets, narrative_eval
from .benford import benford_test, segmented_benford
from .connectors import qbo
from .connectors.tokens import (
    Tokens,
    TokenStore,
    TokenStoreError,
    default_token_dir,
    ensure_private_dir,
    repository_root,
)
from .env import load_dotenv
from .generate import generate_ledger
from .ingest import IdentityError, ledger_identity, load_csv, load_labels
from .model import combine, score_ledger
from .narrate import DEFAULT_EFFORT, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, NarrativeError, Narrator
from .report import build_workpaper
from .review import LEGACY_LEDGER_ID, ReviewStore


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
            store = ReviewStore.read_only(args.db, ledger_identity(df, args.ledger))
        except (RuntimeError, IdentityError) as exc:
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
        ledger_id = ledger_identity(df, args.ledger)
        store = ReviewStore(args.db, ledger_id)
    except (RuntimeError, IdentityError) as exc:
        print(f"error: {exc}")
        return 2
    print(f"Review database {args.db}, rows keyed by {ledger_id}")
    # Rows from before ledgers were keyed cannot be told apart from this
    # ledger's by the file alone. Narrating over them would buy every note
    # again, so the choice is made explicit: adopt them, or start from scratch.
    ledgers = store.ledgers()
    legacy = ledgers[ledgers["ledger_id"] == LEGACY_LEDGER_ID]
    if not legacy.empty and ledger_id not in set(ledgers["ledger_id"]) and not args.ignore_legacy:
        print(f"refused: {args.db} holds {int(legacy.iloc[0].narratives)} narrated and "
              f"{int(legacy.iloc[0].decisions)} decided entries from before review rows were "
              f"keyed by ledger, and nothing yet for this ledger ({ledger_id}). If they were "
              f"written for it, run `ledgerlens adopt-legacy {args.ledger} --db {args.db}` first "
              "(no API calls); otherwise pass --ignore-legacy to start this ledger's notes from "
              "scratch.")
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


def cmd_adopt_legacy(args: argparse.Namespace) -> int:
    """File rows from before review data was keyed by ledger under this ledger."""
    if not Path(args.db).exists():
        print(f"error: no review database at {args.db}")
        return 2
    df = load_csv(args.ledger)
    try:
        ledger_id = ledger_identity(df, args.ledger)
        store = ReviewStore(args.db, ledger_id)  # migrates a schema-3 file on the way
        adopted = store.adopt_legacy(entry_ids=df["entry_id"].unique())
    except (RuntimeError, IdentityError) as exc:  # before ValueError: IdentityError is one
        print(f"error: {exc}")
        return 2
    except ValueError as exc:
        print(f"refused: {exc}")
        return 2
    if not adopted["narratives"] and not adopted["decisions"]:
        print(f"Nothing to adopt: {args.db} holds no legacy rows")
        return 0
    print(f"Adopted {adopted['narratives']} narrative(s) and {adopted['decisions']} decision(s) "
          f"into {ledger_id} in {args.db}. The legacy rows stay as they were.")
    return 0


def cmd_qbo_auth(args: argparse.Namespace) -> int:
    """Sign in to QuickBooks Online in the browser and store the tokens outside the repository."""
    try:
        config = qbo.QboConfig.from_env()
    except qbo.QboConfigError as exc:
        print(f"error: {exc}")
        return 2
    try:  # before the browser: a directory the tokens cannot go into wastes the sign-in
        ensure_private_dir(Path(args.token_dir) if args.token_dir else default_token_dir())
    except TokenStoreError as exc:
        print(f"error: {exc}")
        return 2
    auth = qbo.QboAuth(config, qbo.default_transport())
    state = qbo.new_state()
    try:
        server = qbo.CallbackServer(config.callback_port, config.callback_path)
    except qbo.QboAuthError as exc:
        print(f"error: {exc}")
        return 1
    url = auth.authorization_url(state)
    with server:
        print(f"Sign in to QuickBooks ({config.environment}) and choose the company to connect:")
        print(f"  {url}")
        if not args.no_browser:
            webbrowser.open(url)
        print(f"Waiting up to {args.timeout:g}s for the sign-in to return to {config.redirect_uri}")
        try:
            callback = server.wait(state, args.timeout)
        except qbo.QboAuthError as exc:
            print(f"error: {exc}")
            return 1
    if not qbo.REALM_ID.fullmatch(callback.realm_id):
        print(f"error: the sign-in returned a realm id this tool cannot use: {callback.realm_id!r}")
        return 1
    try:
        # Every check the save makes, before the single-use code is spent.
        store = TokenStore.for_realm(config.environment, callback.realm_id, args.token_dir)
        store.check_writable()
        path = store.save(auth.exchange(callback.code, callback.realm_id))
    except (qbo.QboError, TokenStoreError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"Signed in to {config.environment} company {callback.realm_id}. "
          f"Tokens saved to {path} (mode 600).")
    if config.realm_id != callback.realm_id:
        if config.realm_id:
            print(f"Note: QBO_REALM_ID in the environment is {config.realm_id}, but you signed in "
                  f"to {callback.realm_id}.")
        print(f"Add to .env: QBO_REALM_ID={callback.realm_id}")
    print("Next: ledgerlens pull-qbo --start YYYY-MM-DD --end YYYY-MM-DD")
    return 0


def _stored_realm(environment: str, token_dir: str | None) -> str | None:
    """The realm of the only token file for ``environment``, or None when there are none or several."""
    directory = Path(token_dir).expanduser() if token_dir else default_token_dir()
    found = sorted(directory.glob(f"qbo-{environment}-*.json")) if directory.is_dir() else []
    return found[0].stem[len(f"qbo-{environment}-"):] if len(found) == 1 else None


#: The synthetic ledger the committed eval and the README's numbers are built from.
SYNTHETIC_LEDGER = Path("data/ledger.csv")
FIXTURE_REALM = "sandbox-fixtures"


def _refuse_output(out: Path) -> str | None:
    """Why a pull may not write ``out`` (and its sidecar), or None.

    The sidecar is ``<stem>.identity.json``, so only a ``.csv`` name keeps it one-to-one
    with its ledger (``data/ledger.txt`` would take over ``data/ledger.csv``'s). The
    synthetic ledger is refused however it is spelled (case, ``..``, from another
    directory), by file identity. And pulled books stay in the repository's ignored
    ``data/`` directory, never anywhere git would pick them up.
    """
    if out.suffix.lower() != ".csv":
        return (f"--out must be a .csv file: its identity file is {out.stem}.identity.json, "
                "which a ledger with another suffix would share")
    root = repository_root(out.parent if out.parent.exists() else Path.cwd())
    synthetic = [Path.cwd() / SYNTHETIC_LEDGER] + ([root / SYNTHETIC_LEDGER] if root else [])
    if out.exists() and any(s.exists() and out.samefile(s) for s in synthetic):
        return (f"{out} is the synthetic ledger the eval and the README's numbers come from; "
                "choose another --out (the default is data/qbo-ledger.csv)")
    if root is not None:
        data = root / "data"
        inside = (out.parent.exists() and data.exists() and out.parent.samefile(data)) or \
            out.resolve().parent == data.resolve()
        if not inside:
            return (f"{out} is inside the git checkout at {root} but not in its data/ directory, "
                    "where git ignores ledgers; pulled books are never written where they could "
                    "be committed")
    return None


def cmd_pull_qbo(args: argparse.Namespace) -> int:
    """Pull a period from QuickBooks Online (or replay fixtures) into a ledger CSV and sidecar."""
    if args.start > args.end:
        print(f"error: --start {args.start} is after --end {args.end}")
        return 2
    out = Path(args.out)
    refusal = _refuse_output(out)
    if refusal:
        print(f"refused: {refusal}")
        return 2
    if args.realm_id and not qbo.REALM_ID.fullmatch(args.realm_id):
        print(f"error: --realm-id {args.realm_id!r} is not letters, digits, '_' and '-' starting "
              "with a letter or digit")
        return 2
    store = None
    if args.fixtures:
        realm = args.realm_id or FIXTURE_REALM
        try:  # a replay converts times the way the recorded pull did
            config = qbo.QboConfig.for_fixtures(realm, os.environ.get("QBO_TIMEZONE") or None)
        except qbo.QboConfigError as exc:
            print(f"error: {exc}")
            return 2
        tokens = Tokens.issued("TEST-ACCESS", "TEST-REFRESH", 3600, 86400, realm, "sandbox")
        try:
            transport = qbo.RecordedTransport(args.fixtures)
        except FileNotFoundError as exc:
            print(f"error: {exc}")
            return 2
    else:
        try:
            config = qbo.QboConfig.from_env()
        except qbo.QboConfigError as exc:
            print(f"error: {exc}")
            return 2
        if config.environment == "production" and args.record:
            print("refused: --record writes fixtures into the repository, and only sandbox data "
                  "may go there")
            return 2
        if config.environment == "production" and not args.allow_production:
            print("refused: QBO_ENVIRONMENT is production. This repository works on sandbox "
                  "companies; pass --allow-production to pull real books anyway (the output stays "
                  "under data/, which git ignores)")
            return 2
        realm = args.realm_id or config.realm_id or _stored_realm(config.environment, args.token_dir)
        if not realm:
            print("error: no realm id: set QBO_REALM_ID in .env, pass --realm-id, or run "
                  "`ledgerlens qbo-auth` first")
            return 2
        if not qbo.REALM_ID.fullmatch(realm):
            print(f"error: realm id {realm!r} is not letters, digits, '_' and '-' starting with a "
                  "letter or digit")
            return 2
        config = config.with_realm(realm)
        try:
            store = TokenStore.for_realm(config.environment, realm, args.token_dir)
            tokens = store.load()
            store.check_writable()  # a refresh rotates the token: it must be savable first
        except (TokenStoreError, ValueError) as exc:
            print(f"error: {exc}")
            return 2
        if tokens is None:
            print(f"error: no tokens for {config.environment} company {realm} at {store.path}; "
                  "run `ledgerlens qbo-auth` first")
            return 2
        transport = None
        if args.record:
            record_dir = Path(args.record)
            if record_dir.exists() and not record_dir.is_dir():
                print(f"refused: --record {record_dir} is a file, not a directory")
                return 2
            if record_dir.is_dir() and any(record_dir.glob("*.json")):
                print(f"refused: {record_dir} already holds fixtures; a recording replaces them "
                      "whole, so remove them first (git rm) and record again")
                return 2
            try:  # before any request, so a recording can never fail after one was answered
                record_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"error: cannot create {record_dir}: {exc}")
                return 2
            transport = qbo.Recorder(qbo.default_transport(), record_dir, realm)
        transport = transport or qbo.default_transport()

    client = qbo.QboClient(config, tokens, transport, token_store=store)
    try:
        frame, stats = qbo.pull(client, args.start, args.end)
    except qbo.QboError as exc:
        print(f"error: {exc}")
        return 1
    except qbo.UnexpectedRequest as exc:
        print(f"error: the fixtures do not answer this pull: {exc}")
        return 1
    except ValueError as exc:  # no transactions in range, or a report the mapping refuses
        print(f"error: {exc}")
        return 1

    path = qbo.write_ledger_csv(frame, out)
    sidecar = qbo.write_identity(path, realm, config.environment, args.start, args.end)
    for line in stats.describe():
        print(line)
    entries = frame["entry_id"].nunique()
    print(f"Wrote {len(frame):,} lines / {entries:,} entries to {path}")
    print(f"Identity qbo:{realm} written to {sidecar}")
    if entries < qbo.SMALL_LEDGER:
        print(f"Warning: {entries} entries is a small population; the model tier and Benford "
              "analysis need far more to say anything.")
    if isinstance(transport, qbo.RecordedTransport) and transport.unused:
        print(f"Warning: fixtures not used by this pull: {', '.join(transport.unused)}")
    if isinstance(transport, qbo.Recorder):
        transport.finish()
        print(f"Recorded {len(transport.written)} sanitized fixture(s) in {transport.out_dir}; "
              "read them before committing")
        if transport.failures:
            print("error: some exchanges could not be recorded, so the set is incomplete:")
            for failure in transport.failures:
                print(f"  {failure}")
            return 1
    print(f"Next: ledgerlens test {path}   ledgerlens report {path} --db data/review.sqlite")
    return 0


DEFAULT_CASES = str(narrative_eval.CASES_FILE)


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _path_arg(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("an empty path names no destination")
    return text


def cmd_eval_narratives(args: argparse.Namespace) -> int:
    """Run the narrative eval and write the report, or select a fresh case skeleton."""
    if args.select and not args.labels:
        print("--select needs --labels: the label file decides which entries to test")
        return 2
    df = load_csv(args.ledger)
    # Rule 1 at the point of writing, before anything is scored: the committed
    # case file, runs directory and report are for the generator's default
    # output and nothing else, however a path is spelled.
    ledger_sha256 = narrative_eval.ledger_digest(df)
    default_ledger = ledger_sha256 == narrative_eval.DEFAULT_LEDGER_SHA256
    try:
        if args.select:
            narrative_eval.refuse_committed_path(args.cases, ledger_sha256, "the case file")
        else:
            runs_dir = (Path(args.runs_dir) if args.runs_dir is not None
                        else narrative_eval.default_runs_dir(args.model, regrade=args.regrade))
            narrative_eval.refuse_committed_path(runs_dir, ledger_sha256, "the runs directory")
            narrative_eval.refuse_committed_path(args.out, ledger_sha256, "the report")
    except narrative_eval.CommittedPathError as exc:
        print(f"refused: {exc}")
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2

    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)
    model_scores, _ = score_ledger(df)
    scored = combine(scored, model_scores)

    if args.select:
        if Path(args.cases).exists() and not args.overwrite:
            print(f"{args.cases} exists; --overwrite replaces it (hand-written expectations "
                  "would be lost)")
            return 2
        cases = narrative_eval.select_cases(scored, flags, load_labels(args.labels))
        path = narrative_eval.save_cases(cases, args.cases, generator={"ledger": args.ledger},
                                         ledger_sha256=ledger_sha256)
        print(f"Wrote {len(cases)} case skeleton(s) to {path}. Add must_mention and "
              "expected_confidence by hand before running.")
        return 0

    try:
        cases = narrative_eval.load_cases(args.cases)
    except FileNotFoundError:
        print(f"error: case file not found: {args.cases} (run from the repository root, or pass "
              "--cases PATH)")
        return 2
    except (OSError, ValueError) as exc:  # unreadable, not JSON, or a case the loader refuses
        print(f"error: {args.cases}: {exc}")
        return 2
    problems = narrative_eval.check_cases(cases, scored, df)
    if problems:
        print("The case set is stale for this ledger:")
        for problem in problems:
            print("  " + problem)
        if args.cases == DEFAULT_CASES and not default_ledger:
            print(f"({DEFAULT_CASES} belongs to the default synthetic ledger; select cases for "
                  "this ledger with --select --labels LABELS --cases PATH, outside evals/)")
        return 2
    if args.limit is not None:
        cases = cases[:args.limit]
    cases_sha256 = narrative_eval.cases_digest(args.cases)

    narrator = Narrator(model=args.model, max_tokens=args.max_tokens, effort=args.effort)
    try:
        rows = narrative_eval.run_eval(
            cases, narrator, scored, flags, df, runs_dir, resume=not args.no_resume,
            regrade=args.regrade, cases_sha256=cases_sha256,
            allow_cases_change=args.allow_cases_change,
        )
    except (narrative_eval.CasesChangedError, narrative_eval.CommittedPathError,
            narrative_eval.LedgerChangedError) as exc:
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
    n.add_argument("--ignore-legacy", action="store_true",
                   help="narrate this ledger from scratch even if the database holds rows from "
                        "before review rows were keyed by ledger")
    n.set_defaults(func=cmd_narrate)

    al = sub.add_parser("adopt-legacy",
                        help="file review rows from before ledgers were keyed under this ledger")
    al.add_argument("ledger", help="path to the GL csv the rows were written for")
    al.add_argument("--db", default="data/review.sqlite", help="review database holding them")
    al.set_defaults(func=cmd_adopt_legacy)

    ev = sub.add_parser("eval-narratives", help="grade the narrative layer against the case set")
    ev.add_argument("ledger", help="path to the GL csv the cases were selected from")
    ev.add_argument("--cases", type=_path_arg, default=DEFAULT_CASES)
    ev.add_argument("--out", type=_path_arg, default=str(narrative_eval.REPORT_FILE),
                    help="markdown report")
    ev.add_argument("--runs-dir", type=_path_arg,
                    help="per-case jsonl rows (default: evals/narratives/runs/<utc-date>-<model>, "
                         "or the newest such directory with --regrade)")
    ev.add_argument("--model", default=DEFAULT_MODEL)
    ev.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ev.add_argument("--effort", choices=("low", "medium", "high"), default=DEFAULT_EFFORT)
    ev.add_argument("--limit", type=_positive_int, help="grade only the first N cases")
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

    qa = sub.add_parser("qbo-auth", help="sign in to QuickBooks Online; tokens are kept outside "
                                         "the repository")
    qa.add_argument("--token-dir", help="where tokens are kept (default: $LEDGERLENS_TOKEN_DIR, "
                                        "else ~/.config/ledgerlens)")
    qa.add_argument("--timeout", type=float, default=300, help="seconds to wait for the sign-in")
    qa.add_argument("--no-browser", action="store_true", help="print the sign-in URL only")
    qa.set_defaults(func=cmd_qbo_auth)

    pq = sub.add_parser("pull-qbo", help="pull a period from QuickBooks Online into a ledger CSV")
    pq.add_argument("--start", type=_parse_date, required=True)
    pq.add_argument("--end", type=_parse_date, required=True)
    pq.add_argument("--out", type=_path_arg, default="data/qbo-ledger.csv",
                    help="ledger CSV to write; its .identity.json sidecar is written beside it")
    pq.add_argument("--realm-id", help="company to pull (default: QBO_REALM_ID, else the only "
                                       "stored token file)")
    pq.add_argument("--token-dir", help="where tokens are kept")
    source = pq.add_mutually_exclusive_group()
    source.add_argument("--record", metavar="DIR",
                        help="also write each exchange, sanitized, as a fixture in DIR (sandbox only)")
    source.add_argument("--fixtures", metavar="DIR",
                        help="replay recorded fixtures from DIR instead of calling QuickBooks")
    pq.add_argument("--allow-production", action="store_true",
                    help="permit QBO_ENVIRONMENT=production")
    pq.set_defaults(func=cmd_pull_qbo)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    pd.set_option("display.width", 120)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
