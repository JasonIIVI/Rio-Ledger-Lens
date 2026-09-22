from datetime import date

import pytest

from ledgerlens.generate import generate_ledger
from ledgerlens.schema import AnomalyType


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
