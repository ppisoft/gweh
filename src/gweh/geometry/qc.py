from __future__ import annotations

from typing import Any

import geopandas as gpd

from ..geography.common import geodesic_area_km2, union_admin1_geometry, union_country_geometry
from .common import polygonal, safe_intersection, union_polygonal


def candidate_country_names(domain_spec: dict[str, Any]) -> list[str]:
    return [str(value) for value in domain_spec.get("countries", [])]


def hard_clip_candidates(
    frame: gpd.GeoDataFrame,
    *,
    domains: dict[str, dict[str, Any]],
    admin0: gpd.GeoDataFrame,
    admin1: gpd.GeoDataFrame,
    land,
    ocean,
) -> gpd.GeoDataFrame:
    geometries = []
    for record in frame.itertuples(index=False):
        candidate_id = str(record.candidate_id)
        surface = str(record.surface_type).lower()
        geometry = polygonal(record.geometry)
        domain_spec = domains.get(candidate_id, {})
        countries = candidate_country_names(domain_spec)
        admin1_declarations = list(domain_spec.get("admin1") or [])

        if surface == "onshore":
            geometry = safe_intersection(geometry, land, label=f"{candidate_id} onshore land clip")
            if countries:
                country_geometry = union_country_geometry(admin0, countries)
                if country_geometry is None or country_geometry.is_empty:
                    raise RuntimeError(f"{candidate_id}: configured onshore countries did not resolve")
                geometry = safe_intersection(geometry, country_geometry, label=f"{candidate_id} onshore country clip")
            if admin1_declarations:
                admin1_geometry = union_admin1_geometry(admin1, admin1_declarations)
                if admin1_geometry is None or admin1_geometry.is_empty:
                    raise RuntimeError(f"{candidate_id}: configured Admin-1 units did not resolve")
                geometry = safe_intersection(geometry, admin1_geometry, label=f"{candidate_id} onshore Admin-1 clip")
        elif surface == "offshore":
            geometry = safe_intersection(geometry, ocean, label=f"{candidate_id} offshore ocean clip")

        if geometry is None or geometry.is_empty:
            raise RuntimeError(f"Hard domain clip emptied candidate {candidate_id}")
        geometries.append(geometry)
    result = frame.copy()
    result["geometry"] = geometries
    return gpd.GeoDataFrame(result, geometry="geometry", crs=frame.crs)


def union_preservation(
    base: gpd.GeoDataFrame,
    final: gpd.GeoDataFrame,
    *,
    relative_tolerance: float,
    absolute_tolerance_km2: float,
    redistributed_area_km2: float = 0.0,
    redistributed_relative_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Check union conservation with one additive geodesic area measure."""
    base_union = union_polygonal(base.geometry.tolist())
    final_union = union_polygonal(final.geometry.tolist())
    base_area = geodesic_area_km2(base_union)
    final_area = geodesic_area_km2(final_union)
    symmetric = polygonal(base_union.symmetric_difference(final_union))
    symmetric_km2 = geodesic_area_km2(symmetric) if symmetric is not None else 0.0
    area_delta_km2 = abs(base_area - final_area)

    # GEOS topology can collapse sub-precision slivers while the independent
    # additive geodesic area operator still resolves a small before/after
    # difference. For conservation we therefore use the conservative measured
    # discrepancy, while retaining both raw quantities for diagnostics.
    discrepancy_km2 = max(symmetric_km2, area_delta_km2)

    final_sum_km2 = sum(geodesic_area_km2(geometry) for geometry in final.geometry)
    final_union_sum_delta_km2 = abs(final_sum_km2 - final_area)

    absolute_term = float(absolute_tolerance_km2)
    union_term = float(relative_tolerance) * max(1.0, base_area)
    redistributed_term = float(redistributed_relative_tolerance) * float(redistributed_area_km2)
    limit = max(absolute_term, union_term, redistributed_term)
    measure_epsilon = max(0.01, 1.0e-8 * max(1.0, base_area, final_area))
    invariant_deficit_km2 = max(0.0, area_delta_km2 - symmetric_km2)
    symmetric_ge_area_delta = invariant_deficit_km2 <= measure_epsilon
    final_union_matches_sum = final_union_sum_delta_km2 <= max(measure_epsilon, absolute_term)
    measure_consistent = bool(symmetric_ge_area_delta and final_union_matches_sum)

    return {
        "union_area_km2_before": base_area,
        "union_area_km2_after": final_area,
        "union_area_absolute_delta_km2": area_delta_km2,
        "union_symmetric_difference_km2": symmetric_km2,
        "union_discrepancy_km2": discrepancy_km2,
        "union_measure_invariant_deficit_km2": invariant_deficit_km2,
        "union_measure_epsilon_km2": measure_epsilon,
        "final_component_area_sum_km2": final_sum_km2,
        "final_union_component_sum_delta_km2": final_union_sum_delta_km2,
        "measure_invariant_symdiff_ge_area_delta": bool(symmetric_ge_area_delta),
        "measure_invariant_union_equals_nonoverlap_sum": bool(final_union_matches_sum),
        "measure_consistent": measure_consistent,
        "union_tolerance_km2": limit,
        "union_tolerance_absolute_km2": absolute_term,
        "union_tolerance_union_term_km2": union_term,
        "union_tolerance_redistributed_term_km2": redistributed_term,
        "union_redistributed_area_km2": float(redistributed_area_km2),
        "union_symmetric_difference_ratio_of_limit": symmetric_km2 / limit if limit > 0 else 0.0,
        "union_discrepancy_ratio_of_limit": discrepancy_km2 / limit if limit > 0 else 0.0,
        "union_preserved": bool(measure_consistent and discrepancy_km2 <= limit),
    }

