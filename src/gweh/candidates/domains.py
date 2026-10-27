from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.ops import unary_union

from ..yamlio import load_yaml_mapping
from ..geography.common import (
    boxes_union,
    clean_polygonal,
    coastal_band,
    country_match_mask,
    normalize_country_name,
    resolve_admin1_declarations,
    resolved_country_keys,
    union_admin1_geometry,
    union_country_geometry,
)


_SURFACES = {"onshore", "offshore", "mixed"}


def _read_geography(stage_dir, name):
    path = stage_dir / f"{name}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Stage-02 geography layer not found: {path}")
    return gpd.read_parquet(path).to_crs("EPSG:4326")


def _global_geometry(frame):
    geometry = clean_polygonal(unary_union(frame.geometry.tolist()))
    if geometry is None or geometry.is_empty:
        raise RuntimeError("Empty normalized geography layer")
    return geometry


def _local_frame(frame, envelope):
    try:
        positions = frame.sindex.query(envelope, predicate="intersects")
        return frame.iloc[np.asarray(positions, dtype=int)].copy()
    except Exception:
        return frame.loc[frame.geometry.intersects(envelope)].copy()


def _band_geometry(candidate_id, envelope, coastline, distance_km):
    if distance_km is None:
        return None
    band = coastal_band(coastline, float(distance_km), envelope=envelope)
    if band is None or band.is_empty:
        raise RuntimeError(f"{candidate_id}: coastal band of {distance_km} km is empty")
    return band

def _claim_keys(frame, names):
    """Jurisdiction keys admitted to a membership claim.

    The declared name is always admitted, because the project database may label
    a territory that the reference geography holds under its sovereign. Names
    resolved in the reference layer are added so that an equivalent territory or
    sovereign label in the project database is matched as well.
    """
    keys = {normalize_country_name(name) for name in names if str(name).strip()}
    keys.update(resolved_country_keys(frame, names))
    return sorted(key for key in keys if key)


def _resolve_declarations(
    frame, names, secondary = None
):
    """Reference features each declared jurisdiction resolves to.

    A declaration that resolves nowhere is a configuration error, reported per
    name so that one unmatched entry in a longer list cannot pass unnoticed.
    A maritime jurisdiction can be held in the EEZ layer under its sovereign
    while the project database labels it by territory, so the secondary layer
    is consulted before a declaration is rejected.
    """
    resolution = {}
    for name in names:
        if not str(name).strip():
            continue
        primary_count = int(country_match_mask(frame, [name]).sum())
        secondary_count = (
            int(country_match_mask(secondary, [name]).sum()) if secondary is not None else 0
        )
        resolution[str(name)] = {"primary": primary_count, "secondary": secondary_count}
    return resolution


def build_physical_domain(
    spec: dict[str, Any],
    envelope,
    land,
    ocean,
    coastline,
    admin0: gpd.GeoDataFrame,
    admin1: gpd.GeoDataFrame,
    eez: gpd.GeoDataFrame,
) -> dict[str, object]:
    """Build one candidate's physical domain from executable spatial constraints."""
    candidate_id = str(spec["candidate_id"])
    surface = str(spec["surface_type"]).lower()
    countries = [str(item) for item in (spec.get("countries") or [])]
    eez_countries = [str(item) for item in (spec.get("eez_countries") or [])]
    admin1_declarations = list(spec.get("admin1") or [])
    country_required = bool(countries)
    eez_required = bool(eez_countries)
    band_cfg = spec.get("coastal_band") or {}

    country_resolution = _resolve_declarations(admin0, countries)
    admin1_resolution = resolve_admin1_declarations(admin1, admin1_declarations)
    eez_resolution = _resolve_declarations(eez, eez_countries, secondary=admin0)
    if country_required:
        unmatched = [name for name, counts in country_resolution.items() if sum(counts.values()) == 0]
        if unmatched:
            raise RuntimeError(
                f"{candidate_id}: declared countries not present in the admin-0 layer: {', '.join(unmatched)}. "
                "Check the declared name against the Stage-02 reference geography."
            )
    unmatched_admin1 = [name for name, count in admin1_resolution.items() if int(count) == 0]
    if unmatched_admin1:
        raise RuntimeError(
            f"{candidate_id}: declared Admin-1 units not present in the first-order geography: "
            f"{', '.join(unmatched_admin1)}"
        )
    if eez_required:
        unmatched = [name for name, counts in eez_resolution.items() if sum(counts.values()) == 0]
        if unmatched:
            raise RuntimeError(
                f"{candidate_id}: declared EEZ jurisdictions not present in the EEZ layer: {', '.join(unmatched)}. "
                "Check the declared name against the Stage-02 reference geography."
            )

    onshore = None
    if surface in {"onshore", "mixed"}:
        onshore = clean_polygonal(envelope.intersection(land))
        if country_required:
            resolved_names = [n for n, c in country_resolution.items() if c["primary"] > 0]
            country_geometry = union_country_geometry(admin0, resolved_names)
            onshore = clean_polygonal(onshore.intersection(country_geometry)) if onshore is not None else None
        if admin1_declarations and onshore is not None and not onshore.is_empty:
            admin1_geometry = union_admin1_geometry(admin1, admin1_declarations)
            if admin1_geometry is None or admin1_geometry.is_empty:
                raise RuntimeError(f"{candidate_id}: Admin-1 declarations resolved to an empty geometry")
            onshore = clean_polygonal(onshore.intersection(admin1_geometry))
        band = _band_geometry(candidate_id, envelope, coastline, band_cfg.get("onshore_km"))
        if band is not None and onshore is not None and not onshore.is_empty:
            onshore = clean_polygonal(onshore.intersection(band))

    offshore = None
    if surface in {"offshore", "mixed"}:
        offshore = clean_polygonal(envelope.intersection(ocean))
        if eez_required:
            local_eez = _local_frame(eez, envelope)
            resolved_eez = [n for n, c in eez_resolution.items() if c["primary"] > 0]
            eez_geometry = union_country_geometry(local_eez, resolved_eez) if not local_eez.empty else None
            if eez_geometry is None or eez_geometry.is_empty:
                raise RuntimeError(f"{candidate_id}: declared EEZ countries have no geometry inside the envelope")
            offshore = clean_polygonal(offshore.intersection(eez_geometry)) if offshore is not None else None
        band = _band_geometry(candidate_id, envelope, coastline, band_cfg.get("offshore_km"))
        if band is not None and offshore is not None and not offshore.is_empty:
            offshore = clean_polygonal(offshore.intersection(band))

    if surface == "onshore":
        primary = onshore
    elif surface == "offshore":
        primary = offshore
    else:
        parts = [item for item in (onshore, offshore) if item is not None and not item.is_empty]
        primary = clean_polygonal(unary_union(parts)) if parts else None

    return {"primary": primary, "onshore": onshore, "offshore": offshore}


def _load_domains(path):
    data = load_yaml_mapping(path)
    domains = data.get("domains")
    if not isinstance(domains, dict):
        raise ValueError(f"Invalid domain configuration: {path}")
    return domains


def run_candidate_build(
    *,
    geography_dir: Path,
    candidate_registry_path: Path,
    domains_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(candidate_registry_path, dtype=str).fillna("")
    candidates["display_order"] = pd.to_numeric(candidates["display_order"], errors="raise").astype(int)
    candidates = candidates.sort_values(["display_order", "candidate_id"]).reset_index(drop=True)
    domains = _load_domains(domains_path)

    land = _global_geometry(_read_geography(geography_dir, "land"))
    ocean = _global_geometry(_read_geography(geography_dir, "ocean"))
    coastline_frame = _read_geography(geography_dir, "coastline")
    coastline = unary_union(coastline_frame.geometry.tolist())
    admin0 = _read_geography(geography_dir, "admin0")
    admin1 = _read_geography(geography_dir, "admin1")
    eez = _read_geography(geography_dir, "eez")

    envelope_rows = []
    domain_rows = []
    component_rows = []
    claim_rows = []

    for row in candidates.to_dict(orient="records"):
        candidate_id = str(row["candidate_id"])
        spec = dict(domains[candidate_id])
        spec["candidate_id"] = candidate_id
        surface = str(row["surface_type"]).lower()
        spec["surface_type"] = surface
        if surface not in _SURFACES:
            raise ValueError(f"{candidate_id}: invalid surface_type {surface}")

        envelope = boxes_union(spec.get("boxes", []))
        components = build_physical_domain(spec, envelope, land, ocean, coastline, admin0, admin1, eez)

        countries = [str(item) for item in (spec.get("countries") or [])]
        eez_countries = [str(item) for item in (spec.get("eez_countries") or [])]
        country_claim_keys = (
            _claim_keys(admin0, countries)
            if countries
            else []
        )
        eez_claim_keys = (
            _claim_keys(eez, eez_countries)
            if eez_countries
            else []
        )
        claim_rows.append(
            {
                "candidate_id": candidate_id,
                "surface_type": surface,
                "country_claim_keys": ";".join(country_claim_keys),
                "eez_claim_keys": ";".join(eez_claim_keys),
            }
        )

        primary = components["primary"]
        if primary is None or primary.is_empty:
            raise RuntimeError(f"{candidate_id}: physical domain is empty")
        if not primary.is_valid:
            raise RuntimeError(f"{candidate_id}: physical domain is invalid")

        envelope_rows.append(
            {
                "candidate_id": candidate_id,
                "surface_type": surface,
                "geometry": envelope,
            }
        )
        domain_rows.append(
            {
                "candidate_id": candidate_id,
                "surface_type": surface,
                "geometry": primary,
            }
        )

        for component in ("onshore", "offshore"):
            geometry = components[component]
            if geometry is None or geometry.is_empty:
                continue
            component_rows.append(
                {"candidate_id": candidate_id, "component": component, "geometry": geometry}
            )


    envelopes = gpd.GeoDataFrame(envelope_rows, geometry="geometry", crs="EPSG:4326")
    physical = gpd.GeoDataFrame(domain_rows, geometry="geometry", crs="EPSG:4326")
    components = gpd.GeoDataFrame(component_rows, geometry="geometry", crs="EPSG:4326")
    claims = pd.DataFrame(claim_rows)
    if physical["candidate_id"].duplicated().any():
        raise RuntimeError("Duplicate candidate IDs in physical domains")
    if set(physical["candidate_id"]) != set(candidates["candidate_id"]):
        raise RuntimeError("Physical domain universe differs from candidate registry")

    envelopes.to_parquet(output_dir / "screening_envelopes.parquet", index=False)
    physical.to_parquet(output_dir / "candidate_domains.parquet", index=False)
    components.to_parquet(output_dir / "candidate_domain_components.parquet", index=False)
    claims.to_csv(output_dir / "candidate_membership_claims.csv", index=False)

    qc = {
        "status": "PASS",
        "candidate_count": int(len(physical)),
        "component_count": int(len(components)),
        "surface_counts": {str(k): int(v) for k, v in physical["surface_type"].value_counts().items()},
        "invalid_domains": int((~physical.geometry.is_valid).sum()),
        "empty_domains": int(physical.geometry.is_empty.sum()),
        "candidate_membership_claims_written": int(len(claims)),
        "admin1_constrained_count": int(sum(bool((domains.get(str(cid), {}) or {}).get("admin1")) for cid in candidates["candidate_id"])),
    }
    return qc
