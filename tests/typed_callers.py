"""Calls a typed caller of the package must be able to write: checked by mypy, never run.

The package ships ``py.typed``, so its annotations are what another project's type checker
believes. ``Materiality`` and ``Controls`` are dataclasses whose fields hold what
``__post_init__`` has read (a Decimal, a sorted tuple), while their constructors take what
a person writes (a figure as a string, a list of names). Declared by the fields alone, every
call below was an error to mypy, and the package itself passed only because its own callers
hand over values mypy knows nothing about. ``[tool.mypy]`` lists this file, so CI's type
check fails if a constructor is narrowed again. pytest does not collect it (no ``test_``).
"""

import io
from decimal import Decimal

from ledgerlens.controls import Controls
from ledgerlens.ingest import load_csv
from ledgerlens.materiality import Materiality


def figures_as_a_person_writes_them() -> list[Materiality]:
    return [
        Materiality("50000"),
        Materiality(50_000, "0.75", "0.05"),
        Materiality(50_000.0, performance_share=0.75),
        Materiality(Decimal("50000"), Decimal("0.75"), Decimal("0.05")),
    ]


def controls_as_a_file_gives_them() -> list[Controls]:
    return [
        Controls(["mchen", "controller"], 10_000),
        Controls(("controller", "mchen"), 10_000.0, source="books.controls.json"),
        Controls(["controller"], Decimal("2500.50")),
    ]


def a_ledger_from_an_open_file(text: str) -> int:
    return len(load_csv(io.StringIO(text)))
