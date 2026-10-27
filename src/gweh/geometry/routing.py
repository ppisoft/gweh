"""Route known project types without inferring a new physical type from coordinates.

A location in an opposing surface component is a diagnostic spatial conflict,
not an authorization to recode a phase from onshore to offshore or vice versa.
"""
from __future__ import annotations

import geopandas as gpd

from .common import polygonal, spatial_subset, spatial_subset_tolerant


def route_candidate_points(
    points: gpd.GeoDataFrame,
    domain_parts: dict[str, object],
    component_names: list[str],
    *,
    tolerance_km: float,
) -> tuple[dict[str, gpd.GeoDataFrame], dict[str, tuple[str, str]]]:
    """Route onshore/offshore analytical types to compatible candidate components.

    Returns a per-component GeoDataFrame and per-phase (assigned_component, reason).
    If a phase cannot be routed, `assigned_component` is an empty string. In
    particular no known project type may enter an opposite-surface component,
    even when an approximate GWPT point intersects the opposite polygon.
    """
    routed = {name: points.iloc[0:0].copy() for name in component_names}
    if points.empty:
        return routed, {}
    if points['phase_id'].astype(str).duplicated().any():
        raise ValueError('Candidate routing received duplicate phase IDs')
    if not set(component_names).issubset({'onshore', 'offshore'}):
        raise ValueError('Candidate components must be onshore/offshore')
    if not {'project_surface_type_analytical', 'project_surface_type_verified', 'surface_verification_method'}.issubset(points.columns):
        raise ValueError('Routing requires immutable source/verified/analytical surface contract')

    eligible: dict[str, set[str]] = {}
    strict: dict[str, set[str]] = {}
    for component in component_names:
        base = polygonal(domain_parts.get(component))
        if base is None or base.is_empty:
            eligible[component] = set()
            strict[component] = set()
        else:
            eligible[component] = set(spatial_subset_tolerant(points, base, tolerance_km)['phase_id'].astype(str))
            strict[component] = set(spatial_subset(points, base)['phase_id'].astype(str))

    updates: dict[str, tuple[str, str]] = {}
    routed_ids = {name: [] for name in component_names}
    for item in points.itertuples(index=False):
        phase_id = str(item.phase_id)
        analytical = str(item.project_surface_type_analytical).lower()
        verified = str(item.project_surface_type_verified).lower()
        verification = str(item.surface_verification_method)
        route = ''
        if analytical == 'unknown':
            reason = 'UNROUTED_UNKNOWN_SOURCE_SURFACE'
        elif analytical not in component_names:
            # The source/verified project type is incompatible with this candidate.
            in_opposite = any(phase_id in strict.get(c, set()) for c in component_names)
            reason = 'SURFACE_CONFLICT_CANDIDATE' if in_opposite else 'EXCLUDED_SURFACE_NOT_IN_CANDIDATE'
        elif phase_id in eligible[analytical]:
            route = analytical
            if verified == 'inland_water':
                reason = 'INLAND_WATER_ANALYTICAL_ONSHORE'
            elif verification == 'APPROVED_EVIDENCE_CORRECTION':
                reason = 'VERIFIED_WITHIN_TOLERANCE'
            else:
                reason = 'SOURCE_WITHIN_TOLERANCE'
        elif any(phase_id in strict[c] for c in component_names if c != analytical):
            reason = 'SURFACE_CONFLICT_OPPOSITE_COMPONENT'
        else:
            reason = 'UNROUTED_OUTSIDE_TOLERANCE'
        updates[phase_id] = (route, reason)
        if route:
            routed_ids[route].append(phase_id)

    for component, ids in routed_ids.items():
        selected = points.loc[points['phase_id'].astype(str).isin(ids)].copy()
        if not selected.empty:
            if not selected['project_surface_type_analytical'].astype(str).str.lower().eq(component).all():
                raise RuntimeError('Routing attempted a cross-surface override')
            selected['project_surface_type_effective'] = component
            selected['surface_resolution_method'] = selected['phase_id'].astype(str).map(lambda x: updates[x][1])
        routed[component] = selected
    return routed, updates
