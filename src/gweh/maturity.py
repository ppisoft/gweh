from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


VALID_MATURITY = {"CORE", "EMERGING", "PROSPECTIVE"}


def evaluate_maturity(
    scores: pd.DataFrame,
    metrics: pd.DataFrame,
    maturity: dict[str, Any],
) -> pd.DataFrame:
    """Apply the frozen ABS_RECOMMENDED maturity classification."""
    out = scores[["candidate_id", "D_score", "P_score", "S_score"]].merge(
        metrics[["candidate_id", "surface_type", "operating_mw_total"]],
        on="candidate_id",
        how="left",
        validate="one_to_one",
    )
    for column in ["D_score", "P_score", "S_score", "operating_mw_total"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if not set(out["surface_type"]).issubset({"onshore", "offshore", "mixed"}):
        bad = sorted(set(out["surface_type"]) - {"onshore", "offshore", "mixed"})
        raise ValueError(f"Unsupported surface types for maturity: {bad}")

    out["core_operating_floor_mw"] = out["surface_type"].map(
        {key: float(value) for key, value in maturity["core_operating_floor_mw"].items()}
    )
    out["core_score_pass"] = out["D_score"].ge(float(maturity["CORE_min_D"]))
    out["core_absolute_capacity_pass"] = out["operating_mw_total"].ge(out["core_operating_floor_mw"])
    out["emerging_D_pass"] = out["D_score"].ge(float(maturity["EMERGING_min_D"]))
    out["emerging_pipeline_pass"] = out["D_score"].eq(0) & out["P_score"].ge(float(maturity["EMERGING_min_P_if_D0"]))
    out["prospective_P_pass"] = out["P_score"].ge(float(maturity["PROSPECTIVE_min_P"]))
    out["prospective_S_pass"] = out["S_score"].ge(float(maturity["PROSPECTIVE_min_S"]))

    core = out["core_score_pass"] & out["core_absolute_capacity_pass"]
    emerging = ~core & (out["emerging_D_pass"] | out["emerging_pipeline_pass"])
    out["market_maturity"] = np.select([core, emerging], ["CORE", "EMERGING"], default="PROSPECTIVE")
    out["maturity_basis"] = np.select(
        [
            core,
            emerging & out["emerging_D_pass"],
            emerging & out["emerging_pipeline_pass"],
            out["prospective_P_pass"] & out["prospective_S_pass"],
            out["prospective_P_pass"],
            out["prospective_S_pass"],
        ],
        [
            "OPERATING_AND_RELATIVE_DEPLOYMENT",
            "RELATIVE_DEPLOYMENT",
            "PROJECT_PIPELINE",
            "PIPELINE_AND_STRATEGY",
            "PIPELINE",
            "STRATEGY",
        ],
        default="LOW_MARKET_EVIDENCE",
    )
    if not set(out["market_maturity"]).issubset(VALID_MATURITY):
        raise RuntimeError("Invalid market maturity classification")
    return out
