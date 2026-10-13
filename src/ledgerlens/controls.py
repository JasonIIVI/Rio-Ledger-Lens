"""A company's own controls: who may post manual entries, and the approval limit.

JET-12 asks whether a manual entry was keyed by someone on the approver list,
and JET-06 whether an entry sits just beneath the approval limit. Both are facts
about a company, not thresholds to tune (rule 7): the synthetic company's are the
defaults, and any other company states its own in a file beside its ledger,
``<stem>.controls.json``, placed the way the identity sidecar is::

    {"version": 1, "manual_entry_approvers": ["controller", "mchen"], "approval_limit": 10000}

There is no flag and no environment variable, so every surface that reads a
ledger (the CLI, the dashboard, the MCP server, the workpaper) tests it against
the same controls. A file that is present but cannot be used is refused, never
replaced by the defaults: testing a company against another company's approver
list would raise a flag on every manual entry and say nothing about why.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .ingest import quote

CONTROLS_SUFFIX = ".controls.json"
VERSION = 1
KEYS = ("version", "manual_entry_approvers", "approval_limit")
#: A user name, not a sentence: an approver is printed inside JET-12's reason, and the
#: reason reaches the narration prompt.
MAX_APPROVER_LENGTH = 64


class ControlsError(ValueError):
    """A controls file, or controls built in code, that cannot be used."""


def _shown(value: object) -> str:
    """A value from the file as a message quotes it; a JSON number as it was written."""
    return str(value) if isinstance(value, Decimal) else quote(value)


def _visible(name: str) -> bool:
    """Whether ``name`` prints as itself everywhere it is shown.

    The test is ``summary.one_line``'s, which the surfaces print ledger text
    through: nothing it removes (a control, an invisible or reordering character)
    and no whitespace it would collapse or strip. A name that changed on the way
    to the screen would not be the name JET-12 compares.
    """
    if not name.isprintable() or name != " ".join(name.split()):
        return False
    if name.isascii():
        return True  # nothing else one_line removes is ASCII
    from .summary import one_line  # deferred: summary imports jets, which imports this module
    return one_line(name) == name


def _approver_problems(approvers: object) -> list[str]:
    if not isinstance(approvers, (list, tuple)):
        return [f"manual_entry_approvers must be a list of user names, got {_shown(approvers)}"]
    if not approvers:
        return ["manual_entry_approvers is empty; name at least one user who may post "
                "manual entries"]
    problems, seen = [], set()
    for name in approvers:
        if not isinstance(name, str):
            problems.append(f"approver {_shown(name)} is not a string")
            continue
        if not name:
            problems.append("an approver is an empty string")
        elif "," in name:
            problems.append(f"approver {quote(name)} holds a comma, which separates the names "
                            "in JET-12's reason")
        elif len(name) > MAX_APPROVER_LENGTH:
            problems.append(f"approver {quote(name)} is longer than {MAX_APPROVER_LENGTH} "
                            "characters")
        elif not _visible(name):
            problems.append(f"approver {quote(name)} is not one line of visible text (a control "
                            "or invisible character, or a space at either end or doubled)")
        elif name in seen:
            problems.append(f"approver {quote(name)} is listed twice")
        seen.add(name)
    return problems


def _limit(value: object) -> tuple[float | None, str | None]:
    """``(the limit in dollars, None)``, or ``(None, why it cannot be used)``."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        hint = ' (write 10000, not "10,000")' if isinstance(value, str) else ""
        return None, f"approval_limit must be a number of dollars, got {_shown(value)}{hint}"
    # Judged as written (a file's number is read as a Decimal, never rounded through a
    # float first), then required to come back unchanged from the float JET-06 compares
    # with: 1e999, or more significant digits than a float holds, would be compared as
    # some other limit than the one stated.
    amount = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    if not amount.is_finite() or amount <= 0:
        return None, f"approval_limit must be a finite number above 0, got {amount}"
    dollars = float(amount)
    if not math.isfinite(dollars) or Decimal(repr(dollars)) != amount:
        return None, f"approval_limit {amount} has more digits than can be compared exactly"
    cents = amount * 100  # exact: at most 17 significant digits survive the check above
    if cents != cents.to_integral_value():
        return None, f"approval_limit {amount} is not a whole number of cents"
    return dollars, None


@dataclass(frozen=True)
class Controls:
    """The approver list JET-12 reads and the limit JET-06 reads.

    Approvers are kept sorted, so a file listing the defaults in another order is
    the defaults, and JET-12's reason names them in one order wherever it is read.
    ``source`` is the file they were read from (None for the defaults, or controls
    built in code); it is not part of what two controls are compared by.
    """

    manual_entry_approvers: tuple[str, ...]
    approval_limit: float
    source: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        problems = _approver_problems(self.manual_entry_approvers)
        limit, problem = _limit(self.approval_limit)
        if problem:
            problems.append(problem)
        if problems:
            raise ControlsError("; ".join(problems))
        object.__setattr__(self, "manual_entry_approvers",
                           tuple(sorted(self.manual_entry_approvers)))
        object.__setattr__(self, "approval_limit", limit)

    @property
    def is_default(self) -> bool:
        """Whether these are the synthetic company's controls, wherever they were read from."""
        return self == DEFAULT_CONTROLS

    def describe(self) -> str:
        approvers = format_approvers(self.manual_entry_approvers)
        return f"approvers {approvers} (JET-12); approval limit {format_limit(self.approval_limit)} (JET-06)"

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "default": self.is_default,
            "manual_entry_approvers": list(self.manual_entry_approvers),
            "approval_limit": self.approval_limit,
        }


#: The synthetic company's controls, which the generator's ledger was built against.
#: A test ties them to ``generate.MANUAL_JE_APPROVERS`` and ``APPROVAL_THRESHOLD``.
DEFAULT_CONTROLS = Controls(("controller", "mchen"), 10_000.0)


def format_approvers(approvers: tuple[str, ...]) -> str:
    return ", ".join(approvers)


def format_limit(limit: float) -> str:
    """``$10,000``, or ``$9,999.50`` for a limit with cents (``:,.0f`` would round them away)."""
    return f"${limit:,.0f}" if float(limit).is_integer() else f"${limit:,.2f}"


def controls_path(ledger_path: str | Path) -> Path:
    """Where a ledger's controls sit: ``data/ledger.controls.json`` beside ``data/ledger.csv``."""
    return Path(ledger_path).with_suffix(CONTROLS_SUFFIX)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    twice = sorted({key for key in keys if keys.count(key) > 1})
    if twice:
        # json.loads would keep the last one silently: which of two limits was meant?
        raise ControlsError("key(s) given twice: " + ", ".join(quote(key) for key in twice))
    return dict(pairs)


def _no_constant(name: str) -> object:
    raise ControlsError(f"{name} is not a number (JSON has no NaN or Infinity)")


def parse_controls(text: str) -> Controls:
    """Controls from the text of a controls file; :class:`ControlsError` says why not."""
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_keys,
                             parse_constant=_no_constant, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ControlsError(f"not JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise ControlsError(f"must hold a JSON object with the keys {', '.join(KEYS)}")
    problems = []
    unknown = sorted(set(payload) - set(KEYS))
    if unknown:
        problems.append("unknown key(s): " + ", ".join(quote(key) for key in unknown))
    missing = [key for key in KEYS if key not in payload]
    if missing:
        problems.append("missing key(s): " + ", ".join(missing))
    version = payload.get("version")
    if "version" in payload and not (type(version) is int and version == VERSION):
        problems.append(f"version must be {VERSION}, got {_shown(version)}")
    if problems:
        raise ControlsError("; ".join(problems))
    approvers = payload["manual_entry_approvers"]
    return Controls(tuple(approvers) if isinstance(approvers, list) else approvers,
                    payload["approval_limit"])


def load_controls(ledger_path: str | Path) -> Controls:
    """The controls beside ``ledger_path``, or :data:`DEFAULT_CONTROLS` when there is no file."""
    path = controls_path(ledger_path)
    try:
        text = path.read_text(encoding="utf-8-sig")  # a byte-order mark is not a difference
    except FileNotFoundError:
        if path.is_symlink():  # a link to nothing is still a file somebody meant to be read
            raise ControlsError(f"{path} is a link to a file that does not exist") from None
        return DEFAULT_CONTROLS
    except (OSError, UnicodeDecodeError) as exc:  # a directory, no permission, not UTF-8
        raise ControlsError(f"{path} cannot be read ({exc})") from None
    try:
        controls = parse_controls(text)
    except ControlsError as exc:
        raise ControlsError(f"{path}: {exc}") from None
    return dataclasses.replace(controls, source=str(path))
