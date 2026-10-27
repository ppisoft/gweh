"""Critical failure modes: incomplete external reports and mislabelled V4 tiers."""
import json
import pandas as pd
import pytest

from validation.prepare import _check_merra, _check_spatial
from workflow.run import Settings


def test_validation_rejects_old_merra_wrong_climate_panel(tmp_path):
    ids = ["north", "east", "west", "south"]
    tab = pd.DataFrame({"candidate_id": ids, "year": [2010]*4})
    out = tmp_path / "merra"; out.mkdir()
    pd.DataFrame({"candidate_id": ids, "evidence_selection_status":
                  ["EVIDENCE_SELECTED"]*3 + ["EVIDENCE_NOT_SELECTED"]}).to_csv(
        out / "V4_merra_candidate_values.csv", index=False)
    tab.to_csv(out / "V4_merra_yearly.csv", index=False)
    (out / "V4_summary.json").write_text(json.dumps({
        "status": "COMPUTED", "candidate_count": len(ids),
        "period": ["2010-01-01", "2010-12-31"],
        "comparison": {
            "all_candidates": {"population_n": 4},
            "evidence_selected": {"population_n": 3},
            "climate_eligible": {"population_n": 3},  # incorrect fallback
            "primary_climate": {"population_n": 3},  # incorrect fallback
        },
    }))
    roster = tmp_path / "roster.csv"
    pd.DataFrame({"candidate_id": ids,
                  "climate_analysis_eligible": [1,1,0,0],
                  "primary_climate_panel": [1,0,0,0]}).to_csv(roster,index=False)
    with pytest.raises(ValueError, match="climate_eligible.*requires 2"):
        _check_merra(out, roster)


def test_validation_rejects_missing_external_sources(tmp_path):
    path=tmp_path / "spatial";path.mkdir()
    (path / "V1_V2_summary.json").write_text(json.dumps({
        "status": "COMPUTED", "population_candidate_count": 2,
        "sources": [{"source":"GOWIRES", "valid_point_count":1,
                     "within_selected_denominators":1,"inside_same_selected_region":1,
                     "conditional_containment":1.0}]
    }))
    pd.DataFrame({"source":["GOWIRES"],"candidate_id":["x"],
                  "in_denominator":[1],"in_same_selected_region":[1]}).to_csv(
        path / "V1_V2_candidate_containment.csv",index=False)
    with pytest.raises(ValueError, match="missing"):
        _check_spatial(path, __import__("pathlib").Path(__file__).parents[1] / "validation/config/sources.yaml")


def test_workflow_rejects_missing_config_before_writing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Workflow configuration"):
        Settings(tmp_path / "not_here.yaml")


def test_merra_archive_checks_statistic_against_current_roster(tmp_path):
    """A correct group count must not conceal a miscomputed statistic."""
    ids = ["west", "north", "east", "south"]
    folder = tmp_path / "merra"
    folder.mkdir()
    pd.DataFrame({
        "candidate_id": ids,
        "evidence_selection_status": ["EVIDENCE_SELECTED"] * 3 + ["EVIDENCE_NOT_SELECTED"],
        "mean_wpd50_proxy": [10., 20., 30., 40.],
        "WPD100_mean_aw": [30., 20., 10., 40.],
    }).to_csv(folder / "V4_merra_candidate_values.csv", index=False)
    pd.DataFrame({"candidate_id": ids, "year": [2020] * 4}).to_csv(
        folder / "V4_merra_yearly.csv", index=False)
    roster = tmp_path / "roster.csv"
    pd.DataFrame({"candidate_id": ids, "climate_analysis_eligible": [1, 1, 0, 0],
                  "primary_climate_panel": [1, 0, 0, 0]}).to_csv(roster, index=False)
    report = {
        "status": "COMPUTED", "candidate_count": 4,
        "period": ["2020-01-01", "2020-12-31"],
        "comparison": {
            "all_candidates": {"population_n": 4, "n": 4, "spearman_rho": 0.2},
            "evidence_selected": {"population_n": 3, "n": 3, "spearman_rho": -1.0},
            "climate_eligible": {"population_n": 2, "n": 2, "spearman_rho": None},
            "primary_climate": {"population_n": 1, "n": 1, "spearman_rho": None},
        },
    }
    (folder / "V4_summary.json").write_text(json.dumps(report))
    # 4-candidate rho 0.2 is the actual value for these data.
    assert _check_merra(folder, roster)["candidate_count"] == 4
    report["comparison"]["climate_eligible"]["population_n"] = 3
    (folder / "V4_summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="climate_eligible.*requires 2"):
        _check_merra(folder, roster)
    report["comparison"]["climate_eligible"]["population_n"] = 2
    report["comparison"]["all_candidates"]["spearman_rho"] = -1.0
    (folder / "V4_summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Spearman correlation differs"):
        _check_merra(folder, roster)


def test_public_merra_context_removes_only_private_root():
    from validation.prepare import _public_merra_context
    original = {"blind_geometry_sha256": "secretgeometry", "merra_root": "/home/user/raw/MERRA2",
                "start": "2008-01-01", "end": "2017-12-31", "merra_grid_shape": [361, 576]}
    public = _public_merra_context(original)
    assert public["merra_root"] == "<external MERRA-2 input directory>"
    assert public["blind_geometry_sha256"] == original["blind_geometry_sha256"]
    assert public["merra_grid_shape"] == original["merra_grid_shape"]
    assert original["merra_root"] == "/home/user/raw/MERRA2"


def test_published_merra_protocol_must_preserve_seed(tmp_path):
    from pathlib import Path
    from validation.prepare import check_merra_protocol
    root = Path(__file__).resolve().parents[1]
    protocol = tmp_path / "methodology/merra_protocol.yaml"
    protocol.parent.mkdir()
    correct = (root / "validation/config/merra.yaml").read_text()
    protocol.write_text(correct)
    check_merra_protocol(tmp_path)
    protocol.write_text(correct.replace("blind_seed: 20261018", ""))
    with pytest.raises(ValueError, match="protocol differs"):
        check_merra_protocol(tmp_path)


def test_merra_dictionary_covers_yearly_schema_and_blind_roster():
    from pathlib import Path
    from validation.merra import METRICS
    root = Path(__file__).resolve().parents[1]
    dictionary = pd.read_csv(root / "metadata/data_dictionary.csv")
    assert not set(dictionary.table).intersection({"nomination_round_candidates", "nomination_round_summary"})
    base = {"validation_id", "year", "merra_center_cells", "days_processed", "time_steps", "input_signature"}
    for metric in METRICS:
        base.update({metric + "_n", metric + "_d"})
    got = set(dictionary.loc[dictionary.table ==
            "validation/merra_provenance/annual/year_YYYY.csv", "field"])
    assert base == got
    assert set(dictionary.loc[dictionary.table ==
            "validation/merra_provenance/blind/roster.csv", "field"]) == {"validation_id"}
