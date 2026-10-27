"""Candidate resource, market and strategic scores (R, D, P, S, H)."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .yamlio import load_yaml_mapping

RESOURCE_SPECS = (
    ("RQ", "WPD100_median_aw", "RQ_percentile", "RQ_score"),
    ("RF", "F500", "RF_percentile", "RF_score"),
)
MARKET_SPECS = (
    ("D", "operating_mw_component", "D_peer_percentile", "D_score_component"),
    ("P", "pipeline_mw_component", "P_peer_percentile", "P_score_component"),
)


def _load_config(path):
    data = load_yaml_mapping(path)
    edges = [float(value) for value in data["percentile_edges"]]
    if len(edges) != 4 or edges != sorted(edges):
        raise ValueError("scoring.yaml percentile_edges must contain four increasing thresholds")
    tolerance = float(data["market_qc_tolerance_mw"])
    if tolerance < 0:
        raise ValueError("market_qc_tolerance_mw must be non-negative")
    thresholds = [float(value) for value in data["resource_metrics"]["area_thresholds_w_m2"]]
    if thresholds != [300.0, 500.0]:
        raise ValueError("GWEH resource metrics require 300 and 500 W m-2 area thresholds")
    return data


def validate_scoring_config(path: Path) -> None:
    _load_config(path)


def percentile_rank(values: pd.Series) -> pd.Series:
    """Midrank percentiles among all finite candidate values, including zero."""
    numeric = pd.to_numeric(values, errors="coerce")
    out = pd.Series(np.nan, index=numeric.index, dtype=float)
    finite = numeric.map(np.isfinite)
    if finite.any():
        out.loc[finite] = numeric.loc[finite].rank(method="average", pct=True) * 100.0
    return out


def positive_only_percentile_rank(values: pd.Series) -> pd.Series:
    """Market percentiles: zero stays zero; positive values form the peer set."""
    numeric = pd.to_numeric(values, errors="coerce")
    out = pd.Series(np.nan, index=numeric.index, dtype=float)
    finite = numeric.map(np.isfinite)
    out.loc[finite & numeric.le(0.0)] = 0.0
    positive = finite & numeric.gt(0.0)
    if positive.any():
        out.loc[positive] = numeric.loc[positive].rank(method="average", pct=True) * 100.0
    return out


def percentile_score(percentile: float, raw_value: float, edges: list[float]) -> float:
    try:
        raw, pct = float(raw_value), float(percentile)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(raw):
        return float("nan")
    if raw <= 0.0:
        return 0.0
    if not math.isfinite(pct):
        return float("nan")
    return float(sum(pct >= edge for edge in edges))


def _combine_resource(a, b):
    """Half-up rounding of the two resource component scores."""
    if not np.isfinite(a) or not np.isfinite(b):
        return float("nan")
    return float(math.floor((float(a) + float(b)) / 2.0 + 0.5))


def _load_strategic_scores(source, candidate_ids, score_column):
    if isinstance(source, pd.DataFrame):
        table = source.copy()
    else:
        table = pd.read_csv(source, dtype={"candidate_id": str})

    missing = {"candidate_id", score_column} - set(table.columns)
    if missing:
        raise ValueError(f"Strategic score table is missing columns: {sorted(missing)}")
    table["candidate_id"] = table["candidate_id"].astype(str)
    if table["candidate_id"].duplicated().any():
        raise ValueError("Strategic score table contains duplicate candidate_id values")
    if set(table["candidate_id"]) != candidate_ids:
        raise RuntimeError("Strategic score universe differs from Stage-06 metrics")

    values = pd.to_numeric(table[score_column], errors="coerce")
    invalid = values.isna() | values.lt(0) | values.gt(4) | values.mod(1).ne(0)
    if invalid.any():
        raise RuntimeError("Strategic scores must be complete integer values 0..4")
    return pd.DataFrame({"candidate_id": table["candidate_id"], "S_score": values.astype(int)})


def _resource_scores(metrics, edges):
    scored = metrics[["candidate_id", "WPD100_median_aw", "F500"]].copy()
    for _, metric, percentile_column, score_column in RESOURCE_SPECS:
        raw = pd.to_numeric(scored[metric], errors="coerce")
        pct = percentile_rank(raw)
        scored[percentile_column] = pct
        scored[score_column] = [percentile_score(p, value, edges) for p, value in zip(pct, raw)]
    return scored.drop(columns=["WPD100_median_aw", "F500"])


def _market_scores(components, edges):
    table = components.copy()
    required = {
        "candidate_id", "market_component", "operating_mw_component",
        "pipeline_mw_component", "n_active_phases_component",
    }
    missing = required - set(table.columns)
    if missing:
        raise ValueError(f"Stage-06 market_components is missing columns: {sorted(missing)}")
    table["candidate_id"] = table["candidate_id"].astype(str)
    table["market_component"] = table["market_component"].astype(str).str.lower()
    if not set(table["market_component"]).issubset({"onshore", "offshore"}):
        raise ValueError("market_component must be onshore or offshore")

    # Onshore and offshore component populations have separate market ranks.
    for component in ("onshore", "offshore"):
        mask = table["market_component"].eq(component)
        if not mask.any():
            continue
        for _, metric, pct_column, score_column in MARKET_SPECS:
            raw = pd.to_numeric(table.loc[mask, metric], errors="coerce")
            pct = positive_only_percentile_rank(raw)
            table.loc[mask, pct_column] = pct
            table.loc[mask, score_column] = [
                percentile_score(p, value, edges) for p, value in zip(pct, raw)
            ]

    return table.groupby("candidate_id", sort=False).agg(
        D_score=("D_score_component", "max"),
        P_score=("P_score_component", "max"),
        D_peer_percentile=("D_peer_percentile", "max"),
        P_peer_percentile=("P_peer_percentile", "max"),
    ).reset_index()


def run_scores(
    *,
    metrics_dir: Path,
    scoring_config_path: Path,
    strategic_scores_source,
    strategic_score_column: str,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = _load_config(scoring_config_path)
    edges = [float(value) for value in config["percentile_edges"]]

    metrics = pd.read_csv(metrics_dir / "region_metrics.csv")
    components = pd.read_csv(metrics_dir / "market_components.csv")
    metrics["candidate_id"] = metrics["candidate_id"].astype(str)
    strategic = _load_strategic_scores(
        strategic_scores_source, set(metrics["candidate_id"]), strategic_score_column
    )
    resource = _resource_scores(metrics, edges)
    market = _market_scores(components, edges)

    scores = metrics[["candidate_id", "region_name", "surface_type"]].merge(
        resource, on="candidate_id", validate="one_to_one"
    ).merge(market, on="candidate_id", validate="one_to_one")
    scores["R_score"] = [
        _combine_resource(a, b) for a, b in zip(scores["RQ_score"], scores["RF_score"])
    ]
    scores = scores.merge(strategic, on="candidate_id", validate="one_to_one")
    scores["H_without_S"] = scores[["R_score", "D_score", "P_score"]].sum(axis=1, min_count=3)
    scores["H_score"] = scores[["R_score", "D_score", "P_score", "S_score"]].sum(axis=1, min_count=4)
    scores["score_complete"] = scores[["R_score", "D_score", "P_score", "S_score"]].notna().all(axis=1)
    if not scores["score_complete"].all():
        raise RuntimeError("Stage-07 scores are incomplete")

    columns = [
        "candidate_id", "region_name", "surface_type", "RQ_percentile", "RQ_score",
        "RF_percentile", "RF_score", "R_score", "D_peer_percentile", "D_score",
        "P_peer_percentile", "P_score", "S_score", "H_without_S", "H_score",
        "score_complete",
    ]
    scores[columns].to_csv(output_dir / "scores.csv", index=False)
    return {
        "candidate_count": len(scores),
        "score_complete_count": int(scores.score_complete.sum()),
    }
