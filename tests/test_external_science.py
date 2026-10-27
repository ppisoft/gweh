"""Small, source-independent examples of surface and omission methodology."""
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

from gweh.ingest.surface import apply_surface_corrections


def test_surface_inland_water_maps_to_analytical_onshore():
    frame = pd.DataFrame({"phase_id": ["x"], "surface_type_source": ["offshore"]})
    correction = pd.DataFrame([{
        "phase_id": "x", "source_surface": "offshore", "verified_surface": "inland_water",
        "evidence_url": "https://example.org/review", "reason": "Independent location inspection finds a lake",
    }])
    out, applied = apply_surface_corrections(frame, correction)
    assert out.surface_type_source.iloc[0] == "offshore"
    assert out.surface_type_verified.iloc[0] == "inland_water"
    assert out.surface_type_analytical.iloc[0] == "onshore"
    assert len(applied) == 1


def test_surface_unknown_is_not_reclassified_automatically():
    frame = pd.DataFrame({"phase_id": ["x"], "surface_type_source": ["unknown"]})
    out, _ = apply_surface_corrections(frame, pd.DataFrame(columns=[
        "phase_id", "source_surface", "verified_surface", "evidence_url", "reason"
    ]))
    assert out.surface_type_verified.iloc[0] == "unknown"
    assert out.surface_type_analytical.iloc[0] == "unknown"


def test_external_expert_omission_is_descriptive():
    from omission.screen import compare_expert_proposals
    regions = gpd.GeoDataFrame({"candidate_id": ["existing"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326")
    den = gpd.GeoDataFrame({"candidate_id": ["existing"]}, geometry=[box(-1, -1, 2, 2)], crs="EPSG:4326")
    expert = gpd.GeoDataFrame({"proposal_id": ["a", "b"], "source_url": ["https://example.org/a", "https://example.org/b"]},
                              geometry=[Point(0.5, 0.5), Point(4, 4)], crs="EPSG:4326")
    output = compare_expert_proposals(expert, regions, den).set_index("proposal_id")
    assert not bool(output.loc["a", "review_outside_existing_denominators"])
    assert bool(output.loc["b", "review_outside_existing_denominators"])


def test_external_spatial_conditional_containment():
    from validation.spatial import check_point_source
    den = gpd.GeoDataFrame({"candidate_id": ["a"]}, geometry=[box(0, 0, 2, 2)], crs="EPSG:4326")
    reg = gpd.GeoDataFrame({"candidate_id": ["a"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326")
    pts = gpd.GeoDataFrame(geometry=[Point(0.5, 0.5), Point(1.5, 1.5), Point(9, 9)], crs="EPSG:4326")
    summary, regions = check_point_source(pts, den, reg, "SYNTHETIC")
    assert summary["within_selected_denominators"] == 2
    assert summary["inside_same_selected_region"] == 1
    assert summary["conditional_containment"] == 0.5
    assert regions.in_denominator.tolist() == [2]
    from validation.spatial import _region_area_fractions
    area = _region_area_fractions(den, reg)
    updated, updated_rows = check_point_source(pts, den, reg, "SYNTHETIC", area)
    assert updated["scoped_region_area_fraction_point_weighted"] == pytest.approx(0.25, abs=0.002)
    assert updated["excess_containment_over_area_fraction"] == pytest.approx(0.25, abs=0.002)
    assert updated_rows.region_area_fraction_of_denominator.between(0, 1).all()


def test_assemble_validation_explicitly_marks_missing(tmp_path):
    from validation.prepare import assemble
    report = assemble(tmp_path / "archive")
    assert report["checks"]["spatial"]["status"] == "NOT_PROVIDED"
    assert report["checks"]["merra"]["status"] == "NOT_PROVIDED"
    assert report["status"] == "PARTIAL_EVIDENCE_ARCHIVE"


def test_irena_positive_pair_population_is_explicit():
    from validation.market import market_comparison
    config = {"source_roles": {"IRENASTAT": {
        "leaf_technologies": {"onshore": ["Onshore wind energy"],
                              "offshore": ["Offshore wind energy"]},
        "country_aliases": {}, "exclude_country_keys": ["world"],
    }}}
    gwpt = pd.DataFrame({
        "country_area": ["Alpha", "Beta", "Gamma"],
        "surface_type_source": ["onshore"] * 3,
        "is_operating": [True] * 3,
        "capacity_mw": [100.0, 200.0, 300.0],
    })
    irena = pd.DataFrame({
        "Year": [2025] * 3,
        "Technology": ["Onshore wind energy"] * 3,
        "Country/area": ["Alpha", "Beta", "Gamma"],
        "Electricity capacity statistics": [150.0, 220.0, 350.0],
    })
    result, comparison = market_comparison(gwpt, irena, year=2025, config=config)
    assert result["positive_pair_country_count"] == 3
    assert result["spearman_positive_pairs"] == pytest.approx(1.0)
    assert len(comparison) == 3


def test_irena_country_technology_pairs_preserved_with_mixed_total_records():
    from validation.market import market_comparison
    config = {"source_roles": {"IRENASTAT": {
        "leaf_technologies": {"onshore": ["Onshore wind energy"],
                              "offshore": ["Offshore wind energy"]},
        "country_aliases": {}, "exclude_country_keys": [],
    }}}
    gwpt = pd.DataFrame({
        "country_area": ["Alpha", "Alpha", "Beta", "Beta"],
        "surface_type_source": ["onshore", "offshore", "onshore", "offshore"],
        "is_operating": [True] * 4, "capacity_mw": [80, 25, 175, 35],
    })
    irena = pd.DataFrame({
        "Year": [2025] * 6,
        "Technology": ["Onshore wind energy", "Onshore wind energy", "Onshore wind energy",
                       "Offshore wind energy", "Onshore wind energy", "Offshore wind energy"],
        "Country/area": ["Alpha", "Alpha", "Alpha", "Alpha", "Beta", "Beta"],
        "Grid connection": ["Total", "On-grid", "Off-grid", "Total", "On-grid", "Total"],
        "Electricity capacity statistics": [100, 90, 10, 30, 200, 40],
    })
    result, comparison = market_comparison(gwpt, irena, year=2025, config=config)
    assert len(comparison) == 4
    assert result["positive_pair_country_technology_count"] == 4
    assert result["positive_pair_country_count"] == 2
    assert result["spearman_positive_pairs"] == pytest.approx(1.0)
    assert comparison.loc[(comparison.country_key == "beta") & (comparison.surface == "onshore"),
                          "irena_mw"].iloc[0] == 200


def test_final_validation_requires_real_external_reports(tmp_path):
    from validation.prepare import assemble
    with pytest.raises(ValueError, match="requires spatial"):
        assemble(tmp_path / "release", require_complete=True)
    assert not (tmp_path / "release").exists()


def test_irena_rejects_duplicate_input_snapshots():
    from validation.market import market_comparison
    config = {"source_roles": {"IRENASTAT": {
        "leaf_technologies": {"onshore": ["Onshore wind energy"], "offshore": ["Offshore wind energy"]},
        "country_aliases": {}, "exclude_country_keys": [],
    }}}
    gwpt = pd.DataFrame({"is_operating": [True], "surface_type_source": ["onshore"],
                         "country_area": ["Alpha"], "capacity_mw": [100.0]})
    irena = pd.DataFrame({"Year": [2025, 2025], "Technology": ["Onshore wind energy"] * 2,
                          "Country/area": ["Alpha", "Alpha"], "Grid connection": ["Total", "Total"],
                          "Electricity capacity statistics": [150.0, 150.0]})
    with pytest.raises(ValueError, match="duplicate country/technology"):
        market_comparison(gwpt, irena, year=2025, config=config)


def test_surface_correction_contract_is_minimal_and_source_bound():
    from gweh.ingest.surface import read_validated_corrections, CORRECTION_COLUMNS
    import tempfile
    from pathlib import Path

    assert CORRECTION_COLUMNS == (
        "phase_id", "source_surface", "verified_surface", "evidence_url", "reason"
    )
    original = Path(__file__).resolve().parents[1] / "config/surface_source_corrections.csv"
    accepted = read_validated_corrections(original)
    assert len(accepted) == 3
    assert "review_status" not in accepted and "confidence" not in accepted
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "invalid.csv"
        accepted.assign(confidence="HIGH").to_csv(file, index=False)
        with pytest.raises(ValueError, match="columns must be"):
            read_validated_corrections(file)


def test_anchor_exclusions_are_exact_candidate_phase_pairs():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    table = pd.read_csv(root / "config/geometry_anchor_exclusions.csv", dtype=str)
    assert list(table.columns) == ["candidate_id", "phase_id"]
    assert len(table) == 229
    assert not table[["candidate_id", "phase_id"]].duplicated().any()
    candidates = set(pd.read_csv(root / "config/candidates.csv").candidate_id)
    assert set(table.candidate_id).issubset(candidates)
