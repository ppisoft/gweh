"""Primary and absolute-market pathways for candidate selection."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .yamlio import load_yaml_mapping

EVIDENCE_SELECTED = "EVIDENCE_SELECTED"
EVIDENCE_NOT_SELECTED = "EVIDENCE_NOT_SELECTED"


def _finite_nonnegative(mapping, keys, prefix):
    for key in keys:
        value = float(mapping[key])
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"{prefix}.{key} must be finite and non-negative")


def validate_selection_config(path: Path) -> dict[str, Any]:
    config = load_yaml_mapping(path)
    _finite_nonnegative(
        config["primary"],
        ("H_min", "support_min", "R_min", "market_path_min_DP", "market_path_min_S", "project_resource_wpd_min"),
        "primary",
    )
    _finite_nonnegative(
        config["absolute_market"],
        (
            "project_wpd_min", "strategic_s_min", "active_mw_min",
            "high_resource_active_mw_min", "high_resource_wpd_min",
        ),
        "absolute_market",
    )
    maturity = config["maturity"]
    _finite_nonnegative(
        maturity,
        ("CORE_min_D", "EMERGING_min_D", "EMERGING_min_P_if_D0", "PROSPECTIVE_min_P", "PROSPECTIVE_min_S"),
        "maturity",
    )
    _finite_nonnegative(
        maturity["core_operating_floor_mw"],
        ("onshore", "offshore", "mixed"),
        "maturity.core_operating_floor_mw",
    )
    return config


def _num(series):
    return pd.to_numeric(series, errors="coerce")


def evaluate_primary(scores, metrics, primary):
    inputs = ["candidate_id", "R_score", "D_score", "P_score", "S_score", "H_score"]
    resource = ["candidate_id", "project_WPD100_capacity_weighted"]
    result = scores[inputs].merge(metrics[resource], on="candidate_id", validate="one_to_one")
    for column in (*inputs[1:], resource[1]):
        result[column] = _num(result[column])

    support = result[["D_score", "P_score", "S_score"]].max(axis=1)
    market = result[["D_score", "P_score"]].max(axis=1)
    result["primary_H_pass"] = result.H_score.ge(float(primary["H_min"]))
    result["primary_support_pass"] = support.ge(float(primary["support_min"]))
    result["primary_resource_path_pass"] = result.R_score.ge(float(primary["R_min"]))
    result["primary_market_DP_pass"] = market.ge(float(primary["market_path_min_DP"]))
    result["primary_market_S_pass"] = result.S_score.ge(float(primary["market_path_min_S"]))
    result["primary_market_project_wpd_pass"] = result.project_WPD100_capacity_weighted.ge(
        float(primary["project_resource_wpd_min"])
    )
    result["primary_market_path_pass"] = (
        result.primary_market_DP_pass
        & result.primary_market_S_pass
        & result.primary_market_project_wpd_pass
    )
    result["primary_pass"] = (
        result.primary_H_pass
        & result.primary_support_pass
        & (result.primary_resource_path_pass | result.primary_market_path_pass)
    )
    # Resource pathway has precedence when both primary pathways qualify.
    result["primary_pathway"] = np.select(
        [
            result.primary_pass & result.primary_resource_path_pass,
            result.primary_pass & result.primary_market_path_pass,
        ],
        ["PRIMARY_RESOURCE", "PRIMARY_MARKET_PROJECT_RESOURCE"],
        default="NOT_PRIMARY",
    )
    return result


def evaluate_selection(scores, metrics, primary_result, absolute_market):
    resource_columns = [
        "candidate_id", "project_WPD100_capacity_weighted", "project_resource_n",
        "operating_mw_within_tolerance", "pipeline_mw_within_tolerance",
    ]
    base = scores[["candidate_id", "S_score"]].merge(
        metrics[resource_columns], on="candidate_id", validate="one_to_one"
    )
    for column in ("S_score", *resource_columns[1:]):
        base[column] = _num(base[column])
    base["active_mw_within_tolerance"] = (
        base.operating_mw_within_tolerance.fillna(0)
        + base.pipeline_mw_within_tolerance.fillna(0)
    )
    base["absolute_market_project_resource_pass"] = (
        base.project_resource_n.ge(1)
        & base.project_WPD100_capacity_weighted.notna()
        & base.project_WPD100_capacity_weighted.ge(float(absolute_market["project_wpd_min"]))
    )
    base["absolute_market_strategic_pass"] = base.S_score.ge(
        float(absolute_market["strategic_s_min"])
    )
    base["absolute_market_capacity_pass"] = base.active_mw_within_tolerance.ge(
        float(absolute_market["active_mw_min"])
    )
    base["absolute_market_high_resource_pass"] = (
        base.active_mw_within_tolerance.ge(float(absolute_market["high_resource_active_mw_min"]))
        & base.project_WPD100_capacity_weighted.ge(float(absolute_market["high_resource_wpd_min"]))
    )
    base["absolute_market_raw_pass"] = (
        base.absolute_market_project_resource_pass
        & base.absolute_market_strategic_pass
        & (base.absolute_market_capacity_pass | base.absolute_market_high_resource_pass)
    )
    flags = [
        "candidate_id", "absolute_market_project_resource_pass", "absolute_market_strategic_pass",
        "absolute_market_capacity_pass", "absolute_market_high_resource_pass",
        "absolute_market_raw_pass", "active_mw_within_tolerance",
    ]
    result = primary_result.merge(base[flags], on="candidate_id", validate="one_to_one")
    result["absolute_market_pass"] = result.absolute_market_raw_pass & ~result.primary_pass.astype(bool)
    selected = result.primary_pass.astype(bool) | result.absolute_market_pass.astype(bool)
    result["evidence_selection_status"] = np.where(
        selected, EVIDENCE_SELECTED, EVIDENCE_NOT_SELECTED
    )
    result["selection_pathway"] = np.select(
        [
            result.primary_pass & result.primary_resource_path_pass,
            result.primary_pass & result.primary_market_path_pass,
            result.absolute_market_pass,
        ],
        ["PRIMARY_RESOURCE", "PRIMARY_MARKET_PROJECT_RESOURCE", "ABSOLUTE_MARKET_RESOURCE"],
        default="NOT_SELECTED",
    )
    return result
