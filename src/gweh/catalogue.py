from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .maturity import evaluate_maturity
from .selection import (
    EVIDENCE_NOT_SELECTED,
    EVIDENCE_SELECTED,
    evaluate_primary,
    evaluate_selection,
    validate_selection_config,
)


def _load_csv(path):
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, dtype={"candidate_id": str}).fillna("")


def _validate_candidate_universe(*tables):
    ids = [table["candidate_id"].astype(str).tolist() for table in tables]
    reference = ids[0]
    reference_set = set(reference)
    if len(reference) != len(reference_set):
        raise ValueError("Duplicate candidate_id in candidate registry")
    for table_ids in ids[1:]:
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("Duplicate candidate_id in Stage-08 input")
        if set(table_ids) != reference_set:
            raise ValueError("Stage-08 candidate universes do not match")
    return reference


def run_catalogue(
    *,
    metrics_dir: Path,
    scores_dir: Path,
    candidate_registry_path: Path,
    selection_config_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = validate_selection_config(selection_config_path)
    candidates = _load_csv(candidate_registry_path)
    metrics = _load_csv(metrics_dir / "region_metrics.csv")
    scores = _load_csv(scores_dir / "scores.csv")
    _validate_candidate_universe(candidates, metrics, scores)

    score_complete = scores["score_complete"].astype(str).str.lower().isin({"true", "1"})
    if not score_complete.all():
        bad = scores.loc[~score_complete, "candidate_id"].astype(str).tolist()
        raise RuntimeError(f"Stage-08 requires complete scores: {bad}")

    primary = evaluate_primary(scores, metrics, config["primary"])
    selection = evaluate_selection(scores, metrics, primary, config["absolute_market"])
    maturity = evaluate_maturity(scores, metrics, config["maturity"])[
        ["candidate_id", "market_maturity", "maturity_basis"]
    ].copy()

    catalogue = candidates.merge(
        scores[[
            "candidate_id", "R_score", "D_score", "P_score", "S_score", "H_without_S", "H_score",
        ]],
        on="candidate_id",
        how="left",
        validate="one_to_one",
    ).merge(
        selection[[
            "candidate_id",
            "primary_pass",
            "absolute_market_pass",
            "evidence_selection_status",
            "selection_pathway",
        ]],
        on="candidate_id",
        how="left",
        validate="one_to_one",
    ).merge(maturity, on="candidate_id", how="left", validate="one_to_one")
    catalogue = catalogue.sort_values("display_order", key=lambda s: pd.to_numeric(s, errors="coerce")).reset_index(drop=True)

    trace_metrics = metrics[[
        "candidate_id",
        "coverage_strict_fraction",
        "coverage_within_tolerance_fraction",
        "project_WPD100_capacity_weighted",
        "project_resource_n",
        "operating_mw_total",
        "pipeline_mw_total",
    ]].copy()
    trace = scores[["candidate_id", "R_score", "D_score", "P_score", "S_score", "H_score"]].merge(
        trace_metrics, on="candidate_id", how="left", validate="one_to_one"
    ).merge(
        selection.drop(columns=["R_score", "D_score", "P_score", "S_score", "H_score", "project_WPD100_capacity_weighted"]),
        on="candidate_id",
        how="left",
        validate="one_to_one",
    )
    catalogue.to_csv(output_dir / "catalogue.csv", index=False)
    trace.to_csv(output_dir / "selection_trace.csv", index=False)

    selected_count = int(catalogue["evidence_selection_status"].eq(EVIDENCE_SELECTED).sum())
    not_selected_count = int(catalogue["evidence_selection_status"].eq(EVIDENCE_NOT_SELECTED).sum())
    qc = {
        "candidate_count": int(len(catalogue)),
        "evidence_selected_count": selected_count,
        "evidence_not_selected_count": not_selected_count,
        "selection_pathways": {str(k): int(v) for k, v in catalogue["selection_pathway"].value_counts().sort_index().items()},
        "market_maturity": {str(k): int(v) for k, v in catalogue["market_maturity"].value_counts().sort_index().items()},
        "primary_selected_count": int(catalogue["primary_pass"].astype(bool).sum()),
        "absolute_market_selected_count": int(catalogue["absolute_market_pass"].astype(bool).sum()),
    }
    return qc
