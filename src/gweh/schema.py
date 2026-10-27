from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import pandas as pd


SURFACE_TYPES = {"onshore", "offshore", "mixed"}
SOURCE_ROLES = {"PRIMARY_EVIDENCE", "GEOMETRY_SUPPORT"}


CANDIDATE_COLUMNS = [
    "candidate_id",
    "region_name",
    "macroregion",
    "surface_type",
    "display_order",
]


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str


def _missing(columns, required):
    have = set(columns)
    return [column for column in required if column not in have]


def validate_candidates(path: Path) -> list[ValidationIssue]:
    df = pd.read_csv(path, dtype=str).fillna("")
    issues = []
    missing = _missing(df.columns, CANDIDATE_COLUMNS)
    if missing:
        return [ValidationIssue("ERROR", "candidate.columns", f"Missing columns: {missing}")]
    extra = sorted(set(df.columns) - set(CANDIDATE_COLUMNS))
    if extra:
        issues.append(ValidationIssue("ERROR", "candidate.columns", f"Unexpected columns: {extra}"))

    ids = df["candidate_id"].str.strip()
    if ids.eq("").any():
        issues.append(ValidationIssue("ERROR", "candidate.empty_id", "candidate_id must be non-empty"))
    if ids.duplicated().any():
        dup = sorted(ids[ids.duplicated(keep=False)].unique())
        issues.append(ValidationIssue("ERROR", "candidate.duplicate_id", f"Duplicate candidate_id: {dup}"))

    bad_surface = sorted(set(df["surface_type"]) - SURFACE_TYPES)
    if bad_surface:
        issues.append(ValidationIssue("ERROR", "candidate.surface", f"Invalid surface_type: {bad_surface}"))

    if len(df) < 1:
        issues.append(ValidationIssue("ERROR", "candidate.empty", "Candidate registry is empty"))
    return issues
