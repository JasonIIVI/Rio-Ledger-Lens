"""Unsupervised anomaly scoring - the second tier.

The rule tier only finds what somebody thought to describe. This tier asks a
different question: which entries do not look like the rest of the population?
It is deliberately kept apart from the rule score rather than blended into it,
for two reasons.

First, they answer different questions, and averaging them would produce a
number that answers neither. Second, the rule score is explainable and this one
is not - an Isolation Forest can tell you an entry is unusual but not why. A
reviewer is entitled to know which kind of signal they are looking at.

The interesting output is not either score on its own. It is the disagreement:
an entry the model dislikes that no rule caught is the case worth opening.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from .features import build_features

#: Expected proportion of anomalies. Set slightly above the generator's 1.5%
#: so the model is not starved, but far below a level that would flag noise.
DEFAULT_CONTAMINATION = 0.02

#: Fixed so a given ledger always produces the same scores. A portfolio project
#: whose headline numbers move between runs is not worth much.
RANDOM_STATE = 20260922


@dataclass
class ModelReport:
    """What the fit actually did, so the run can be described honestly."""

    n_entries: int
    n_features_in: int
    n_features_used: int
    dropped_constant: list[str] = field(default_factory=list)
    contamination: float = DEFAULT_CONTAMINATION

    def describe(self) -> str:
        lines = [
            f"Isolation Forest fitted on {self.n_entries:,} entries",
            f"  features supplied  {self.n_features_in}",
            f"  features used      {self.n_features_used}",
            f"  contamination      {self.contamination:.3f}",
        ]
        if self.dropped_constant:
            lines.append("  dropped (constant) {}".format(", ".join(self.dropped_constant)))
        return "\n".join(lines)


class AnomalyModel:
    """Isolation Forest wrapper that produces a 0-1 anomaly score.

    Constant features are dropped at fit time. They carry no information, and
    silently keeping them would misrepresent how many signals the model is
    really using - the synthetic ledger currently has two (every entry has the
    same line count and zero posting lag), and both become meaningful once the
    generator produces multi-line entries.
    """

    def __init__(
        self,
        contamination: float = DEFAULT_CONTAMINATION,
        n_estimators: int = 300,
        random_state: int = RANDOM_STATE,
    ) -> None:
        self.contamination = contamination
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.scaler: StandardScaler | None = None
        self.forest: IsolationForest | None = None
        self.columns_: list[str] = []
        self.report_: ModelReport | None = None

    def fit(self, features: pd.DataFrame) -> AnomalyModel:
        variances = features.var(axis=0)
        keep = [c for c in features.columns if variances.get(c, 0.0) > 1e-12]
        dropped = [c for c in features.columns if c not in keep]

        self.columns_ = keep
        matrix = features[keep].to_numpy(dtype=float)

        self.scaler = StandardScaler().fit(matrix)
        self.forest = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=-1,
        ).fit(self.scaler.transform(matrix))

        self.report_ = ModelReport(
            n_entries=len(features),
            n_features_in=features.shape[1],
            n_features_used=len(keep),
            dropped_constant=dropped,
            contamination=self.contamination,
        )
        return self

    def score(self, features: pd.DataFrame) -> pd.Series:
        """Anomaly score in [0, 1]; higher means more unusual.

        sklearn's decision_function is positive for inliers and negative for
        outliers, which is the opposite of what a reviewer expects from
        something called a risk score, so it is inverted and rescaled here.
        """
        if self.forest is None or self.scaler is None:
            raise RuntimeError("model must be fitted before scoring")

        matrix = features[self.columns_].to_numpy(dtype=float)
        raw = -self.forest.decision_function(self.scaler.transform(matrix))

        lo, hi = float(raw.min()), float(raw.max())
        scaled = (raw - lo) / (hi - lo) if hi > lo else np.zeros_like(raw)
        return pd.Series(scaled, index=features.index, name="model_score")

    def fit_score(self, features: pd.DataFrame) -> pd.Series:
        return self.fit(features).score(features)


def score_ledger(df: pd.DataFrame, contamination: float = DEFAULT_CONTAMINATION):
    """Convenience path: prepared ledger in, (scores, report) out."""
    features = build_features(df)
    model = AnomalyModel(contamination=contamination)
    scores = model.fit_score(features)
    return scores, model.report_


def combine(
    rule_scored: pd.DataFrame,
    model_scores: pd.Series,
    model_top_pct: float = 0.02,
) -> pd.DataFrame:
    """Put both tiers side by side and label how they relate.

    The model flag is defined by *rank*, not by an absolute score cutoff. An
    Isolation Forest score has no natural scale - it depends on the population
    it was fitted to - so a hardcoded threshold like 0.6 means something
    different on every ledger. Taking the top ``model_top_pct`` is both
    defensible and directly comparable to how much review capacity exists.

    The ``agreement`` column is the point of this function:

    - ``both``        - rules and model agree it is unusual
    - ``rules only``  - a known pattern the model considers ordinary
    - ``model only``  - unusual in a way no rule describes; the interesting case
    - ``neither``     - unremarkable
    """
    out = rule_scored.merge(
        model_scores.rename("model_score"), left_on="entry_id", right_index=True, how="left"
    )
    out["model_score"] = out["model_score"].fillna(0.0)

    n_flag = max(1, int(round(len(out) * model_top_pct)))
    cutoff = out["model_score"].nlargest(n_flag).min()
    out["model_flag"] = out["model_score"] >= cutoff
    out["rule_flag"] = out["risk_score"] > 0

    conditions = [
        out["rule_flag"] & out["model_flag"],
        out["rule_flag"] & ~out["model_flag"],
        ~out["rule_flag"] & out["model_flag"],
    ]
    out["agreement"] = np.select(
        conditions, ["both", "rules only", "model only"], default="neither"
    )
    return out.sort_values(
        ["risk_score", "model_score"], ascending=False
    ).reset_index(drop=True)
