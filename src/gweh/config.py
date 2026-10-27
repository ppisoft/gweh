from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from .paths import RepositoryPaths
from .schema import SOURCE_ROLES, ValidationIssue, validate_candidates
from .scoring import validate_scoring_config
from .selection import validate_selection_config
from .strategic_s import validate_locked_strategic_s
from .yamlio import load_yaml_mapping
from .ingest.surface import check_surface_policy, read_validated_corrections


# Public compatibility name used by repository validation.
load_yaml = load_yaml_mapping


@dataclass(frozen=True)
class ConfigReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def ok(self) -> bool:
        return not any(issue.level == "ERROR" for issue in self.issues)


def _check_files_and_rules(repo, issues):
    """Required files are present and the scoring and selection rules parse."""
    required = [
        "sources.yaml",
        "candidates.csv",
        "domains.yaml",
        "geometry.yaml",
        "partition.yaml",
        "scoring.yaml",
        "selection.yaml",
        "boundary.yaml",
        "strategic_s.yaml",
        "geometry_anchor_exclusions.csv",
        "surface_source_corrections.csv",
        "surface_policy.yaml",
    ]
    for name in required:
        if not (repo.config / name).exists():
            issues.append(ValidationIssue("ERROR", "config.missing", f"Missing config/{name}"))
    if issues:
        return

    issues.extend(validate_candidates(repo.config / "candidates.csv"))
    try:
        validate_scoring_config(repo.config / "scoring.yaml")
    except (ValueError, KeyError, TypeError) as exc:
        issues.append(ValidationIssue("ERROR", "scoring.contract", str(exc)))

    try:
        validate_selection_config(repo.config / "selection.yaml")
    except (ValueError, KeyError, TypeError) as exc:
        issues.append(ValidationIssue("ERROR", "selection.contract", str(exc)))


def _check_candidates(repo, issues):
    """Validate the runtime candidate/domain schema."""
    candidates = pd.read_csv(repo.config / "candidates.csv", dtype=str).fillna("")
    candidate_ids = set(candidates["candidate_id"])

    domains = load_yaml(repo.config / "domains.yaml").get("domains", {})
    if not isinstance(domains, dict):
        domains = {}
        issues.append(ValidationIssue("ERROR", "domains.schema", "config/domains.yaml must contain a domains mapping"))
    if set(domains) != candidate_ids:
        issues.append(
            ValidationIssue(
                "ERROR",
                "domains.universe",
                "Domain IDs differ from candidate registry; "
                f"missing={sorted(candidate_ids - set(domains))} "
                f"extra={sorted(set(domains) - candidate_ids)}",
            )
        )

    allowed_keys = {"boxes", "countries", "eez_countries", "coastal_band", "admin1"}
    for row in candidates.to_dict(orient="records"):
        cid = str(row["candidate_id"])
        spec = domains.get(cid, {})
        if not isinstance(spec, dict):
            issues.append(ValidationIssue("ERROR", "domains.entry", f"{cid}: domain specification must be a mapping"))
            continue
        unknown = sorted(set(spec) - allowed_keys)
        if unknown:
            issues.append(ValidationIssue("ERROR", "domains.schema", f"{cid}: unknown domain keys {unknown}"))
        surface = str(row.get("surface_type", ""))
        countries = spec.get("countries") or []
        eez_countries = spec.get("eez_countries") or []
        if surface in {"onshore", "mixed"} and not countries:
            issues.append(ValidationIssue("ERROR", "domains.countries", f"{cid}: {surface} candidate requires countries"))
        if surface in {"offshore", "mixed"} and not eez_countries:
            issues.append(ValidationIssue("ERROR", "domains.eez_countries", f"{cid}: {surface} candidate requires eez_countries"))
        band = spec.get("coastal_band")
        if band is not None and not isinstance(band, dict):
            issues.append(ValidationIssue("ERROR", "domains.coastal_band", f"{cid}: coastal_band must be a mapping"))
        elif isinstance(band, dict):
            unknown = sorted(set(band) - {"onshore_km", "offshore_km"})
            if unknown:
                issues.append(ValidationIssue("ERROR", "domains.coastal_band", f"{cid}: unknown coastal_band keys {unknown}"))
            for key, value in band.items():
                if not isinstance(value, (int, float)) or float(value) <= 0:
                    issues.append(
                        ValidationIssue("ERROR", "domains.coastal_band", f"{cid}: coastal_band.{key} must be positive")
                    )
        admin1 = spec.get("admin1") or []
        if admin1 and surface not in {"onshore", "mixed"}:
            issues.append(ValidationIssue("ERROR", "domains.admin1", f"{cid}: Admin-1 constraints are valid only for onshore/mixed candidates"))
        if admin1 and not isinstance(admin1, list):
            issues.append(ValidationIssue("ERROR", "domains.admin1", f"{cid}: admin1 must be a list of country/name mappings"))
        elif isinstance(admin1, list):
            for item in admin1:
                if not isinstance(item, dict):
                    issues.append(ValidationIssue("ERROR", "domains.admin1", f"{cid}: each admin1 item must be a mapping"))
                    continue
                unknown_admin = sorted(set(item) - {"country", "names"})
                if unknown_admin:
                    issues.append(ValidationIssue("ERROR", "domains.admin1", f"{cid}: unknown admin1 keys {unknown_admin}"))
                country = str(item.get("country", "")).strip()
                names = item.get("names") or []
                if not country or not isinstance(names, list) or not [name for name in names if str(name).strip()]:
                    issues.append(ValidationIssue("ERROR", "domains.admin1", f"{cid}: each admin1 item requires country and non-empty names"))

        boxes = spec.get("boxes", [])
        if not isinstance(boxes, list) or not boxes:
            issues.append(ValidationIssue("ERROR", "domains.boxes", f"{cid}: at least one bounding box is required"))
        else:
            for values in boxes:
                try:
                    west, south, east, north = map(float, values)
                    valid = -180 <= west < east <= 180 and -90 <= south < north <= 90
                except Exception:
                    valid = False
                if not valid:
                    issues.append(ValidationIssue("ERROR", "domains.bbox", f"{cid}: invalid bounding box {values}"))


def _check_geometry_anchor_exclusions(repo, issues):
    path = repo.config / "geometry_anchor_exclusions.csv"
    try:
        table = pd.read_csv(path, dtype=str).fillna("")
    except Exception as exc:
        issues.append(ValidationIssue("ERROR", "geometry.anchor_exclusions", f"cannot read {path}: {exc}"))
        return
    required = {"candidate_id", "phase_id"}
    missing = sorted(required - set(table.columns))
    if missing:
        issues.append(ValidationIssue("ERROR", "geometry.anchor_exclusions", f"missing columns {missing}"))
        return
    if list(table.columns) != ["candidate_id", "phase_id"]:
        issues.append(ValidationIssue("ERROR", "geometry.anchor_exclusions",
                                      "expected exactly candidate_id,phase_id"))
        return
    candidates = set(pd.read_csv(repo.config / "candidates.csv", dtype=str)["candidate_id"].astype(str))
    bad_candidates = sorted(set(table["candidate_id"].astype(str)) - candidates)
    if bad_candidates:
        issues.append(ValidationIssue("ERROR", "geometry.anchor_exclusions", f"unknown candidate IDs {bad_candidates}"))
    if table[["candidate_id", "phase_id"]].duplicated().any():
        issues.append(ValidationIssue("ERROR", "geometry.anchor_exclusions", "duplicate candidate_id/phase_id rows"))
    if table["phase_id"].astype(str).str.strip().eq("").any():
        issues.append(ValidationIssue("ERROR", "geometry.anchor_exclusions", "blank phase_id"))


def _check_surface_contract(repo, issues):
    try:
        policy = load_yaml(repo.config / "surface_policy.yaml")
        for problem in check_surface_policy(policy):
            issues.append(ValidationIssue("ERROR", "surface.policy", problem))
        corrections = read_validated_corrections(repo.config / "surface_source_corrections.csv")
        if corrections.empty:
            issues.append(ValidationIssue("WARN", "surface.corrections", "No approved phase-level corrections"))
    except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
        issues.append(ValidationIssue("ERROR", "surface.corrections", str(exc)))


def _check_geometry_settings(repo, issues):
    geometry = load_yaml(repo.config / "geometry.yaml")
    try:
        if float(geometry["membership_spatial_eligibility"]["tolerance_km"]) < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        issues.append(ValidationIssue(
            "ERROR", "geometry.membership_tolerance",
            "membership_spatial_eligibility.tolerance_km must be non-negative",
        ))

def _check_sources(repo, issues):
    """The source registry and the rule files it points at."""
    sources = load_yaml(repo.config / "sources.yaml").get("sources", {})

    try:
        boundary = load_yaml(repo.config / "boundary.yaml")
        if str(boundary.get("name", "")) != "NE10m":
            raise ValueError("production boundary must be NE10m")
        boundary_sources = {
            str(boundary.get("physical_dataset_id", "")),
            str(boundary.get("admin_dataset_id", "")),
            str(boundary.get("admin1_dataset_id", "")),
            str(boundary.get("eez_dataset_id", "")),
        }
        missing_sources = sorted(source for source in boundary_sources if source and source not in sources)
        if missing_sources:
            raise ValueError(f"undeclared source IDs: {missing_sources}")
    except (ValueError, KeyError, TypeError) as exc:
        issues.append(ValidationIssue("ERROR", "boundary.contract", str(exc)))

    for source_id, metadata in sources.items():
        roles = set(metadata.get("roles", []))
        bad = sorted(roles - SOURCE_ROLES)
        if bad:
            issues.append(
                ValidationIssue("ERROR", "source.role", f"{source_id}: invalid roles {bad}")
            )

    try:
        strategic_report = validate_locked_strategic_s(
            repo.root,
            candidate_registry_path=repo.config / "candidates.csv",
            config_path=repo.config / "strategic_s.yaml",
        )
        for message in strategic_report.get("errors", []):
            issues.append(ValidationIssue("ERROR", "strategic.locked_package", str(message)))
        for message in strategic_report.get("warnings", []):
            issues.append(ValidationIssue("WARN", "strategic.locked_package", str(message)))
    except (ValueError, KeyError, TypeError, FileNotFoundError) as exc:
        issues.append(ValidationIssue("ERROR", "strategic.contract", str(exc)))


def validate_repository(repo: RepositoryPaths) -> ConfigReport:
    """Report everything wrong with the configuration, not just the first thing."""
    issues = []
    _check_files_and_rules(repo, issues)
    if any(i.level == "ERROR" and i.code == "config.missing" for i in issues):
        return ConfigReport(tuple(issues))
    _check_candidates(repo, issues)
    _check_geometry_anchor_exclusions(repo, issues)
    _check_surface_contract(repo, issues)
    _check_geometry_settings(repo, issues)
    _check_sources(repo, issues)
    return ConfigReport(tuple(issues))
