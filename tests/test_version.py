"""What pyproject.toml promises: the version, stated twice, which a tag must never find
disagreeing, and floors the code can actually run on."""

import re
from pathlib import Path

import ledgerlens

ROOT = Path(__file__).resolve().parents[1]


def test_the_package_version_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text()
    declared = re.search(r'^version = "(.+)"$', pyproject, re.M).group(1)  # 3.9 has no tomllib
    assert ledgerlens.__version__ == declared


def _floor(package):
    match = re.search(rf'"{package}>=([0-9.]+)"', (ROOT / "pyproject.toml").read_text())
    assert match, f'pyproject.toml gives no floor written as "{package}>=X"'
    return tuple(int(part) for part in match.group(1).split("."))


def test_the_pandas_floor_is_one_the_code_can_import_with():
    """ingest imports pandas.tseries.api.guess_datetime_format, public from pandas 2.2.0 (and
    reads dates with to_datetime(format="mixed"), from 2.0.0). Below 2.2 every command fails at
    its start, so a lower floor promises an install that cannot run."""
    assert _floor("pandas") >= (2, 2)


def test_the_streamlit_floor_takes_the_width_the_dashboard_gives():
    """app.py gives st.dataframe width="stretch", which Streamlit's st.dataframe takes from 1.49.0
    (in 1.48.0 its width is an int or None)."""
    assert 'width="stretch"' in (ROOT / "app.py").read_text(encoding="utf-8")
    assert _floor("streamlit") >= (1, 49)
