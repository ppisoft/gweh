"""Focused scientific invariants for delineation and exclusive polygons."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from gweh.geometry import delineate
from gweh.geometry.partition import pair_overlap_table


def _small_config():
    return {
        "project_coverage_target": 0.8,
        "project_link_km": {"onshore": 10.0},
        "resource_support": {
            "wpd_threshold_w_m2": 300.0,
            "coarse_resource_grid_km": 10.0,
            "min_patch_km2": 1.0,
            "link_km": {"onshore": 10.0},
            "area_fraction_no_projects": 0.55,
        },
        "morphology": {
            "margin_km": {"onshore": 1.0},
            "closing_km": {"onshore": 0.0},
            "simplify_km": 0.0,
        },
    }


def test_delineation_rejects_empty_evidence(monkeypatch):
    monkeypatch.setattr(delineate, "select_capacity_clusters", lambda *a, **k: None)
    monkeypatch.setattr(delineate, "coarse_resource_envelope", lambda *a, **k: None)
    monkeypatch.setattr(delineate, "select_resource_support", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="cannot substitute the entire candidate domain"):
        delineate._component_proposal(None, box(0, 0, 1, 1), pd.DataFrame({"capacity_mw": pd.Series(dtype=float)}), "onshore", _small_config())


def test_delineation_rejects_empty_clipped_proposal(monkeypatch):
    support = box(0.1, 0.1, 0.4, 0.4)
    monkeypatch.setattr(delineate, "coarse_resource_envelope", lambda *a, **k: support)
    monkeypatch.setattr(delineate, "select_capacity_clusters", lambda *a, **k: None)
    monkeypatch.setattr(delineate, "select_resource_support", lambda *a, **k: support)
    monkeypatch.setattr(delineate, "compact_support_envelope", lambda *a, **k: box(2, 2, 3, 3))
    with pytest.raises(RuntimeError, match="vanished after clipping"):
        delineate._component_proposal(None, box(0, 0, 1, 1), pd.DataFrame({"capacity_mw": pd.Series(dtype=float)}), "onshore", _small_config())


def test_partition_overlap_table_distinguishes_disjoint_and_overlapping():
    distinct = gpd.GeoDataFrame(
        {"candidate_id": ["a", "b"], "surface_type": ["onshore", "onshore"]},
        geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1)], crs="EPSG:4326",
    )
    assert pair_overlap_table(distinct, 0).empty
    overlaps = distinct.copy()
    overlaps.at[1, "geometry"] = box(0.9, 0, 1.5, 1)
    assert len(pair_overlap_table(overlaps, 0.01)) == 1


def test_union_preservation_requires_no_double_counting():
    from gweh.geometry.qc import union_preservation
    base = gpd.GeoDataFrame(
        {"candidate_id": ["a", "b"]}, geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)], crs="EPSG:4326"
    )
    clean = base.copy()
    qc = union_preservation(base, clean, relative_tolerance=1e-8, absolute_tolerance_km2=1e-3)
    assert qc["union_preserved"]
    overlapping = base.copy()
    overlapping.at[1, "geometry"] = box(0.5, 0, 2, 1)
    changed = union_preservation(base, overlapping, relative_tolerance=1e-8, absolute_tolerance_km2=1e-3)
    assert not changed["union_preserved"]


def test_ownership_surface_crossings_are_not_admissible():
    from gweh.geometry.ownership import build_membership_admissibility
    from shapely.geometry import Point
    active = gpd.GeoDataFrame({"phase_id": ["x"]}, geometry=[Point(0.5, 0.5)], crs="EPSG:4326")
    candidates = gpd.GeoDataFrame(
        {"candidate_id": ["land", "sea"], "surface_type": ["onshore", "offshore"]},
        geometry=[box(0, 0, 1, 1), box(2, 0, 3, 1)], crs="EPSG:4326",
    )
    membership = pd.DataFrame({
        "phase_id": ["x", "x"], "candidate_id": ["land", "sea"],
        "is_active_boundary": [True, True],
        "candidate_membership_effective": [True, True],
        "surface_resolution_method": ["STRICT_ROUTING", "STRICT_ROUTING"],
        "project_surface_type_analytical": ["onshore", "onshore"],
        "project_surface_type_effective": ["onshore", "onshore"],
    })
    accepted = build_membership_admissibility(membership, active, candidates)
    assert accepted.candidate_id.tolist() == ["land"]


def test_ownership_rejects_duplicate_active_phase_ids():
    from gweh.geometry.ownership import assign_active_project_ownership
    from shapely.geometry import Point
    active = gpd.GeoDataFrame(
        {"phase_id": ["x", "x"]}, geometry=[Point(0, 0), Point(0, 0)], crs="EPSG:4326"
    )
    with pytest.raises(RuntimeError, match="not unique"):
        assign_active_project_ownership(
            active=active, membership=pd.DataFrame(),
            evidence_base=active, denominator_base=active,
            evidence_exclusive=active, denominator_exclusive=active,
            position_tolerance_km=0,
        )
