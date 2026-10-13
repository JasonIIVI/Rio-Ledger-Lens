"""The controls file: the approver list JET-12 reads and the limit JET-06 reads.

Kept ASCII like summary's tests: the characters an approver may not hold are
built with chr(), so the file can be reviewed.
"""

from __future__ import annotations

import json
import os

import pytest

from ledgerlens import controls, generate, summary
from ledgerlens.controls import (
    DEFAULT_CONTROLS,
    Controls,
    ControlsError,
    controls_path,
    format_limit,
    load_controls,
    parse_controls,
)
from ledgerlens.ingest import identity_path


def _write(tmp_path, text: str, name: str = "ledger.csv"):
    ledger = tmp_path / name
    controls_path(ledger).write_text(text, encoding="utf-8")
    return ledger


def _file(approvers=("controller", "mchen"), limit=10000, **extra) -> str:
    return json.dumps({"version": 1, "manual_entry_approvers": list(approvers),
                       "approval_limit": limit, **extra})


def test_the_defaults_are_the_generators_controls():
    # The generator built the default ledger against these; detection reads them from
    # here (jets no longer imports the generator), so the two must not drift apart.
    assert DEFAULT_CONTROLS.manual_entry_approvers == tuple(sorted(generate.MANUAL_JE_APPROVERS))
    assert DEFAULT_CONTROLS.approval_limit == generate.APPROVAL_THRESHOLD
    assert DEFAULT_CONTROLS.source is None and DEFAULT_CONTROLS.is_default


def test_the_file_sits_beside_the_ledger_as_the_identity_sidecar_does(tmp_path):
    ledger = tmp_path / "data" / "qbo-ledger.csv"
    assert controls_path(ledger) == tmp_path / "data" / "qbo-ledger.controls.json"
    assert controls_path(ledger).parent == identity_path(ledger).parent
    assert controls_path("ledger") == controls_path("ledger.csv")


def test_no_file_means_the_defaults(tmp_path):
    assert load_controls(tmp_path / "ledger.csv") is DEFAULT_CONTROLS


def test_a_file_restating_the_defaults_is_the_defaults(tmp_path):
    # Another order, a float limit, a byte-order mark: the same controls, rendered the same way.
    ledger = tmp_path / "ledger.csv"
    controls_path(ledger).write_text(
        chr(0xFEFF) + _file(("mchen", "controller"), 10000.00), encoding="utf-8")
    loaded = load_controls(ledger)
    assert loaded == DEFAULT_CONTROLS and loaded.is_default
    assert loaded.manual_entry_approvers == ("controller", "mchen")
    assert loaded.describe() == DEFAULT_CONTROLS.describe()
    assert loaded.source == str(controls_path(ledger))  # where they came from is still said


def test_a_companys_own_controls_are_read_and_kept_sorted(tmp_path):
    ledger = _write(tmp_path, _file(("zoe", "Ann Lee", "qbo-user-1"), 2500.5))
    loaded = load_controls(ledger)
    assert loaded.manual_entry_approvers == ("Ann Lee", "qbo-user-1", "zoe")
    assert loaded.approval_limit == 2500.5 and not loaded.is_default
    assert loaded.as_dict() == {"source": str(controls_path(ledger)), "default": False,
                                "manual_entry_approvers": ["Ann Lee", "qbo-user-1", "zoe"],
                                "approval_limit": 2500.5}
    assert loaded.describe() == "approvers Ann Lee, qbo-user-1, zoe (JET-12); approval limit $2,500.50 (JET-06)"


@pytest.mark.parametrize(("limit", "shown"), [
    (10_000.0, "$10,000"), (10_000.5, "$10,000.50"), (9_999.99, "$9,999.99"),
    (1_234_567, "$1,234,567"), (0.01, "$0.01"),
])
def test_a_limit_is_shown_with_its_cents_only_when_it_has_some(limit, shown):
    assert format_limit(limit) == shown


# Each refusal: the file's text, and words the message must carry.
REFUSED = [
    ("not json", ["not JSON"]),
    ("", ["not JSON"]),
    ("[1, 2]", ["JSON object"]),
    ('"controls"', ["JSON object"]),
    ('{"version": 1, "version": 1, "manual_entry_approvers": ["a"], "approval_limit": 5}',
     ["given twice", "'version'"]),
    ('{"version": 1, "manual_entry_approvers": ["a"], "approval_limit": 5, "approval_limit": 9}',
     ["given twice", "'approval_limit'"]),
    ('{"version": 1, "manual_entry_approvers": ["a"], "approval_limit": NaN}', ["NaN is not a number (JSON has no NaN or Infinity)"]),
    ('{"version": 1, "manual_entry_approvers": ["a"], "approval_limit": Infinity}', ["Infinity is not a number (JSON has no NaN or Infinity)"]),
    ('{"version": 1, "manual_entry_approvers": ["a"], "approval_limit": -Infinity}', ["-Infinity is not a number (JSON has no NaN or Infinity)"]),
    (_file(aproval_limit=5), ["unknown key", "'aproval_limit'"]),
    ('{"version": 1, "approval_limit": 5}', ["missing key", "manual_entry_approvers"]),
    ('{"manual_entry_approvers": ["a"], "approval_limit": 5}', ["missing key", "version"]),
    ('{}', ["missing key", "version, manual_entry_approvers, approval_limit"]),
    (_file().replace('"version": 1', '"version": 2'), ["version must be 1, got 2"]),
    (_file().replace('"version": 1', '"version": 1.0'), ["version must be 1, got 1.0"]),
    (_file().replace('"version": 1', '"version": true'), ["version must be 1, got True"]),
    (_file().replace('"version": 1', '"version": "1"'), ["version must be 1, got '1'"]),
    (_file().replace('"version": 1', '"version": 0'), ["version must be 1, got 0"]),
    ('{"version": 1, "manual_entry_approvers": "controller", "approval_limit": 5}',
     ["must be a list of user names", "'controller'"]),
    (_file(()), ["is empty"]),
    (_file(("controller", 7)), ["approver 7 is not a string"]),
    (_file(("controller", None)), ["approver None is not a string"]),
    (_file(("controller", "")), ["an approver is an empty string"]),
    (_file(("controller", " mchen")), ["' mchen'", "not one line of visible text"]),
    (_file(("controller", "mchen ")), ["'mchen '", "not one line of visible text"]),
    (_file(("controller", "Ann  Lee")), ["'Ann  Lee'", "not one line of visible text"]),
    (_file(("controller", "ann\nlee")), ["'ann\\nlee'", "not one line of visible text"]),
    (_file(("controller", "ann\tlee")), ["'ann\\tlee'", "not one line of visible text"]),
    (_file(("controller", "Lee, Ann")), ["'Lee, Ann'", "holds a comma"]),
    (_file(("controller", "x" * 65)), ["longer than 64 characters"]),
    (_file(("mchen", "controller", "mchen")), ["'mchen' is listed twice"]),
    (_file(limit="10,000"), ["must be a number of dollars", "'10,000'", 'not "10,000"']),
    (_file(limit=True), ["must be a number of dollars, got True"]),
    (_file(limit=None), ["must be a number of dollars, got None"]),
    (_file(limit=[10000]), ["must be a number of dollars"]),
    (_file(limit=0), ["above 0, got 0"]),
    (_file(limit=-5), ["above 0, got -5"]),
    (_file().replace("10000", "-0.0"), ["above 0, got -0.0"]),
    (_file().replace("10000", "9999.999"), ["9999.999 is not a whole number of cents"]),
    (_file().replace("10000", "0.001"), ["0.001 is not a whole number of cents"]),
    (_file().replace("10000", "1e999"), ["1E+999 has more digits than can be compared exactly"]),
    (_file().replace("10000", "12345678901234567.89"),
     ["12345678901234567.89 has more digits than can be compared exactly"]),
]


@pytest.mark.parametrize(("text", "said"), REFUSED, ids=[str(i) for i in range(len(REFUSED))])
def test_a_file_that_cannot_be_used_is_refused_never_replaced_by_the_defaults(tmp_path, text, said):
    ledger = _write(tmp_path, text)
    with pytest.raises(ControlsError) as caught:
        load_controls(ledger)
    message = str(caught.value)
    assert message.startswith(f"{controls_path(ledger)}: "), message  # the file is named first
    for words in said:
        assert words in message, (words, message)
    assert "\n" not in message  # one line, whatever the file held
    assert isinstance(caught.value, ValueError)  # every surface already turns these into a message


def test_every_problem_in_a_file_is_named_at_once(tmp_path):
    ledger = _write(tmp_path, _file(("a,b", "a,b"), 0))
    message = str(pytest.raises(ControlsError, load_controls, ledger).value)
    assert message.count("holds a comma") == 2 and "above 0" in message
    # a key problem does not hide the values' problems behind it
    ledger = _write(tmp_path, _file(("a,b",), "10,000", version=2, limit_usd=5))
    message = str(pytest.raises(ControlsError, load_controls, ledger).value)
    for said in ("unknown key(s): 'limit_usd'", "version must be 1, got 2", "holds a comma",
                 "must be a number of dollars"):
        assert said in message, (said, message)
    ledger = _write(tmp_path, '{"version": 1, "approval_limit": -1}')
    message = str(pytest.raises(ControlsError, load_controls, ledger).value)
    assert "missing key(s): manual_entry_approvers" in message and "above 0" in message


@pytest.mark.parametrize("code", sorted({a for a, _ in summary._UNSEEN_RANGES}
                                        | {b for _, b in summary._UNSEEN_RANGES} | {0x00A0, 0x3000}))
def test_an_approver_holding_a_character_no_reader_sees_is_refused(code):
    # Built in code: some of these cannot be written to a UTF-8 file at all (surrogates).
    with pytest.raises(ControlsError, match="not one line of visible text"):
        Controls(("controller", "m" + chr(code) + "chen"), 10_000)


def test_visibility_is_one_lines_test_for_every_ascii_character():
    # The ASCII shortcut in _visible has to agree with summary.one_line, which it skips.
    for code in range(128):
        for name in (chr(code), "a" + chr(code), "a" + chr(code) + "b", chr(code) + "b"):
            expected = summary.one_line(name) == name and name.isprintable()
            assert controls._visible(name) is expected, ascii(name)


def test_an_approver_written_in_another_script_is_kept_as_written(tmp_path):
    name = "Jos" + chr(0xE9) + " Garc" + chr(0xED) + "a"
    ledger = _write(tmp_path, _file(("controller", name)))
    assert load_controls(ledger).manual_entry_approvers == ("Jos" + chr(0xE9) + " Garc" + chr(0xED) + "a", "controller")


def test_controls_built_in_code_are_checked_the_same_way():
    with pytest.raises(ControlsError, match="above 0"):
        Controls(("controller",), -1.0)
    with pytest.raises(ControlsError, match="above 0"):
        Controls(("controller",), float("nan"))
    with pytest.raises(ControlsError, match="whole number of cents"):
        Controls(("controller",), 0.1 + 0.2)
    assert Controls(["mchen", "controller"], 10_000) == DEFAULT_CONTROLS  # a list, an int: the same


def test_parse_controls_names_no_file():
    with pytest.raises(ControlsError) as caught:
        parse_controls("[]")
    assert str(caught.value).startswith("must hold")


def test_a_controls_path_that_is_a_directory_is_refused(tmp_path):
    ledger = tmp_path / "ledger.csv"
    controls_path(ledger).mkdir()
    with pytest.raises(ControlsError, match="cannot be read"):
        load_controls(ledger)


def test_a_controls_file_that_is_not_utf8_is_refused(tmp_path):
    ledger = tmp_path / "ledger.csv"
    text = '{"version": 1, "manual_entry_approvers": ["m' + chr(0xE9) + '"], "approval_limit": 5}'
    controls_path(ledger).write_bytes(text.encode("latin-1"))  # the e-acute alone is not UTF-8
    with pytest.raises(ControlsError, match="cannot be read"):
        load_controls(ledger)


def test_a_link_to_nothing_is_refused_not_read_as_no_file(tmp_path):
    ledger = tmp_path / "ledger.csv"
    os.symlink(tmp_path / "gone.json", controls_path(ledger))
    with pytest.raises(ControlsError, match="does not exist"):
        load_controls(ledger)
