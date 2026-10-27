from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..geography.common import geodesic_area_km2
from ..yamlio import load_yaml_mapping
from .common import read_gwpt, read_stage_vector
from .ownership import OWNERSHIP_EXPORT_COLUMNS, assign_active_project_ownership, ownership_invariants
from .partition import pair_overlap_table, partition_geometries, persist_topology_safe_parquet
from .qc import hard_clip_candidates, union_preservation


def _read_membership(stage_dir):
    path = stage_dir / "candidate_project_membership.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Candidate membership not found: {path}")
    return pd.read_parquet(path)


def _partition_layer(
    base,
    output_path,
    overlap_cfg,
    persistence_cfg,
):
    min_overlap = float(overlap_cfg["min_area_km2"])
    overlaps_before = pair_overlap_table(base, min_overlap)
    exclusive = partition_geometries(
        base,
        min_overlap_km2=min_overlap,
        residual_trigger_km2=float(persistence_cfg["cleanup_trigger_area_km2"]),
        residual_max_passes=int(persistence_cfg["cleanup_max_passes"]),
    )
    exclusive["prepartition_area_km2"] = [geodesic_area_km2(g) for g in base.geometry]
    exclusive["exclusive_area_km2"] = [geodesic_area_km2(g) for g in exclusive.geometry]
    exclusive, persistence = persist_topology_safe_parquet(
        exclusive,
        output_path,
        cleanup_trigger_km2=float(persistence_cfg["cleanup_trigger_area_km2"]),
        verify_tolerance_km2=float(persistence_cfg["postwrite_overlap_tolerance_km2"]),
        max_artifact_km2=float(persistence_cfg["cleanup_max_artifact_area_km2"]),
        max_passes=int(persistence_cfg["cleanup_max_passes"]),
    )
    overlaps_after = pair_overlap_table(exclusive, min_overlap)
    contested_km2 = (
        float(overlaps_before["overlap_area_km2"].sum()) if not overlaps_before.empty else 0.0
    )
    union = union_preservation(
        base,
        exclusive,
        redistributed_area_km2=contested_km2,
        relative_tolerance=float(overlap_cfg["union_relative_tolerance"]),
        absolute_tolerance_km2=float(persistence_cfg["union_symmetric_difference_tolerance_km2"]),
        redistributed_relative_tolerance=float(overlap_cfg["union_redistributed_relative_tolerance"]),
    )
    return exclusive, overlaps_before, overlaps_after, persistence, union


def run_exclusive_partition(
    *,
    ingest_dir: Path,
    geography_dir: Path,
    delineation_dir: Path,
    domains_path: Path,
    partition_config: dict[str, Any],
    geometry_config: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Create mutually exclusive evidence/denominator geometry and project ownership.

    The production stage keeps only state consumed downstream and hard scientific
    invariants. Detailed partition traces are part of the external validation
    workflow.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_raw = read_stage_vector(delineation_dir, "prepartition_regions")
    denominators_raw = read_stage_vector(delineation_dir, "denominator_domains")
    gwpt = read_gwpt(ingest_dir)
    membership = _read_membership(delineation_dir)
    domains = load_yaml_mapping(domains_path)["domains"]
    if not isinstance(domains, dict):
        raise ValueError("domains.yaml must contain a domains mapping")

    land = read_stage_vector(geography_dir, "land").geometry.union_all()
    ocean = read_stage_vector(geography_dir, "ocean").geometry.union_all()
    admin0 = read_stage_vector(geography_dir, "admin0")
    admin1 = read_stage_vector(geography_dir, "admin1")
    evidence_base = hard_clip_candidates(
        evidence_raw,
        domains=domains,
        admin0=admin0,
        admin1=admin1,
        land=land,
        ocean=ocean,
    )
    denominator_base = hard_clip_candidates(
        denominators_raw,
        domains=domains,
        admin0=admin0,
        admin1=admin1,
        land=land,
        ocean=ocean,
    )

    overlap_cfg = partition_config["overlap"]
    persistence_cfg = partition_config["persistence"]
    evidence_exclusive, evidence_overlaps_before, evidence_overlaps_after, evidence_persist, evidence_union = _partition_layer(
        evidence_base, output_dir / "exclusive_regions.parquet", overlap_cfg, persistence_cfg
    )
    denominator_exclusive, denominator_overlaps_before, denominator_overlaps_after, denominator_persist, denominator_union = _partition_layer(
        denominator_base, output_dir / "exclusive_denominators.parquet", overlap_cfg, persistence_cfg
    )
    active = gwpt.loc[gwpt["is_active_boundary"].astype(bool)].copy()
    ownership, admissible, spatial_exclusion_count = assign_active_project_ownership(
        active=active,
        membership=membership,
        evidence_base=evidence_base,
        denominator_base=denominator_base,
        evidence_exclusive=evidence_exclusive,
        denominator_exclusive=denominator_exclusive,
        position_tolerance_km=float(geometry_config["membership_spatial_eligibility"]["tolerance_km"]),
        land_geometry=land,
        ocean_geometry=ocean,
    )
    invariants = ownership_invariants(ownership, admissible, evidence_exclusive)

    # Persist every active phase, including non-owned and conflict phases. The
    # owner is always unique or blank; nothing is silently deleted from GWPT.
    ownership[OWNERSHIP_EXPORT_COLUMNS].to_csv(output_dir / "active_project_ownership.csv", index=False)
    conflict_claims = membership.loc[
        membership['is_active_boundary'].astype(bool)
        & membership['surface_resolution_method'].astype(str).str.startswith('SURFACE_CONFLICT')
    ].copy()
    conflicts = conflict_claims.merge(
        ownership[['phase_id','owner_candidate_id','unassigned_reason']],
        on='phase_id', how='left', validate='many_to_one',
    )
    conflicts.to_csv(output_dir / 'surface_routing_conflicts.csv', index=False)
    unowned = ownership.loc[ownership['owner_candidate_id'].astype(str).eq('')].copy()
    unowned[OWNERSHIP_EXPORT_COLUMNS + ['capacity_mw', 'status']].to_csv(
        output_dir / 'unassigned_active_phases.csv', index=False
    )
    reason_summary = (
        unowned.groupby('unassigned_reason', dropna=False)
        .agg(phase_n=('phase_id','count'), active_mw=('capacity_mw','sum'))
        .reset_index()
    )
    reason_summary.to_csv(output_dir / 'unassigned_reason_summary.csv', index=False)
    conflicting_owned = ownership.loc[
        ownership['owner_candidate_id'].astype(str).ne('')
        & ownership['analytical_surface_type'].astype(str).ne(ownership['owner_candidate_surface'].astype(str))
        & ownership['owner_candidate_surface'].astype(str).ne('mixed')
    ].copy()
    approved_change = ownership['surface_verification_method'].astype(str).eq('APPROVED_EVIDENCE_CORRECTION')
    provisional = ownership['owner_candidate_id'].astype(str).ne('')
    source_mismatch = provisional & ownership['source_surface_type'].astype(str).ne(ownership['project_surface_type'].astype(str))
    source_mismatch_unauthorized = source_mismatch & ~approved_change
    surface_summary = {
        'status': 'PASS' if conflicting_owned.empty and not source_mismatch_unauthorized.any() else 'FAIL',
        'active_phase_count': int(len(ownership)),
        'active_mw': float(pd.to_numeric(ownership['capacity_mw'], errors='coerce').fillna(0).sum()),
        'owned_phase_count': int(provisional.sum()),
        'owned_mw': float(ownership.loc[provisional,'capacity_mw'].sum()),
        'unassigned_phase_count': int(len(unowned)),
        'unassigned_mw': float(unowned['capacity_mw'].sum()),
        'unassigned_surface_conflict_phase_count': int(unowned['unassigned_reason'].eq('UNASSIGNED_SURFACE_CONFLICT').sum()),
        'unassigned_surface_conflict_mw': float(unowned.loc[unowned['unassigned_reason'].eq('UNASSIGNED_SURFACE_CONFLICT'),'capacity_mw'].sum()),
        'unassigned_reason_counts': {str(k): int(v) for k,v in unowned['unassigned_reason'].value_counts().items()},
        'approved_corrections_active_count': int(approved_change.sum()),
        'approved_corrections_active_mw': float(ownership.loc[approved_change,'capacity_mw'].sum()),
        'source_vs_owner_mismatch_count': int(source_mismatch.sum()),
        'unauthorized_source_vs_owner_mismatch_count': int(source_mismatch_unauthorized.sum()),
        'verified_vs_owner_mismatch_count': int(len(conflicting_owned)),
        'surface_conflict_candidate_claim_rows': int(len(conflicts)),
        'surface_conflict_candidate_unique_phases': int(conflicts['phase_id'].nunique()) if not conflicts.empty else 0,
        'marine_vs_inland_water_policy': 'MARINE_WATERS_ONLY / INLAND_WATER_ANALYTICAL_ONSHORE',
    }
    (output_dir/'surface_ownership_qc.json').write_text(
        json.dumps(surface_summary,indent=2,sort_keys=True)+'\n', encoding='utf-8'
    )
    if surface_summary['status'] != 'PASS':
        raise RuntimeError('Stage 05 surface ownership contract violated; see surface_ownership_qc.json')
    # Capacity conservation is a scientific inventory identity, not an
    # acceptance requirement that all global phases belong to GWEH.
    if abs(surface_summary['active_mw'] - surface_summary['owned_mw'] - surface_summary['unassigned_mw']) > 1e-6:
        raise RuntimeError('Stage 05 active GWPT phase-capacity reconciliation failed')

    post_tolerance = float(overlap_cfg["post_partition_tolerance_km2"])
    evidence_overlap_after_area = float(evidence_overlaps_after["overlap_area_km2"].sum()) if not evidence_overlaps_after.empty else 0.0
    denominator_overlap_after_area = float(denominator_overlaps_after["overlap_area_km2"].sum()) if not denominator_overlaps_after.empty else 0.0
    hard_ownership_ok = all(
        invariants[key] == 0
        for key in (
            "owner_not_admissible_count",
            "owner_not_spatially_eligible_count",
            "surface_mismatch_owned_count",
        )
    )
    checks = {
        "evidence_candidate_count": len(evidence_exclusive) == len(evidence_base),
        "denominator_candidate_count": len(denominator_exclusive) == len(denominator_base),
        "evidence_residual_overlap": evidence_overlap_after_area <= post_tolerance,
        "denominator_residual_overlap": denominator_overlap_after_area <= post_tolerance,
        "evidence_area_measure_consistent": bool(evidence_union["measure_consistent"]),
        "denominator_area_measure_consistent": bool(denominator_union["measure_consistent"]),
        "evidence_union_preserved": bool(evidence_union["union_preserved"]),
        "denominator_union_preserved": bool(denominator_union["union_preserved"]),
        "evidence_persisted_overlap": evidence_persist["persisted_overlap_pairs_after"] == 0,
        "denominator_persisted_overlap": denominator_persist["persisted_overlap_pairs_after"] == 0,
        "evidence_non_empty": not bool(evidence_exclusive.geometry.is_empty.any()),
        "denominator_non_empty": not bool(denominator_exclusive.geometry.is_empty.any()),
        "ownership_invariants": hard_ownership_ok,
    }
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    qc = {
        "stage": "05_exclusive",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if not failed_checks else "FAIL",
        "hard_checks": checks,
        "failed_checks": failed_checks,
        "candidate_count": int(len(evidence_exclusive)),
        "evidence_overlap_pairs_before": int(len(evidence_overlaps_before)),
        "evidence_overlap_pairs_after": int(len(evidence_overlaps_after)),
        "evidence_overlap_area_km2_after": evidence_overlap_after_area,
        "denominator_overlap_pairs_before": int(len(denominator_overlaps_before)),
        "denominator_overlap_pairs_after": int(len(denominator_overlaps_after)),
        "denominator_overlap_area_km2_after": denominator_overlap_after_area,
        "evidence_union": evidence_union,
        "denominator_union": denominator_union,
        "evidence_persistence": evidence_persist,
        "denominator_persistence": denominator_persist,
        "membership_position_tolerance_km": float(geometry_config["membership_spatial_eligibility"]["tolerance_km"]),
        "ownership": invariants,
        "active_project_rows": int(len(ownership)),
        "admissible_membership_rows": int(len(admissible)),
        "membership_outside_named_domain_rows": int(spatial_exclusion_count),
        "surface_ownership": surface_summary,
    }
    (output_dir / "exclusive_qc.json").write_text(json.dumps(qc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failed_checks:
        raise RuntimeError(f"Stage 05 exclusive partition failed hard QC ({', '.join(failed_checks)})")
    return qc
