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


def test_the_package_ships_its_type_marker_and_mypy_checks_for_the_python_it_runs_on():
    """``py.typed`` tells a type checker that the installed package carries its own
    annotations: it has to be in the source tree and named as package data, or a wheel
    leaves it out. And ``[tool.mypy]`` names no ``python_version``: mypy 2 refuses to check
    for Python 3.9 ("must be 3.10 or higher"), so with one named, either the 3.9 leg or the
    other two could not run it. Each leg checks for its own Python: 3.9 under mypy 1.19,
    the last release that runs there, which is why that is the floor in the dev extra."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert (ROOT / "src" / "ledgerlens" / "py.typed").is_file()
    assert re.search(r'(?m)^\[tool\.setuptools\.package-data\]\nledgerlens = \["py\.typed"\]$', pyproject)
    block = re.search(r"(?ms)^\[tool\.mypy\]\n(.*?)(?=^\[)", pyproject).group(1)
    # the package, and the calls a typed caller must be able to write (tests/typed_callers.py)
    assert 'files = ["src/ledgerlens", "tests/typed_callers.py"]' in block
    assert (ROOT / "tests" / "typed_callers.py").is_file() and "python_version" not in block
    for setting in ("check_untyped_defs", "disallow_untyped_defs", "disallow_incomplete_defs"):
        assert re.search(rf"(?m)^{setting} = true$", block), setting
    dev = re.search(r"(?m)^dev = \[(.*)\]$", pyproject).group(1)
    assert '"mypy>=1.19"' in dev


def test_the_streamlit_floor_takes_the_width_the_dashboard_gives():
    """app.py gives st.dataframe width="stretch", which Streamlit's st.dataframe takes from 1.49.0
    (in 1.48.0 its width is an int or None)."""
    assert 'width="stretch"' in (ROOT / "app.py").read_text(encoding="utf-8")
    assert _floor("streamlit") >= (1, 49)
