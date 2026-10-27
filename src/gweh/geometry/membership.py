from __future__ import annotations

import geopandas as gpd
import pandas as pd

from ..geography.common import normalize_country_name
from .common import boolean_series, eligibility_geometry


MEMBERSHIP_COLUMNS = [
    "phase_id",
    "candidate_id",
    "capacity_mw",
    "is_active_boundary",
    "project_surface_type_source",
    "project_surface_type_verified",
    "project_surface_type_analytical",
    "surface_verification_method",
    "project_surface_type_effective",
    "candidate_membership_effective",
    "surface_resolution_method",
]


def _key_set(value):
    return {item for item in str(value or "").split(";") if item}


def _jurisdiction_match(country, claim):
    key = normalize_country_name(country)
    surface = str(claim["surface_type"]).lower()
    country_keys = _key_set(claim.get("country_claim_keys", ""))
    eez_keys = _key_set(claim.get("eez_claim_keys", ""))
    onshore_ok = not country_keys or key in country_keys
    offshore_ok = not eez_keys or key in eez_keys
    if surface == "onshore":
        return onshore_ok
    if surface == "offshore":
        return offshore_ok
    return onshore_ok or offshore_ok


def build_candidate_membership(
    gwpt: gpd.GeoDataFrame,
    envelopes: gpd.GeoDataFrame,
    claims: pd.DataFrame,
    *,
    positional_tolerance_km: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the phase-by-candidate membership claim.

    The claim uses only the nomination envelope and declared jurisdiction.
    Surface routing and positional consistency are resolved later in Stage 04.
    """
    if gwpt.crs is None:
        gwpt = gwpt.set_crs("EPSG:4326")
    else:
        gwpt = gwpt.to_crs("EPSG:4326")
    envelopes = envelopes.to_crs("EPSG:4326")

    required = {
        "phase_id", "capacity_mw", "country_area", "is_active_boundary",
        "surface_type_source", "surface_type_verified", "surface_type_analytical", "surface_verification_method", "geometry",
    }
    missing = sorted(required - set(gwpt.columns))
    if missing:
        raise ValueError(f"GWPT input lacks required fields: {missing}")

    claim_required = {
        "candidate_id", "surface_type", "country_claim_keys", "eez_claim_keys",
    }
    missing_claim = sorted(claim_required - set(claims.columns))
    if missing_claim:
        raise ValueError(f"Candidate membership claims lack fields: {missing_claim}")
    if claims["candidate_id"].astype(str).duplicated().any():
        raise ValueError("Candidate membership claims contain duplicate candidate_id")

    env = envelopes[["candidate_id", "surface_type", "geometry"]].rename(
        columns={"surface_type": "candidate_surface_type"}
    )
    join_env = env.copy()
    tolerance_km = float(positional_tolerance_km)
    if tolerance_km > 0:
        join_env["geometry"] = [eligibility_geometry(geometry, tolerance_km) for geometry in join_env.geometry]
        join_env = gpd.GeoDataFrame(join_env, geometry="geometry", crs=env.crs)
    joined = gpd.sjoin(
        gwpt[list(required)],
        join_env,
        how="inner",
        predicate="intersects",
    ).reset_index(drop=True)


    claim_index = claims.set_index(claims["candidate_id"].astype(str), drop=False)
    keep = []
    for row in joined.itertuples(index=False):
        candidate_id = str(row.candidate_id)
        if candidate_id not in claim_index.index:
            raise RuntimeError(f"Missing membership claim for candidate {candidate_id}")
        claim = claim_index.loc[candidate_id]
        if isinstance(claim, pd.DataFrame):
            raise RuntimeError(f"Membership claim is not unique for candidate {candidate_id}")
        keep.append(_jurisdiction_match(getattr(row, "country_area", ""), claim))
    accepted = pd.Series(keep, index=joined.index, dtype=bool)
    rejected = joined.loc[~accepted]
    claim_exclusions = pd.DataFrame({
        "phase_id": rejected["phase_id"].astype(str),
        "candidate_id": rejected["candidate_id"].astype(str),
        "capacity_mw": pd.to_numeric(rejected["capacity_mw"], errors="coerce").fillna(0.0),
        "is_active_boundary": boolean_series(rejected["is_active_boundary"]),
        "reason": "JURISDICTION_MISMATCH",
    }).drop_duplicates(["phase_id", "candidate_id"], keep="first")
    joined = joined.loc[accepted].copy()

    frame = pd.DataFrame(
        {
            "phase_id": joined["phase_id"].astype(str),
            "candidate_id": joined["candidate_id"].astype(str),
            "capacity_mw": pd.to_numeric(joined["capacity_mw"], errors="raise"),
            "is_active_boundary": boolean_series(joined["is_active_boundary"]),
            "project_surface_type_source": joined["surface_type_source"].astype(str).str.lower(),
            "project_surface_type_verified": joined["surface_type_verified"].astype(str).str.lower(),
            "project_surface_type_analytical": joined["surface_type_analytical"].astype(str).str.lower(),
            "surface_verification_method": joined["surface_verification_method"].astype(str),
            "project_surface_type_effective": joined["surface_type_analytical"].astype(str).str.lower(),
            "candidate_membership_effective": False,
            "surface_resolution_method": "UNROUTED",
        }
    )
    frame = frame.drop_duplicates(["phase_id", "candidate_id"], keep="first")


    frame = frame.sort_values(["candidate_id", "phase_id"], kind="mergesort").reset_index(drop=True)
    claim_exclusions = claim_exclusions.sort_values(["candidate_id", "phase_id"], kind="mergesort").reset_index(drop=True)
    return frame[MEMBERSHIP_COLUMNS], claim_exclusions


def active_claimed_membership(membership: pd.DataFrame, candidate_id: str) -> pd.DataFrame:
    """Active jurisdictional claim before Stage-04 spatial routing."""
    return membership.loc[
        membership["candidate_id"].astype(str).eq(str(candidate_id))
        & membership["is_active_boundary"].astype(bool)
    ].copy()

