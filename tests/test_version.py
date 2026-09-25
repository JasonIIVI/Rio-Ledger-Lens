"""The version is stated twice; a tag must never find the two disagreeing."""

import re
from pathlib import Path

import ledgerlens


def test_the_package_version_matches_pyproject():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    declared = re.search(r'^version = "(.+)"$', pyproject, re.M).group(1)  # 3.9 has no tomllib
    assert ledgerlens.__version__ == declared
