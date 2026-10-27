"""Construct pre-partition candidate geometries from frozen inputs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import rasterio

from .common import polygonal, read_gwpt, read_stage_vector, safe_intersection, union_polygonal
from .membership import active_claimed_membership, build_candidate_membership
from .routing import route_candidate_points
from .support import (
    apply_anchor_guard,
    coarse_resource_envelope,
    compact_support_envelope,
    component_capacity_clusters,
    select_capacity_clusters,
    select_resource_support,
)


def _component_domains(
    domains,
    components,
    candidate_id,
):
    row = domains.loc[domains["candidate_id"].astype(str).eq(candidate_id)]
    if len(row) != 1:
        raise RuntimeError(f"Expected one physical domain for {candidate_id}")
    result = {
        "primary": polygonal(row.iloc[0].geometry),
        "onshore": None,
        "offshore": None,
    }
    subset = components.loc[components["candidate_id"].astype(str).eq(candidate_id)]
    for item in subset.itertuples(index=False):
        result[str(item.component)] = polygonal(item.geometry)
    return result


def _candidate_claim_points(
    gwpt,
    membership,
    candidate_id,
):
    selected = active_claimed_membership(membership, candidate_id)
    if selected.empty:
        columns = list(selected.columns) + ["geometry"]
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs="EPSG:4326")
    source = gwpt[["phase_id", "geometry"]].copy()
    source["phase_id"] = source["phase_id"].astype(str)
    selected["phase_id"] = selected["phase_id"].astype(str)
    merged = selected.merge(source, on="phase_id", how="left", validate="many_to_one")
    if merged["geometry"].isna().any():
        missing = merged.loc[merged["geometry"].isna(), "phase_id"].astype(str).tolist()
        raise RuntimeError(f"Missing GWPT geometry for candidate membership: {missing[:10]}")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs="EPSG:4326")


def _capacity(frame):
    return float(pd.to_numeric(frame.get("capacity_mw", 0), errors="coerce").fillna(0.0).sum())


def _route_updates(
    membership,
    candidate_id,
    updates,
):
    for phase_id, (route, method) in updates.items():
        mask = (
            membership["candidate_id"].astype(str).eq(candidate_id)
            & membership["phase_id"].astype(str).eq(phase_id)
        )
        membership.loc[mask, "candidate_membership_effective"] = bool(route)
        if route:
            expected = membership.loc[mask, "project_surface_type_analytical"].astype(str).str.lower()
            if not expected.eq(route).all():
                raise RuntimeError(f"{phase_id}: route changed the verified analytical surface")
            membership.loc[mask, "project_surface_type_effective"] = route
        membership.loc[mask, "surface_resolution_method"] = method


def _component_proposal(
    dataset,
    base,
    points,
    component,
    geometry_config,
):
    morphology = geometry_config["morphology"]
    support_cfg = geometry_config["resource_support"]
    project_target = float(geometry_config["project_coverage_target"])
    link_km = float(geometry_config["project_link_km"][component])
    clusters = component_capacity_clusters(points, link_km) if not points.empty else []
    project_anchor = select_capacity_clusters(clusters, project_target, _capacity(points))
    resource = coarse_resource_envelope(
        dataset,
        base,
        threshold_w_m2=float(support_cfg["wpd_threshold_w_m2"]),
        coarse_resource_grid_km=float(support_cfg["coarse_resource_grid_km"]),
        min_patch_km2=float(support_cfg["min_patch_km2"]),
    )
    resource = select_resource_support(
        resource,
        project_anchor,
        link_km=float(support_cfg["link_km"][component]),
        area_fraction_no_projects=float(support_cfg["area_fraction_no_projects"]),
    )
    support = union_polygonal([project_anchor, resource])
    if support is None or support.is_empty:
        raise RuntimeError(
            f"{component}: no qualifying project or wind-resource support; "
            "cannot substitute the entire candidate domain"
        )
    proposal = compact_support_envelope(
        support,
        margin_km=float(morphology["margin_km"][component]),
        closing_km=float(morphology["closing_km"][component]),
        simplify_km=float(morphology["simplify_km"]),
    )
    proposal = safe_intersection(proposal, base, label=f"{component} domain clip")
    if proposal is None or proposal.is_empty:
        raise RuntimeError(
            f"{component}: compact support vanished after clipping to the domain"
        )
    return compact_support_envelope(
        proposal,
        margin_km=0.0,
        closing_km=0.0,
        simplify_km=float(morphology["simplify_km"]),
    )


def run_delineation(
    *,
    ingest_dir: Path,
    candidates_dir: Path,
    candidate_registry_path: Path,
    geometry_anchor_exclusions_path: Path,
    geometry_config: dict[str, Any],
    wpd_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Build pre-partition regions and the project membership used by Stage 05.

    Stage 04 intentionally emits only load-bearing scientific state plus hard QC.
    Detailed routing, support-component and coverage diagnostics live in the
    external validation workflow.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    gwpt = read_gwpt(ingest_dir)
    envelopes = read_stage_vector(candidates_dir, "screening_envelopes")
    domains = read_stage_vector(candidates_dir, "candidate_domains")
    components = read_stage_vector(candidates_dir, "candidate_domain_components")
    registry = pd.read_csv(candidate_registry_path, dtype=str).fillna("")
    registry["display_order"] = pd.to_numeric(registry["display_order"], errors="raise").astype(int)
    registry = registry.sort_values(["display_order", "candidate_id"]).reset_index(drop=True)
    exclusions = pd.read_csv(geometry_anchor_exclusions_path, dtype=str).fillna("")
    excluded_by_candidate = {
        str(cid): set(group["phase_id"].astype(str))
        for cid, group in exclusions.groupby("candidate_id", sort=False)
    }

    claims = pd.read_csv(candidates_dir / "candidate_membership_claims.csv", dtype=str).fillna("")
    membership, claim_exclusions = build_candidate_membership(
        gwpt,
        envelopes,
        claims,
        positional_tolerance_km=float(geometry_config["membership_spatial_eligibility"]["tolerance_km"]),
    )

    morphology = geometry_config["morphology"]
    eligibility_cfg = geometry_config["membership_spatial_eligibility"]
    tolerance_km = float(eligibility_cfg["tolerance_km"])

    region_rows = []
    denominator_rows = []
    anchor_guard_candidates = 0

    with rasterio.open(wpd_path) as wpd:
        for candidate in registry.to_dict(orient="records"):
            cid = str(candidate["candidate_id"])
            surface = str(candidate["surface_type"]).lower()
            domain_parts = _component_domains(domains, components, cid)
            primary = domain_parts["primary"]
            if primary is None or primary.is_empty:
                raise RuntimeError(f"{cid}: empty physical domain")

            claim_points = _candidate_claim_points(gwpt, membership, cid)
            component_names = ["onshore", "offshore"] if surface == "mixed" else [surface]
            routed_points, route_updates = route_candidate_points(
                claim_points,
                domain_parts,
                component_names,
                tolerance_km=tolerance_km,
            )

            _route_updates(membership, cid, route_updates)

            anchor_exclusions = excluded_by_candidate.get(cid, set())
            proposal_points = {
                component: frame.loc[~frame["phase_id"].astype(str).isin(anchor_exclusions)].copy()
                for component, frame in routed_points.items()
            }
            used = [frame for frame in proposal_points.values() if not frame.empty]
            effective_points = (
                gpd.GeoDataFrame(pd.concat(used, ignore_index=True), geometry="geometry", crs="EPSG:4326")
                if used else claim_points.iloc[0:0].copy()
            )

            proposals = []
            for component in component_names:
                base = polygonal(domain_parts.get(component))
                if base is None or base.is_empty:
                    continue
                proposals.append(
                    _component_proposal(wpd, base, proposal_points[component], component, geometry_config)
                )

            final = union_polygonal(proposals)
            if final is None or final.is_empty:
                raise RuntimeError(
                    f"{cid}: no candidate component produced evidence-supported geometry"
                )
            final = safe_intersection(final, primary, label=f"{cid} primary domain clip")
            if final is None or final.is_empty:
                raise RuntimeError(f"{cid}: delineation produced empty geometry")

            guarded, guard_applied = apply_anchor_guard(
                final,
                domain_parts,
                effective_points,
                candidate_surface=surface,
                guard_km=float(morphology["project_anchor_guard_km"]),
                position_tolerance_km=tolerance_km,
            )
            guarded = safe_intersection(guarded, primary, label=f"{cid} guarded domain clip")
            if guarded is None or guarded.is_empty:
                raise RuntimeError(f"{cid}: anchor guard produced empty geometry")
            anchor_guard_candidates += int(guard_applied)

            region_rows.append(
                {
                    "candidate_id": cid,
                    "region_name": candidate["region_name"],
                    "macroregion": candidate["macroregion"],
                    "surface_type": surface,
                    "geometry": guarded,
                }
            )
            denominator_rows.append(
                {
                    "candidate_id": cid,
                    "region_name": candidate["region_name"],
                    "surface_type": surface,
                    "geometry": primary,
                }
            )

    membership.to_parquet(output_dir / "candidate_project_membership.parquet", index=False)

    regions = gpd.GeoDataFrame(region_rows, geometry="geometry", crs="EPSG:4326")
    denominators = gpd.GeoDataFrame(denominator_rows, geometry="geometry", crs="EPSG:4326")
    regions.to_parquet(output_dir / "prepartition_regions.parquet", index=False)
    denominators.to_parquet(output_dir / "denominator_domains.parquet", index=False)

    active = membership["is_active_boundary"].astype(bool)
    claimed = active
    effective = active & membership["candidate_membership_effective"].astype(bool)
    methods = membership["surface_resolution_method"].astype(str)
    unrouted = claimed & methods.eq("UNROUTED_OUTSIDE_TOLERANCE")
    wrong_surface = claimed & methods.isin(("EXCLUDED_SURFACE_NOT_IN_CANDIDATE", "SURFACE_CONFLICT_CANDIDATE", "SURFACE_CONFLICT_OPPOSITE_COMPONENT"))
    surface_conflict = claimed & methods.str.startswith("SURFACE_CONFLICT")
    unknown = claimed & methods.eq("UNROUTED_UNKNOWN_SOURCE_SURFACE")
    if (claimed & methods.eq("AUTO_GEOMETRY_OVERRIDE")).any():
        raise RuntimeError("Known physical surface was automatically overridden")
    active_claim_exclusions = claim_exclusions.loc[claim_exclusions["is_active_boundary"].astype(bool)]

    qc = {
        "stage": "04_delineation",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "candidate_count": int(len(registry)),
        "region_count": int(len(regions)),
        "denominator_count": int(len(denominators)),
        "invalid_regions": int((~regions.geometry.is_valid).sum()),
        "empty_regions": int(regions.geometry.is_empty.sum()),
        "membership_rows": int(len(membership)),
        "jurisdiction_exclusion_rows": int(len(claim_exclusions)),
        "active_jurisdiction_exclusion_rows": int(len(active_claim_exclusions)),
        "active_jurisdiction_exclusion_mw": _capacity(active_claim_exclusions),
        "active_membership_claim_rows": int(claimed.sum()),
        "active_effective_membership_rows": int(effective.sum()),
        "anchor_guard_candidates": int(anchor_guard_candidates),
        "geometry_anchor_exclusion_rows": int(len(exclusions)),
        "geometry_anchor_exclusion_candidates": int(exclusions["candidate_id"].nunique()),
        "membership_position_tolerance_km": tolerance_km,
        "surface_geometry_overrides": 0,
        "surface_conflict_membership_rows": int(surface_conflict.sum()),
        "surface_conflict_membership_mw_claims": _capacity(membership.loc[surface_conflict]),
        "unknown_surface_membership_rows": int(unknown.sum()),
        "unknown_surface_membership_mw_claims": _capacity(membership.loc[unknown]),
        "unrouted_membership_phases": int(unrouted.sum()),
        "unrouted_membership_mw": _capacity(membership.loc[unrouted]),
        "surface_not_in_candidate_phases": int(wrong_surface.sum()),
        "surface_not_in_candidate_mw": _capacity(membership.loc[wrong_surface]),
        "wpd_source": str(wpd_path),
    }
    if len(regions) != len(registry) or len(denominators) != len(registry):
        raise RuntimeError("Delineation candidate universe changed")
    if qc["invalid_regions"] or qc["empty_regions"]:
        raise RuntimeError("Delineation produced invalid or empty candidate geometry")
    return qc
