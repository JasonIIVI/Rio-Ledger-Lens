import calendar
import warnings
from collections import Counter
from datetime import date, timedelta

import pandas as pd
import pytest

from ledgerlens import jets
from ledgerlens.coa import COMMON_PAIRS
from ledgerlens.generate import _VENDORS, HELD_OUT_SEED, ArchetypeSkipped, generate_ledger
from ledgerlens.ingest import prepare
from ledgerlens.schema import AnomalyType, HeldOutType, is_us_holiday

#: Seeds the held-out recipes may be developed and tested on: any but HELD_OUT_SEED.
#: The first is the default ledger's.
DEV_SEEDS = (20260922, 101, 202, 303)

#: The held-out ledger as measured from the commit tagged held-out-freeze (HELD_OUT_SEED).
HELD_OUT_LEDGER_SHA256 = "3a3ab764870bb294854747ef24dc38591032cf5d79cbf1577a92737c6838f09c"

#: The default ledger with the held-out rows, generated on DEV_SEEDS[0]: pins the recipes.
DEV_HELD_OUT_SHA256 = "d5182b57029fb7d7dfc3d2b6d0ea6bb34d7779a7fda7189b35cfeca9bc24c94d"


def test_generation_is_deterministic():
    a, _ = generate_ledger(end=date(2024, 3, 31), seed=42)
    b, _ = generate_ledger(end=date(2024, 3, 31), seed=42)
    assert a.equals(b)


def test_different_seeds_differ():
    a, _ = generate_ledger(end=date(2024, 3, 31), seed=1)
    b, _ = generate_ledger(end=date(2024, 3, 31), seed=2)
    assert not a.equals(b)


def test_entries_balance_except_injected_exceptions(ledger, labels):
    totals = ledger.groupby("entry_id")[["debit", "credit"]].sum()
    imbalanced = totals[(totals["debit"] - totals["credit"]).abs() > 0.005]
    unbalanced_ids = set(
        labels.loc[labels["anomaly_type"] == AnomalyType.UNBALANCED_ENTRY, "entry_id"]
    )
    # The only entries allowed to be out of balance are the ones we injected.
    assert set(imbalanced.index) == unbalanced_ids


def test_every_archetype_is_injected(labels):
    injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
    assert injected == set(AnomalyType.ALL)


def test_anomaly_rate_is_close_to_requested(labels):
    assert 0.01 <= labels["is_anomaly"].mean() <= 0.025


def test_labels_cover_every_entry(ledger, labels):
    assert set(ledger["entry_id"]) == set(labels["entry_id"])


def test_normal_entries_are_on_business_days(ledger, labels):
    normal_ids = set(labels.loc[~labels["is_anomaly"], "entry_id"])
    normal = ledger[ledger["entry_id"].isin(normal_ids)]
    # Injected weekend/holiday entries are the exception; the baseline is not.
    assert not normal["is_weekend"].any()
    assert not normal["is_holiday"].any()


def test_rejects_impossible_parameters():
    with pytest.raises(ValueError):
        generate_ledger(anomaly_rate=1.5)
    with pytest.raises(ValueError):
        generate_ledger(start=date(2025, 1, 1), end=date(2024, 1, 1))


# --- ranges too short for some archetypes --------------------------------------
# The calendar is worked out here from first principles, not through the
# generator's own helpers, so the expectation cannot share a mistake with them.

def _days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _lacking(start, end):
    """The archetypes a range has no day for, as this test reads the calendar."""
    days = _days(start, end)
    lacking = set()
    if not any(d.weekday() >= 5 for d in days):
        lacking.add(AnomalyType.WEEKEND_ENTRY)
    if not any(is_us_holiday(d) and d.weekday() < 5 for d in days):
        lacking.add(AnomalyType.HOLIDAY_ENTRY)
    if not any(calendar.monthrange(d.year, d.month)[1] - d.day <= 2 for d in days):
        lacking.add(AnomalyType.PERIOD_END_MANUAL_REVENUE)
    return lacking


def _has_business_day(start, end):
    return any(d.weekday() < 5 and not is_us_holiday(d) for d in _days(start, end))


def _generate_noting_skips(start, end):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lines, labels = generate_ledger(start=start, end=end, entries_per_day=2)
    return lines, labels, [str(w.message) for w in caught if issubclass(w.category, UserWarning)]


@pytest.mark.parametrize("start, end, lacking", [
    # The reported case: New Year's Day and a weekend, but no period end.
    (date(2024, 1, 1), date(2024, 1, 10), {AnomalyType.PERIOD_END_MANUAL_REVENUE}),
    (date(2024, 1, 2), date(2024, 1, 5), {AnomalyType.WEEKEND_ENTRY, AnomalyType.HOLIDAY_ENTRY,
                                          AnomalyType.PERIOD_END_MANUAL_REVENUE}),
    (date(2024, 1, 29), date(2024, 1, 31), {AnomalyType.WEEKEND_ENTRY, AnomalyType.HOLIDAY_ENTRY}),
    # March and April hold no federal holiday: a two-month ledger can lack one.
    (date(2024, 3, 1), date(2024, 4, 30), {AnomalyType.HOLIDAY_ENTRY}),
])
def test_a_short_range_skips_what_it_cannot_place_and_says_so(start, end, lacking):
    assert _lacking(start, end) == lacking  # the cases are what they claim to be
    lines, labels, notes = _generate_noting_skips(start, end)
    injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
    assert injected == set(AnomalyType.ALL) - lacking
    assert set(lines["entry_id"]) == set(labels["entry_id"])
    assert len(notes) == len(lacking)
    for archetype in lacking:
        (note,) = [n for n in notes if archetype in n]
        assert note.startswith(f"{start}..{end} has ")
        assert "\n" not in note


def test_a_range_with_no_business_day_is_refused():
    # Saturday, Sunday and Martin Luther King Jr. Day: no normal activity at all,
    # so every archetype drawn from business days would have nowhere to go.
    with pytest.raises(ValueError, match=r"^2024-01-13\.\.2024-01-15 has no business day"):
        generate_ledger(start=date(2024, 1, 13), end=date(2024, 1, 15))


def test_every_one_and_three_day_range_of_a_year_generates_or_is_refused():
    for offset in range(366):
        start = date(2024, 1, 1) + timedelta(days=offset)
        for end in (start, start + timedelta(days=2)):
            if not _has_business_day(start, end):
                with pytest.raises(ValueError, match="no business day"):
                    generate_ledger(start=start, end=end, entries_per_day=2)
                continue
            _, labels, notes = _generate_noting_skips(start, end)
            injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
            assert injected == set(AnomalyType.ALL) - _lacking(start, end), (start, end)
            assert len(notes) == len(_lacking(start, end)), (start, end)


# --- the held-out ledger ---------------------------------------------------------
# The recipes are tested on DEV_SEEDS. HELD_OUT_SEED was generated with once, after the code
# was frozen; that ledger is pinned below and its measurement reproduced in test_limitations.


@pytest.fixture(scope="module")
def held_out_pairs():
    """For each development seed: the default-range ledger with and without the held-out rows."""
    pairs = {}
    for seed in DEV_SEEDS:
        pairs[seed] = (generate_ledger(seed=seed, held_out=True), generate_ledger(seed=seed))
    return pairs


def _held_out_ids(labels):
    return set(labels.loc[labels["anomaly_type"].isin(HeldOutType.ALL), "entry_id"])


def test_the_development_seeds_are_not_the_held_out_one():
    assert HELD_OUT_SEED not in DEV_SEEDS and len(set(DEV_SEEDS)) == 4
    assert HeldOutType.ALL and not set(HeldOutType.ALL) & set(AnomalyType.ALL)


def test_without_its_held_out_rows_the_ledger_is_the_one_the_seed_gives_alone(held_out_pairs):
    for seed, ((lines, labels), (plain_lines, plain_labels)) in held_out_pairs.items():
        ids = _held_out_ids(labels)
        assert len(ids) == 43, seed
        rest = lines[~lines["entry_id"].isin(ids)].reset_index(drop=True)
        pd.testing.assert_frame_equal(rest, plain_lines)
        pd.testing.assert_frame_equal(
            labels[~labels["entry_id"].isin(ids)].reset_index(drop=True), plain_labels)
        # the held-out ids come after every other entry's
        number = lines["entry_id"].str[-6:].astype(int)
        assert number[lines["entry_id"].isin(ids)].min() > number[~lines["entry_id"].isin(ids)].max()


def test_the_held_out_rows_draw_from_their_own_streams():
    """A different anomaly rate moves every main draw; the held-out entries stay as they were."""
    def held_out_rows(rate):
        lines, labels = generate_ledger(start=date(2024, 1, 1), end=date(2024, 6, 30),
                                        anomaly_rate=rate, seed=DEV_SEEDS[1], held_out=True)
        rows = lines[lines["entry_id"].isin(_held_out_ids(labels))]
        return rows.drop(columns="entry_id").sort_values(
            ["posting_date", "entered_at", "line_no"]).reset_index(drop=True)

    pd.testing.assert_frame_equal(held_out_rows(0.015), held_out_rows(0.04))


def test_no_rule_fires_on_a_held_out_entry_or_moves_on_any_other(held_out_pairs):
    """Rule silence, the premise of the held-out measurement: zero flags on the held-out
    entries, and the same (entry, test) flags on every other entry as without them."""
    for seed, ((lines, labels), (plain_lines, _)) in held_out_pairs.items():
        flags = jets.run_all(prepare(lines.copy()))
        ids = _held_out_ids(labels)
        on_held_out = flags[flags["entry_id"].isin(ids)]
        assert on_held_out.empty, (seed, sorted(set(on_held_out["test_id"])))
        plain = jets.run_all(prepare(plain_lines.copy()))
        # counted, not as a set: JET-09 and JET-11 judge lines, and a line can stop firing
        assert (Counter(zip(flags["entry_id"], flags["test_id"]))
                == Counter(zip(plain["entry_id"], plain["test_id"]))), seed


def test_the_held_out_recipes_hold_their_bands(held_out_pairs):
    for seed, ((lines, labels), _) in held_out_pairs.items():
        kinds = labels.set_index("entry_id")["anomaly_type"]
        rows = lines[lines["entry_id"].isin(_held_out_ids(labels))].copy()
        rows["kind"] = rows["entry_id"].map(kinds)
        entries = rows[rows["line_no"] == 1].set_index("entry_id")
        credits = rows[rows["line_no"] == 2].set_index("entry_id")
        assert (entries["debit"] == credits["credit"]).all()  # balanced
        counts = entries["kind"].value_counts().to_dict()
        assert counts == {"token_amount": 8, "unfamiliar_user": 8, "split_purchase": 9,
                          "round_trip": 8, "shell_vendor_drip": 10}, seed
        amount = entries["debit"]
        assert ((amount * 100).round() % 100 != 0).all()  # cents on every one
        assert not ((amount >= 9_700) & (amount < 10_000)).any()
        assert not entries["source"].isin(["Manual", "System"]).any()
        pairs = set(zip(rows.loc[rows["line_no"] == 1, "account_code"],
                        rows.loc[rows["line_no"] == 2, "account_code"]))
        assert pairs <= set(COMMON_PAIRS)
        stamp = pd.to_datetime(entries["entered_at"])
        assert stamp.dt.hour.between(8, 17).all()
        days = pd.to_datetime(entries["posting_date"]).dt.date
        assert all(d.weekday() < 5 and not is_us_holiday(d) for d in days)
        assert (stamp.dt.date == days).all()

        kind = entries["kind"]
        assert amount[kind == "token_amount"].between(0.50, 2.99).all()
        assert entries.loc[kind == "unfamiliar_user", "created_by"].isin(
            ["temp01", "temp02", "svc_import", "admin2"]).all()
        assert not entries.loc[kind != "unfamiliar_user", "created_by"].isin(
            ["temp01", "temp02", "svc_import", "admin2"]).any()
        splits = entries[kind == "split_purchase"]
        for _, group in splits.groupby([splits["posting_date"], splits["description"].str.split(" - vendor ").str[1]]):
            assert len(group) == 3 and group["debit"].nunique() == 3
            assert 10_050 <= group["debit"].sum() <= 10_800 and (group["debit"] < 9_700).all()
        trips = entries[kind == "round_trip"].sort_values("posting_date")
        business = [d for d in pd.date_range("2024-01-01", "2025-12-31").date
                    if d.weekday() < 5 and not is_us_holiday(d)]
        ins = trips[trips["account_code"] == "1000"]
        outs = trips[trips["account_code"] == "6500"]
        assert len(ins) == len(outs) == 4
        for _, leg in ins.iterrows():
            back = business[business.index(pd.Timestamp(leg["posting_date"]).date()) + 3]
            match = outs[(pd.to_datetime(outs["posting_date"]).dt.date == back) & (outs["debit"] == leg["debit"])]
            assert len(match) >= 1
        drips = entries[kind == "shell_vendor_drip"]
        assert drips["debit"].between(150, 450).all()
        for vendor, group in drips.groupby(drips["description"]):
            dates = sorted(pd.to_datetime(group["posting_date"]).dt.date)
            assert len(dates) == 5 and vendor.split(" - ")[1] not in _VENDORS
            # a fortnight, each payment moved to the next business day: 11 to 17 days apart
            assert all(11 <= (b - a).days <= 17 for a, b in zip(dates, dates[1:]))


def test_a_short_range_leaves_out_a_held_out_archetype_it_has_no_room_for():
    with pytest.warns(ArchetypeSkipped) as caught:
        lines, labels = generate_ledger(start=date(2024, 1, 2), end=date(2024, 1, 4),
                                        seed=DEV_SEEDS[2], held_out=True)
    said = " ".join(str(w.message) for w in caught)
    assert "round_trip" in said and "shell_vendor_drip" in said
    kinds = set(labels["anomaly_type"]) & set(HeldOutType.ALL)
    assert kinds == {"token_amount", "unfamiliar_user", "split_purchase"}


def test_the_development_held_out_ledger_is_pinned(held_out_pairs):
    """Any change to a held-out recipe before the freeze moves this, and says so."""
    from ledgerlens.ingest import ledger_digest

    (lines, _), _ = held_out_pairs[DEV_SEEDS[0]]
    assert ledger_digest(prepare(lines.copy())) == DEV_HELD_OUT_SHA256


def test_the_limitations_list_each_held_out_archetype_with_its_entries():
    """docs/limitations.md registers the measurement; its counts are the generator's."""
    from pathlib import Path

    from ledgerlens.generate import HELD_OUT_MIX

    doc = (Path(__file__).resolve().parents[1] / "docs" / "limitations.md").read_text(encoding="utf-8")
    per_group = {"split_purchase": 3, "round_trip": 2, "shell_vendor_drip": 5}
    entries = {kind: n * per_group.get(kind, 1) for kind, n in HELD_OUT_MIX.items()}
    assert list(entries) == list(HeldOutType.ALL) and sum(entries.values()) == 43
    for kind, n in entries.items():
        assert doc.count(f"| `{kind}` | {n} |") == 2, kind  # the recipe table and the registered one
    assert "| all held-out | 43 |" in doc and "how many of 43 the budget holds" in doc


def test_the_measured_held_out_ledger_is_pinned(held_out_raw):
    """The ledger docs/limitations.md measured, in ledger_digest's canonical form."""
    from ledgerlens.ingest import ledger_digest

    lines, labels = held_out_raw
    assert ledger_digest(prepare(lines.copy())) == HELD_OUT_LEDGER_SHA256
    assert len(_held_out_ids(labels)) == 43 and int(labels["is_anomaly"].sum()) == 120
