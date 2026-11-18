"""Milestone 3, modelling - the scorers compared: two baselines and two learned models.

Every scorer gives one score per (entry, candidate) pair; the candidates of an
entry are ranked by score and the top 5 are the prediction.

  rules     baseline: the automatic labelling rules as a score, an exact arXiv
            id or DOI first, then an identical title, then title similarity
            (4 * id_match + 4 * doi_match + 2 * title_exact + title_jacc3)
  title     baseline: title similarity alone (title_jacc3)
  logreg    pairwise classification: logistic regression on the pairs
            (median imputation, standardisation, balanced classes)
  lgbm      learning to rank: LightGBM lambdarank, one group per entry,
            optimising NDCG (close to MRR@5); LGBM_PARAMS

A learned model can be trained with the link features of a share of the
training entries masked (common.NO_LINK), so that it also learns to match
entries that name no identifier.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np

from common import NO_LINK, Pairs

BASELINES = ("rules", "title")


def baseline_scores(pairs: Pairs, rows: np.ndarray, name: str, mask_link=False) -> np.ndarray:
    f = ["id_match", "doi_match", "title_exact", "title_jacc3"]
    X = np.nan_to_num(pairs.matrix(rows, f, mask_link), nan=0.0)
    if name == "rules":
        return 4 * X[:, 0] + 4 * X[:, 1] + 2 * X[:, 2] + X[:, 3]
    if name == "title":
        return X[:, 3]
    raise ValueError(name)


def fit_logreg(X: np.ndarray, y: np.ndarray, seed: int):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                          LogisticRegression(class_weight="balanced", max_iter=1000, random_state=seed))
    model.fit(X, y)
    return model


LGBM_PARAMS = {"objective": "lambdarank", "n_estimators": 300, "learning_rate": 0.05, "num_leaves": 31,
               "min_child_samples": 50, "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8,
               "verbose": -1, "n_jobs": -1}


def fit_lgbm(X: np.ndarray, y: np.ndarray, groups: np.ndarray, seed: int, params: dict | None = None):
    """LightGBM lambdarank, one group per entry (groups = sizes of consecutive groups). A fixed number of trees:
    no early stopping, which would look at the data the model is judged on."""
    import lightgbm as lgb
    model = lgb.LGBMRanker(**{**LGBM_PARAMS, "random_state": seed, **(params or {})})
    model.fit(X, y, group=groups)
    return model


def score(model, kind: str, X: np.ndarray) -> np.ndarray:
    if kind == "logreg":
        return model.predict_proba(X)[:, 1]
    return model.predict(X, num_iteration=getattr(model, "best_iteration_", None) or None)


def save(model, kind: str, features: list[str], meta: dict, folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{name}.pkl", "wb") as f:
        pickle.dump(model, f)
    (folder / f"{name}.json").write_text(json.dumps({"kind": kind, "features": features, **meta}, indent=1),
                                         encoding="utf-8")


def load(folder: Path, name: str):
    meta = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
    with open(folder / f"{name}.pkl", "rb") as f:
        return pickle.load(f), meta


__all__ = ["BASELINES", "LGBM_PARAMS", "NO_LINK", "baseline_scores", "fit_logreg", "fit_lgbm", "score", "save", "load"]
