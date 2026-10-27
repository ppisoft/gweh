"""Source-preserving phase-specific physical-surface provenance and analytical mapping.

Only reviewed corrections with a cited phase ID are accepted. Positions never
rewrite known source/verified onshore/offshore taxonomies. Inland freshwater
siting has its own physical class, and maps to analytical onshore explicitly.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

CORRECTION_COLUMNS = (
    'phase_id', 'source_surface', 'verified_surface', 'evidence_url',
    'reason',
)
SOURCE_SURFACES = frozenset({'onshore', 'offshore', 'unknown'})
VERIFIED_SURFACES = frozenset({'onshore', 'offshore', 'inland_water'})
MANDATORY_POLICY = {
    'marine_offshore_definition': 'MARINE_WATERS_ONLY',
    'inland_water_analytical_component': 'onshore',
    'unknown_physical_surface_action': 'UNASSIGNED',
    'known_cross_surface_geometry_override': 'FORBIDDEN',
}


def check_surface_policy(policy: dict[str, Any]) -> list[str]:
    issues = []
    if not isinstance(policy, dict):
        return ['surface_policy.yaml is not a mapping']
    for key, expected in MANDATORY_POLICY.items():
        if str(policy.get(key, '')) != expected:
            issues.append(f'{key}: expected {expected!r}, observed {policy.get(key)!r}')
    unknown = sorted(set(policy) - set(MANDATORY_POLICY))
    if unknown:
        issues.append(f'unknown surface policy keys: {unknown}')
    return issues


def _clean(value: object) -> str:
    return str(value or '').strip().lower()


def read_validated_corrections(path: Path) -> pd.DataFrame:
    """Strict syntax/content validation; phase/source identity is checked at ingest."""
    corrections = pd.read_csv(path, dtype=str, keep_default_na=False)
    if tuple(corrections.columns) != CORRECTION_COLUMNS:
        raise ValueError(f'Surface correction columns must be {CORRECTION_COLUMNS}')
    if corrections['phase_id'].duplicated().any():
        raise ValueError('Duplicate phase_id in surface correction register')
    for row in corrections.itertuples(index=False):
        phase = str(row.phase_id).strip()
        raw = _clean(row.source_surface)
        verified = _clean(row.verified_surface)
        if not phase or phase != str(row.phase_id):
            raise ValueError(f'Invalid phase ID in surface corrections: {phase!r}')
        if raw not in SOURCE_SURFACES or verified not in VERIFIED_SURFACES:
            raise ValueError(f'{phase}: unsupported correction {raw!r} -> {verified!r}')
        if raw == verified:
            raise ValueError(f'{phase}: source/verified classes identical; do not add no-op correction')
        url = str(row.evidence_url).strip()
        if not re.fullmatch(r'https://[^\s]+', url, flags=re.I):
            raise ValueError(f'{phase}: missing HTTPS evidence URL')
        if len(str(row.reason).strip()) < 20:
            raise ValueError(f'{phase}: correction reason is missing or too short')
    return corrections


def apply_surface_corrections(
    frame: pd.DataFrame,
    corrections: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Add verified physical and analytical classes; never overwrite GWPT source."""
    result = frame.copy()
    if result['phase_id'].astype(str).duplicated().any():
        raise ValueError('GWPT contains duplicate phase IDs')
    result['surface_type_source'] = result['surface_type_source'].astype(str).str.lower()
    if not set(result['surface_type_source']).issubset(SOURCE_SURFACES):
        raise ValueError('GWPT contains unsupported normalized surface types')
    result['surface_type_verified'] = result['surface_type_source']
    result['surface_verification_method'] = 'SOURCE_UNCORRECTED'
    result['surface_correction_evidence_url'] = ''
    result['surface_correction_applied'] = False
    idx = pd.Index(result['phase_id'].astype(str))
    corrections_applied = []
    for row in corrections.itertuples(index=False):
        phase = str(row.phase_id)
        positions = idx.get_indexer([phase])
        if len(positions) != 1 or positions[0] < 0:
            raise ValueError(f'Approved correction phase absent from GWPT: {phase}')
        k = int(positions[0])
        observed = str(result.iloc[k]['surface_type_source'])
        declared = _clean(row.source_surface)
        if observed != declared:
            raise ValueError(f'{phase}: frozen GWPT type {observed!r} != declared source {declared!r}')
        verified = _clean(row.verified_surface)
        column_idx = result.columns.get_loc
        result.iat[k, column_idx('surface_type_verified')] = verified
        result.iat[k, column_idx('surface_verification_method')] = 'APPROVED_EVIDENCE_CORRECTION'
        result.iat[k, column_idx('surface_correction_evidence_url')] = str(row.evidence_url).strip()
        result.iat[k, column_idx('surface_correction_applied')] = True
        corrections_applied.append({
            'phase_id': phase,
            'surface_type_source': observed,
            'surface_type_verified': verified,
            'source_correction_reason': str(row.reason),
            'source_correction_evidence_url': str(row.evidence_url),
        })
    result['surface_type_analytical'] = result['surface_type_verified'].replace({'inland_water':'onshore'})
    allowed = {'onshore', 'offshore', 'unknown'}
    if not set(result['surface_type_analytical']).issubset(allowed):
        raise ValueError('Unsupported analytical surface types in processed GWPT')
    return result, pd.DataFrame(corrections_applied, columns=[
        'phase_id', 'surface_type_source', 'surface_type_verified',
        'source_correction_reason', 'source_correction_evidence_url',
    ])
