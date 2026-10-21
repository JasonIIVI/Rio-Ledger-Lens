"""The version is stated twice; a tag must never find the two disagreeing."""

import re
from pathlib import Path

import ledgerlens


def test_the_package_version_matches_pyproject():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    declared = re.search(r'^version = "(.+)"$', pyproject, re.M).group(1)  # 3.9 has no tomllib
    assert ledgerlens.__version__ == declared


def test_the_pandas_floor_is_one_the_code_can_import_with():
    """ingest imports pandas.tseries.api.guess_datetime_format, public from pandas 2.2.0 (and
    reads dates with to_datetime(format="mixed"), from 2.0.0). Below 2.2 every command fails at
    its start, so a lower floor promises an install that cannot run."""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    floor = re.search(r'^\s*"pandas>=([0-9.]+)"', pyproject, re.M).group(1)
    assert tuple(int(part) for part in floor.split(".")) >= (2, 2), floor
