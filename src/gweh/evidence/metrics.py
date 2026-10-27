from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

from ..geography.common import geodesic_area_km2
from ..geometry.common import boolean_series, read_gwpt, read_stage_vector
from .resource import HistogramSpec, sample_project_wpd, summarize_wpd


REGION_METRIC_COLUMNS = [
    "candidate_id",
    "region_name",
    "macroregion",
    "surface_type",
    "evidence_area_km2",
    "WPD100_mean_aw",
    "WPD100_median_aw",
    "WPD100_p75_aw",
    "A300_km2",
    "A500_km2",
    "F300",
    "F500",
    "wpd_valid_area_km2",
    "project_resource_n",
    "project_WPD100_median",
    "project_WPD100_capacity_weighted",
    "operating_mw_total",
    "pipeline_mw_total",
    "operating_mw_strict_polygon",
    "pipeline_mw_strict_polygon",
    "operating_mw_within_tolerance",
    "pipeline_mw_within_tolerance",
    "coverage_denominator_mw",
    "coverage_inside_mw",
    "coverage_outside_mw",
    "coverage_strict_fraction",
    "coverage_within_tolerance_mw",
    "coverage_within_tolerance_fraction",
    "n_denominator_phases",
    "n_inside_phases",
    "n_within_tolerance_phases",
    "coverage_defined",
    "coverage_missing_reason",
    "resource_evidence_defined",
    "project_wpd_defined",
    "evidence_missing_reason",
]

PRODUCTION_RASTER_CHUNK_SIZE = 2048

MARKET_COMPONENT_COLUMNS = [
    "candidate_id",
    "region_name",
    "candidate_surface_type",
    "market_component",
    "operating_mw_component",
    "pipeline_mw_component",
    "n_active_phases_component",
]


def _owned_project_table(
    gwpt,
    ownership,
):
    owner = ownership.copy()
    owner["phase_id"] = owner["phase_id"].astype(str)
    owner["owner_candidate_id"] = owner["owner_candidate_id"].fillna("").astype(str)
    owner = owner.loc[owner["owner_candidate_id"].str.len().gt(0)].copy()
    if owner["phase_id"].duplicated().any():
        raise RuntimeError("Stage-05 ownership contains duplicate phase_id values")

    source = gwpt.copy()
    source["phase_id"] = source["phase_id"].astype(str)
    if source["phase_id"].duplicated().any():
        raise RuntimeError("Stage-01 GWPT phase_id is not unique")
    ownership_columns = ["phase_id", "owner_candidate_id", "project_surface_type", "owner_within_tolerance_of_evidence"]
    missing = [column for column in ownership_columns if column not in owner.columns]
    if missing:
        raise RuntimeError(f"Stage-05 ownership is missing required columns: {missing}")
    merged = source.merge(
        owner[ownership_columns],
        on="phase_id",
        how="inner",
        validate="one_to_one",
    )
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=gwpt.crs).to_crs("EPSG:4326")


def _coverage(owned, geometry):
    """Strict and tolerance-aware coverage of uniquely owned active capacity."""
    capacity = pd.to_numeric(owned["capacity_mw"], errors="coerce").fillna(0.0)
    denominator_mw = float(capacity.sum())
    denominator_n = int(len(owned))

    if denominator_n:
        strict_mask = owned.geometry.intersects(geometry)
        strict_inside = owned.loc[strict_mask]
        tolerance_mask = boolean_series(owned["owner_within_tolerance_of_evidence"])
        tolerance_inside = owned.loc[tolerance_mask]
    else:
        strict_inside = owned.iloc[0:0].copy()
        tolerance_inside = owned.iloc[0:0].copy()

    strict_mw = float(pd.to_numeric(strict_inside["capacity_mw"], errors="coerce").fillna(0.0).sum())
    tolerance_mw = float(pd.to_numeric(tolerance_inside["capacity_mw"], errors="coerce").fillna(0.0).sum())
    strict_n = int(len(strict_inside))
    tolerance_n = int(len(tolerance_inside))

    if denominator_mw > 0.0:
        strict_fraction = strict_mw / denominator_mw
        tolerance_fraction = tolerance_mw / denominator_mw
        return {
            "coverage_denominator_mw": denominator_mw,
            "coverage_inside_mw": strict_mw,
            "coverage_outside_mw": denominator_mw - strict_mw,
            "coverage_strict_fraction": strict_fraction,
            "coverage_within_tolerance_mw": tolerance_mw,
            "coverage_within_tolerance_fraction": tolerance_fraction,
            "n_denominator_phases": denominator_n,
            "n_inside_phases": strict_n,
            "n_within_tolerance_phases": tolerance_n,
            "coverage_defined": True,
            "coverage_missing_reason": "",
        }
    return {
        "coverage_denominator_mw": denominator_mw,
        "coverage_inside_mw": strict_mw,
        "coverage_outside_mw": 0.0,
        "coverage_strict_fraction": float("nan"),
        "coverage_within_tolerance_mw": tolerance_mw,
        "coverage_within_tolerance_fraction": float("nan"),
        "n_denominator_phases": denominator_n,
        "n_inside_phases": strict_n,
        "n_within_tolerance_phases": tolerance_n,
        "coverage_defined": False,
        "coverage_missing_reason": "NO_ACTIVE_PROJECT_DENOMINATOR",
    }


def _market_alignment(owned, geometry):
    """Market capacity aligned with the strict polygon and tolerance envelope."""
    if owned.empty:
        return {
            "operating_mw_strict_polygon": 0.0,
            "pipeline_mw_strict_polygon": 0.0,
            "operating_mw_within_tolerance": 0.0,
            "pipeline_mw_within_tolerance": 0.0,
        }
    strict = owned.geometry.intersects(geometry)
    tolerant = boolean_series(owned["owner_within_tolerance_of_evidence"])
    operating = boolean_series(owned["is_operating"])
    pipeline = boolean_series(owned["is_pipeline"])
    capacity = pd.to_numeric(owned["capacity_mw"], errors="coerce").fillna(0.0)
    return {
        "operating_mw_strict_polygon": float(capacity.loc[strict & operating].sum()),
        "pipeline_mw_strict_polygon": float(capacity.loc[strict & pipeline].sum()),
        "operating_mw_within_tolerance": float(capacity.loc[tolerant & operating].sum()),
        "pipeline_mw_within_tolerance": float(capacity.loc[tolerant & pipeline].sum()),
    }

def _market_totals(owned):
    operating = boolean_series(owned["is_operating"]) if not owned.empty else pd.Series(dtype=bool)
    pipeline = boolean_series(owned["is_pipeline"]) if not owned.empty else pd.Series(dtype=bool)
    capacity = pd.to_numeric(owned["capacity_mw"], errors="coerce").fillna(0.0)

    op_total = float(capacity.loc[operating].sum()) if len(capacity) else 0.0
    cm_total = float(capacity.loc[pipeline].sum()) if len(capacity) else 0.0
    project_surface = owned["project_surface_type"].astype(str).str.strip().str.lower()

    component_rows = []
    op_components = 0.0
    cm_components = 0.0
    for surface in ("onshore", "offshore"):
        mask = project_surface.eq(surface)
        subset = owned.loc[mask]
        subset_capacity = capacity.loc[mask]
        subset_operating = operating.loc[mask] if len(operating) else pd.Series(dtype=bool)
        subset_pipeline = pipeline.loc[mask] if len(pipeline) else pd.Series(dtype=bool)
        op = float(subset_capacity.loc[subset_operating].sum()) if len(subset_capacity) else 0.0
        cm = float(subset_capacity.loc[subset_pipeline].sum()) if len(subset_capacity) else 0.0
        component_rows.append(
            {
                "market_component": surface,
                "operating_mw_component": op,
                "pipeline_mw_component": cm,
                "n_active_phases_component": int(len(subset)),
            }
        )
        op_components += op
        cm_components += cm

    op_unclassified = max(0.0, op_total - op_components)
    cm_unclassified = max(0.0, cm_total - cm_components)
    totals = {
        "operating_mw_total": op_total,
        "pipeline_mw_total": cm_total,
        "operating_mw_unclassified_surface": op_unclassified,
        "pipeline_mw_unclassified_surface": cm_unclassified,
    }
    return totals, component_rows


def _candidate_components(surface_type):
    return ("onshore", "offshore") if surface_type == "mixed" else (surface_type,)


def _missing_reason(resource_defined, project_wpd_defined, coverage_defined):
    reasons = []
    if not resource_defined:
        reasons.append("RESOURCE_UNDEFINED")
    if not project_wpd_defined:
        reasons.append("PROJECT_WPD_UNDEFINED")
    if not coverage_defined:
        reasons.append("COVERAGE_UNDEFINED")
    return ";".join(reasons)


def run_evidence_metrics(
    *,
    ingest_dir: Path,
    exclusive_dir: Path,
    candidate_registry_path: Path,
    wpd_path: Path,
    output_dir: Path,
    resource_metric_config: dict[str, Any],
    market_qc_tolerance_mw: float,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    regions = read_stage_vector(exclusive_dir, "exclusive_regions")
    gwpt = read_gwpt(ingest_dir)
    ownership = pd.read_csv(exclusive_dir / "active_project_ownership.csv", dtype={"phase_id": str}).fillna("")
    registry = pd.read_csv(candidate_registry_path, dtype=str).fillna("")
    owned = _owned_project_table(gwpt, ownership)

    regions["candidate_id"] = regions["candidate_id"].astype(str)
    registry["candidate_id"] = registry["candidate_id"].astype(str)
    if set(registry["candidate_id"]) != set(regions["candidate_id"]):
        raise RuntimeError("Stage-06 candidate registry does not match Stage-05 exclusive regions")

    metric_rows = []
    unclassified_mw = 0.0
    component_rows = []
    histogram = resource_metric_config["histogram"]
    histogram_spec = HistogramSpec(
        minimum=float(histogram["minimum_w_m2"]),
        maximum=float(histogram["maximum_w_m2"]),
        width=float(histogram["bin_width_w_m2"]),
    )
    thresholds = [float(value) for value in resource_metric_config["area_thresholds_w_m2"]]
    required_thresholds = {300.0, 500.0}
    if not required_thresholds.issubset(set(thresholds)):
        raise ValueError("Stage-06 metric contract must include 300 and 500 W m-2 area thresholds")

    with rasterio.open(wpd_path) as dataset:
        for region in regions.itertuples(index=False):
            candidate_id = str(region.candidate_id)
            surface_type = str(region.surface_type).strip().lower()
            owned_candidate = owned.loc[owned["owner_candidate_id"].astype(str).eq(candidate_id)].copy()

            resource = summarize_wpd(
                dataset,
                region.geometry,
                chunk_size=PRODUCTION_RASTER_CHUNK_SIZE,
                histogram_spec=histogram_spec,
                thresholds=thresholds,
            )
            project_wpd = sample_project_wpd(dataset, owned_candidate)
            coverage = _coverage(owned_candidate, region.geometry)
            totals, raw_components = _market_totals(owned_candidate)
            unclassified_mw += totals["operating_mw_unclassified_surface"] + totals["pipeline_mw_unclassified_surface"]
            alignment = _market_alignment(owned_candidate, region.geometry)

            allowed_components = set(_candidate_components(surface_type))
            for component in raw_components:
                if component["market_component"] not in allowed_components:
                    continue
                component_rows.append({
                    "candidate_id": candidate_id,
                    "region_name": str(region.region_name),
                    "candidate_surface_type": surface_type,
                    **component,
                })

            resource_defined = bool(np.isfinite(resource["median_aw"]))
            project_wpd_defined = bool(project_wpd["n"] > 0 and np.isfinite(project_wpd["mean_capacity_weighted"]))
            coverage_defined = bool(coverage["coverage_defined"])
            metric_rows.append(
                {
                    "candidate_id": candidate_id,
                    "region_name": str(region.region_name),
                    "macroregion": str(getattr(region, "macroregion", "")),
                    "surface_type": surface_type,
                    "evidence_area_km2": geodesic_area_km2(region.geometry),
                    "WPD100_mean_aw": resource["mean_aw"],
                    "WPD100_median_aw": resource["median_aw"],
                    "WPD100_p75_aw": resource["p75_aw"],
                    "A300_km2": resource["A300_km2"],
                    "A500_km2": resource["A500_km2"],
                    "F300": resource["F300"],
                    "F500": resource["F500"],
                    "wpd_valid_area_km2": resource["resource_valid_area_km2"],
                    "project_resource_n": int(project_wpd["n"]),
                    "project_WPD100_median": project_wpd["median"],
                    "project_WPD100_capacity_weighted": project_wpd["mean_capacity_weighted"],
                    **totals,
                    **alignment,
                    **coverage,
                    "resource_evidence_defined": resource_defined,
                    "project_wpd_defined": project_wpd_defined,
                    "evidence_missing_reason": _missing_reason(resource_defined, project_wpd_defined, coverage_defined),
                }
            )

    metrics = pd.DataFrame(metric_rows, columns=REGION_METRIC_COLUMNS)
    components = pd.DataFrame(component_rows, columns=MARKET_COMPONENT_COLUMNS)
    metrics = metrics.sort_values("candidate_id").reset_index(drop=True)
    components = components.sort_values(["market_component", "candidate_id"]).reset_index(drop=True)

    market_active = pd.to_numeric(metrics["operating_mw_total"], errors="coerce").fillna(0.0) + pd.to_numeric(metrics["pipeline_mw_total"], errors="coerce").fillna(0.0)
    denominator = pd.to_numeric(metrics["coverage_denominator_mw"], errors="coerce")
    market_difference = market_active - denominator
    comparable = denominator.notna()
    qc_pass = ~comparable | market_difference.abs().le(float(market_qc_tolerance_mw))

    coverage = pd.to_numeric(metrics["coverage_strict_fraction"], errors="coerce")
    bad_coverage = metrics.loc[
        metrics["coverage_defined"].astype(bool)
        & (coverage.lt(-1.0e-12) | coverage.gt(1.0 + 1.0e-9))
    ]
    if not bad_coverage.empty:
        raise RuntimeError("Stage-06 coverage is outside the physical [0,1] range")
    bad_market = metrics.loc[~qc_pass]
    if not bad_market.empty:
        raise RuntimeError(
            f"Stage-06 market totals differ from the unique-ownership denominator for {len(bad_market)} candidate(s)"
        )

    metrics.to_csv(output_dir / "region_metrics.csv", index=False)
    components.to_csv(output_dir / "market_components.csv", index=False)

    qc = {
        "candidate_count": int(len(metrics)),
        "resource_defined_count": int(metrics["resource_evidence_defined"].astype(bool).sum()),
        "project_wpd_defined_count": int(metrics["project_wpd_defined"].astype(bool).sum()),
        "coverage_defined_count": int(metrics["coverage_defined"].astype(bool).sum()),
        "coverage_undefined_count": int((~metrics["coverage_defined"].astype(bool)).sum()),
        "market_active_mw_qc_fail_count": int((~qc_pass).sum()),
        "market_active_mw_qc_tolerance_mw": float(market_qc_tolerance_mw),
        "mixed_unclassified_surface_mw_total": float(unclassified_mw),
        "wpd_raster": str(wpd_path),
        "chunk_size": PRODUCTION_RASTER_CHUNK_SIZE,
        "histogram": {
            "minimum_w_m2": histogram_spec.minimum,
            "maximum_w_m2": histogram_spec.maximum,
            "bin_width_w_m2": histogram_spec.width,
        },
        "area_thresholds_w_m2": thresholds,
    }
    return qc
