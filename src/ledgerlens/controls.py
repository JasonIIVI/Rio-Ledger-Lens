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
import os
import stat
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .ingest import quote

CONTROLS_SUFFIX = ".controls.json"
VERSION = 1
KEYS = ("version", "manual_entry_approvers", "approval_limit")
#: A user name, not a sentence: an approver is printed inside JET-12's reason, and the
#: reason reaches the narration prompt. For the same reason the list is short.
MAX_APPROVER_LENGTH = 64
MAX_APPROVERS = 50
#: No approval limit is this large, and below it a limit in whole cents has at most 14
#: significant digits, which a float holds exactly: the limit JET-06 compares with, and
#: the one every surface prints, is the one the file states.
LIMIT_CEILING = Decimal(10) ** 12
#: A controls file is a few lines; a file this size is something else.
MAX_FILE_BYTES = 64 * 1024
#: A message lists this many of a kind of problem, then counts the rest.
_LISTED = 5


class ControlsError(ValueError):
    """A controls file, or controls built in code, that cannot be used."""


class _Unusable:
    """A JSON value no control can hold, kept so the key holding it can be named.

    NaN and Infinity (which Python's json reads and JSON does not have), and a number
    with more digits than a control needs (which a float would round, or Python 3.12
    refuse to read at all, before anything here could say which key held it).
    """

    def __init__(self, text: str, why: str) -> None:
        self.text = text if len(text) <= 24 else f"{text[:20]}... ({len(text)} characters)"
        self.why = why


def _number(text: str, kind) -> object:
    if len(text) > 40:
        return _Unusable(text, "has more digits than a control needs")
    try:
        return kind(text)
    except (ArithmeticError, ValueError):  # 1e99999999999999999999: no Decimal holds it
        return _Unusable(text, "has more digits than a control needs")


def _shown(value: object) -> str:
    """A value from the file as a message quotes it: a JSON number as it was written, and
    nothing longer than a line's worth."""
    if isinstance(value, _Unusable):
        return value.text
    if isinstance(value, str):
        return quote(value)  # cut to a line's worth already
    text = str(value) if isinstance(value, Decimal) else ascii(value)
    return text if len(text) <= 60 else f"{text[:48]}... ({len(text)} characters)"


def _listed(items: list[str]) -> str:
    more = len(items) - _LISTED
    return ", ".join(items[:_LISTED]) + (f" and {more} more" if more > 0 else "")


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
    if len(approvers) > MAX_APPROVERS:
        # every name is printed in each JET-12 reason, and so in every prompt that quotes one
        return [f"manual_entry_approvers lists {len(approvers):,} names; at most "
                f"{MAX_APPROVERS} can be named in JET-12's reason"]
    problems = []
    for name in approvers:
        if not isinstance(name, str):
            problems.append(f"approver {_shown(name)} is not a string")
        elif not name:
            problems.append("an approver is an empty string")
        elif "," in name:
            problems.append(f"approver {quote(name)} holds a comma, which separates the names "
                            "in JET-12's reason")
        elif len(name) > MAX_APPROVER_LENGTH:
            problems.append(f"approver {quote(name)} is longer than {MAX_APPROVER_LENGTH} "
                            "characters")
        elif not _visible(name):
            # "unassigned": a character newer than this Python's Unicode tables is refused too
            problems.append(f"approver {quote(name)} is not one line of visible text (a control, "
                            "invisible or unassigned character, or a space at either end or "
                            "doubled)")
    names = Counter(name for name in approvers if isinstance(name, str))
    problems += [f"approver {quote(name)} is listed {count} times"
                 for name, count in names.items() if count > 1]
    if len(problems) > _LISTED:
        problems = problems[:_LISTED] + [f"{len(problems) - _LISTED} more problems with approvers"]
    return problems


def _limit(value: object) -> tuple[float | None, str | None]:
    """``(the limit in dollars, None)``, or ``(None, why it cannot be used)``."""
    if isinstance(value, _Unusable):
        return None, f"approval_limit {value.text} {value.why}"
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        hint = ' (write 10000, not "10,000")' if isinstance(value, str) else ""
        return None, f"approval_limit must be a number of dollars, got {_shown(value)}{hint}"
    # Judged as written: a file's number is read as a Decimal, never rounded through a float
    # first. float() before repr: a float subclass (numpy's) has a repr of its own.
    amount = Decimal(repr(float(value))) if isinstance(value, float) else Decimal(value)
    if not amount.is_finite() or amount <= 0:
        return None, f"approval_limit must be a finite number above 0, got {_shown(amount)}"
    if amount >= LIMIT_CEILING:
        return None, (f"approval_limit {_shown(amount)} is not below 1,000,000,000,000; no "
                      "approval limit is that large")
    cents = amount * 100  # exact: below the ceiling, a whole number of cents has 14 digits
    if cents != cents.to_integral_value():
        return None, f"approval_limit {_shown(amount)} is not a whole number of cents"
    return float(amount), None


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
    """Where a ledger's controls sit: ``data/ledger.controls.json`` beside ``data/ledger.csv``.

    A leading ``~`` is expanded as pandas expands it when it reads the ledger, so the file
    looked for is the one beside the ledger read.
    """
    return Path(os.path.expanduser(os.fspath(ledger_path))).with_suffix(CONTROLS_SUFFIX)


def parse_controls(text: str) -> Controls:
    """Controls from the text of a controls file; :class:`ControlsError` says why not.

    Every problem the file has is named in one message, so it is fixed in one round.
    """
    twice: list[str] = []

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
        # json.loads would keep the last one silently: which of two limits was meant?
        twice.extend(key for key, count in Counter(key for key, _ in pairs).items() if count > 1)
        return dict(pairs)

    try:
        payload = json.loads(
            text, object_pairs_hook=no_duplicates,
            parse_constant=lambda name: _Unusable(name, "is not a number (JSON has no NaN or Infinity)"),
            parse_float=lambda digits: _number(digits, Decimal),
            parse_int=lambda digits: _number(digits, int))
    except json.JSONDecodeError as exc:
        raise ControlsError(f"not JSON ({exc})") from None
    except (ValueError, RecursionError) as exc:  # nested past what the decoder can follow
        raise ControlsError(f"not JSON a controls file can hold ({type(exc).__name__})") from None
    if not isinstance(payload, dict):
        raise ControlsError(f"must hold a JSON object with the keys {', '.join(KEYS)}")
    problems = []
    if twice:
        problems.append("key(s) given twice: " + _listed([quote(key) for key in sorted(set(twice))]))
    unknown = sorted(set(payload) - set(KEYS))
    if unknown:
        problems.append("unknown key(s): " + _listed([quote(key) for key in unknown]))
    missing = [key for key in KEYS if key not in payload]
    if missing:
        problems.append("missing key(s): " + ", ".join(missing))
    version = payload.get("version")
    if "version" in payload and not (type(version) is int and version == VERSION):
        problems.append(f"version must be {VERSION}, got {_shown(version)}")
    approvers = payload.get("manual_entry_approvers")
    if isinstance(approvers, list):
        approvers = tuple(approvers)
    if problems:  # the values are judged too, so one message names everything to fix
        if "manual_entry_approvers" in payload:
            problems += _approver_problems(approvers)
        if "approval_limit" in payload:
            _, problem = _limit(payload["approval_limit"])
            problems += [problem] if problem else []
        raise ControlsError("; ".join(problems))
    return Controls(approvers, payload["approval_limit"])


def load_controls(ledger_path: str | Path) -> Controls:
    """The controls beside ``ledger_path``, or :data:`DEFAULT_CONTROLS` when there is no file."""
    path = controls_path(ledger_path)
    try:
        status = os.stat(path)
    except FileNotFoundError:
        if path.is_symlink():  # a link to nothing is still a file somebody meant to be read
            raise ControlsError(f"{path}: a link to a file that does not exist") from None
        return DEFAULT_CONTROLS
    except (OSError, ValueError) as exc:  # a parent nobody may search, a name too long, a loop
        raise ControlsError(f"{path}: cannot be read ({exc})") from None
    if not stat.S_ISREG(status.st_mode):  # a directory; a FIFO, which a read would wait on forever
        raise ControlsError(f"{path}: not a regular file")
    if status.st_size > MAX_FILE_BYTES:
        raise ControlsError(f"{path}: {status.st_size:,} bytes; a controls file is a few lines")
    try:
        text = path.read_bytes().decode("utf-8-sig")  # a byte-order mark is not a difference
    except (OSError, UnicodeDecodeError) as exc:  # no permission, not UTF-8
        raise ControlsError(f"{path}: cannot be read ({exc})") from None
    try:
        controls = parse_controls(text)
    except ControlsError as exc:
        raise ControlsError(f"{path}: {exc}") from None
    return dataclasses.replace(controls, source=str(path))
