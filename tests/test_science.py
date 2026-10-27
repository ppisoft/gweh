"""Numerical and selection behaviour against a frozen 65-candidate example."""
from pathlib import Path

import numpy as np
import pandas as pd

from gweh.catalogue import run_catalogue
from gweh.scoring import positive_only_percentile_rank, percentile_rank, run_scores
from gweh.selection import evaluate_primary, evaluate_selection, validate_selection_config
from gweh.strategic_s import load_strategic_s_contract

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "tests/fixtures"


def test_scoring_and_catalogue_match_reference_bytes(tmp_path):
    stage07 = tmp_path / "07_scores"
    stage08 = tmp_path / "08_catalogue"
    strategic = load_strategic_s_contract(ROOT)
    run_scores(
        metrics_dir=REFERENCE / "06_metrics",
        scoring_config_path=ROOT / "config/scoring.yaml",
        strategic_scores_source=strategic.score_frame("REFERENCE"),
        strategic_score_column="S_score",
        output_dir=stage07,
    )
    run_catalogue(
        metrics_dir=REFERENCE / "06_metrics",
        scores_dir=stage07,
        candidate_registry_path=ROOT / "config/candidates.csv",
        selection_config_path=ROOT / "config/selection.yaml",
        output_dir=stage08,
    )
    for name, result in (
        ("07_scores/scores.csv", stage07 / "scores.csv"),
        ("08_catalogue/catalogue.csv", stage08 / "catalogue.csv"),
        ("08_catalogue/selection_trace.csv", stage08 / "selection_trace.csv"),
    ):
        assert result.read_bytes() == (REFERENCE / name).read_bytes(), name


def test_percentiles_ties_zero_and_missing():
    values = pd.Series([0.0, 0.0, 10.0, 10.0, np.nan])
    inclusive = percentile_rank(values)
    positive = positive_only_percentile_rank(values)
    assert inclusive.iloc[0] == inclusive.iloc[1]
    assert inclusive.iloc[2] == inclusive.iloc[3]
    assert positive.iloc[0] == 0.0
    assert positive.iloc[1] == 0.0
    assert positive.iloc[2] == positive.iloc[3] == 75.0
    assert np.isnan(positive.iloc[4])


def test_strategic_scores_share_candidate_universe():
    contract = load_strategic_s_contract(ROOT)
    reference = contract.score_frame("REFERENCE")
    strict = contract.score_frame("COMBINED_STRICT")
    ids = pd.read_csv(ROOT / "config/candidates.csv").candidate_id
    assert set(reference.candidate_id) == set(strict.candidate_id) == set(ids)
    assert (strict.S_score <= reference.S_score).all()


def test_selection_pathways_have_no_duplicate_candidates():
    scores = pd.read_csv(REFERENCE / "07_scores/scores.csv")
    metrics = pd.read_csv(REFERENCE / "06_metrics/region_metrics.csv")
    config = validate_selection_config(ROOT / "config/selection.yaml")
    primary = evaluate_primary(scores, metrics, config["primary"])
    selection = evaluate_selection(scores, metrics, primary, config["absolute_market"])
    assert selection.candidate_id.is_unique
    assert not (selection.primary_pass & selection.absolute_market_pass).any()
    assert set(selection.evidence_selection_status).issubset({"EVIDENCE_SELECTED", "EVIDENCE_NOT_SELECTED"})
