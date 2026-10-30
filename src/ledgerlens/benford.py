"""Benford's Law digit analysis.

Benford's Law says that in naturally occurring financial populations the
leading digit 1 appears about 30.1% of the time and 9 only about 4.6%.
Fabricated numbers rarely follow that curve, which makes the test a cheap
first screen over a whole population.

The caveat that matters, and that the report repeats: non-conformity is not
evidence of fraud. Populations with price points, thresholds, or a narrow
range of values fail Benford for entirely innocent reasons. It tells you
where to look, not what you will find.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Expected first-digit frequencies under Benford's Law.
EXPECTED_FIRST_DIGIT: dict[int, float] = {
    d: float(np.log10(1 + 1 / d)) for d in range(1, 10)
}

#: Nigrini's mean-absolute-deviation conformity bands for the first-digit test.
MAD_BANDS = (
    (0.006, "close conformity"),
    (0.012, "acceptable conformity"),
    (0.015, "marginal conformity"),
    (float("inf"), "nonconformity"),
)

#: Below this many observations the test is not meaningful, no matter what the
#: statistic says. Nigrini suggests at least 300-500; under 300 no band is given.
MIN_SAMPLE = 300

#: One wording, printed ahead of the figures wherever a test falls under the floor. The
#: bands were set for large populations, and a small conformant one reads high by chance.
SMALL_SAMPLE_NOTE = (
    f"Below {MIN_SAMPLE} amounts the first-digit test is not meaningful (Nigrini suggests at "
    "least 300 to 500), so no conformity band is given; MAD and chi-square are shown as computed."
)


def first_digit(values: pd.Series) -> pd.Series:
    """Leading significant digit of each value, ignoring sign and zeros."""
    v = pd.to_numeric(values, errors="coerce").abs()
    v = v[(v.notna()) & (v > 0)]
    if v.empty:
        return pd.Series(dtype="int64")
    # Scale each value into [1, 10) and take the integer part.
    exponent = np.floor(np.log10(v))
    leading = (v / np.power(10.0, exponent)).astype(int)
    return leading.clip(1, 9)


def mad_conformity(mad: float) -> str:
    """Translate a MAD statistic into Nigrini's plain-English band."""
    for threshold, label in MAD_BANDS:
        if mad <= threshold:
            return label
    return "nonconformity"  # pragma: no cover - unreachable, inf catches all


def benford_test(values: pd.Series, label: str = "population") -> dict:
    """Run the first-digit test over `values`.

    Returns the observed vs expected distribution plus a chi-square statistic
    and the MAD conformity band. Chi-square is reported because it is what
    textbooks use; MAD is reported because chi-square rejects conformity on
    almost any large population, which makes it useless at audit scale.

    Under ``MIN_SAMPLE`` amounts (zeros have no leading digit and are not
    counted) the band is "insufficient data". A small population that follows
    the law still reads high by chance, so a band there would be a reading the
    test cannot make; the statistics are returned all the same.
    """
    digits = first_digit(values)
    n = int(digits.size)

    counts = digits.value_counts().reindex(range(1, 10), fill_value=0).sort_index()
    observed_prop = (counts / n) if n else counts.astype(float)
    expected_prop = pd.Series(EXPECTED_FIRST_DIGIT).sort_index()
    expected_counts = expected_prop * n

    if n:
        chi_square = float((((counts - expected_counts) ** 2) / expected_counts).sum())
        mad = float((observed_prop - expected_prop).abs().mean())
    else:
        chi_square, mad = 0.0, 0.0

    return {
        "label": label,
        "n": n,
        "sufficient_sample": n >= MIN_SAMPLE,
        "chi_square": round(chi_square, 4),
        # 8 degrees of freedom, alpha = 0.05
        "chi_square_critical_5pct": 15.507,
        "exceeds_critical": chi_square > 15.507,
        "mad": round(mad, 6),
        "conformity": mad_conformity(mad) if n >= MIN_SAMPLE else "insufficient data",
        "observed": {int(d): int(c) for d, c in counts.items()},
        "observed_prop": {int(d): round(float(p), 4) for d, p in observed_prop.items()},
        "expected_prop": {int(d): round(float(p), 4) for d, p in expected_prop.items()},
    }


def segmented_benford(
    df: pd.DataFrame,
    value_col: str = "abs_amount",
    by: str | None = "account_code",
    min_n: int = MIN_SAMPLE,
) -> pd.DataFrame:
    """Run the first-digit test per segment (per account, per user, per source).

    Population-level conformity hides a lot: one fabricated account inside a
    conformant ledger will not move the overall curve. Segmenting is where
    Benford earns its keep.
    """
    rows: list[dict] = []
    if by is None:
        rows.append(benford_test(df[value_col], label="ALL"))
    else:
        for key, chunk in df.groupby(by, observed=True):
            if len(chunk) < min_n:
                continue
            rows.append(benford_test(chunk[value_col], label=str(key)))

    if not rows:
        return pd.DataFrame(
            columns=["label", "n", "sufficient_sample", "chi_square", "mad", "conformity"]
        )

    out = pd.DataFrame(
        [{k: r[k] for k in ("label", "n", "sufficient_sample", "chi_square",
                            "exceeds_critical", "mad", "conformity")} for r in rows]
    )
    # Segments tied on MAD go by label. A one-key sort keeps tied rows in input order up to 16
    # rows and scrambles them from 17, and differently on a Mac and on CI's Linux runners.
    return out.sort_values(["mad", "label"], ascending=[False, True]).reset_index(drop=True)
