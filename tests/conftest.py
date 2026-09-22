"""Shared fixtures.

The full two-year ledger is generated once per session because it is the
expensive part; individual tests slice it rather than regenerating.
"""

from datetime import date

import pytest

from ledgerlens.generate import generate_ledger
from ledgerlens.ingest import prepare


@pytest.fixture(scope="session")
def raw_ledger():
    return generate_ledger(start=date(2024, 1, 1), end=date(2025, 12, 31), entries_per_day=10)


@pytest.fixture(scope="session")
def ledger(raw_ledger):
    lines, _ = raw_ledger
    return prepare(lines)


@pytest.fixture(scope="session")
def labels(raw_ledger):
    _, lbl = raw_ledger
    return lbl


@pytest.fixture(scope="session")
def small_ledger():
    lines, lbl = generate_ledger(
        start=date(2024, 1, 1), end=date(2024, 3, 31), entries_per_day=6
    )
    return prepare(lines), lbl
