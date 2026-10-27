"""Evidence-based Strategic-S assessment and scoring rubric."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .yamlio import load_yaml_mapping

_REQUIRED_ASSESSMENT = {
    "candidate_id", "region_name", "S1", "S2", "S3", "S4",
    "negative_policy", "viable_alternative", "exceptional_integrated_programme",
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n", ""}:
        return False
    raise ValueError(f"Cannot parse boolean value {value!r}")


def _condition_matches(row: pd.Series, condition: dict[str, Any]) -> bool:
    if condition.get("otherwise") is True:
        return True
    for key, expected in condition.items():
        if key == "otherwise":
            continue
        if key.endswith("_min"):
            if float(row[key[:-4]]) < float(expected):
                return False
        elif key.endswith("_max"):
            if float(row[key[:-4]]) > float(expected):
                return False
        elif key in {"viable_alternative", "exceptional_integrated_programme"}:
            if _as_bool(row[key]) is not bool(expected):
                return False
        else:
            try:
                if float(row[key]) == float(expected):
                    continue
            except (TypeError, ValueError):
                if row[key] == expected:
                    continue
            return False
    return True


def _mapped_score(row, mapping):
    """Apply the first matching rule, in the order specified by the rubric."""
    ordered = mapping.get("ordered_rules")
    if not isinstance(ordered, list) or not ordered:
        raise ValueError("Strategic-S mapping must contain non-empty ordered_rules")
    for rule in ordered:
        if _condition_matches(row, rule.get("when", {})):
            return int(rule["score"])
    raise ValueError(f"No Strategic-S rule matched candidate {row['candidate_id']}")


def _apply_policy_cap(score, row, rules, variant):
    policy = str(row["negative_policy"])
    viable = _as_bool(row["viable_alternative"])
    branch = "viable_alternative_true" if viable else "viable_alternative_false"
    cap = rules["negative_policy_caps"][policy][branch].get(f"{variant}_max")
    return min(int(score), int(cap)) if cap is not None else int(score)


def validate_assessment(table, rules):
    missing = _REQUIRED_ASSESSMENT - set(table.columns)
    if missing:
        raise ValueError(f"Strategic-S assessment missing columns {sorted(missing)}")
    if table["candidate_id"].astype(str).duplicated().any():
        raise ValueError("Strategic-S assessment contains duplicate candidate_id values")
    components = rules.get("components")
    if not isinstance(components, dict):
        raise ValueError("Strategic-S rules missing components mapping")
    for component, spec in components.items():
        if component not in table.columns:
            raise ValueError(f"Strategic-S assessment missing component {component}")
        lower, upper = [int(value) for value in spec["range"]]
        values = pd.to_numeric(table[component], errors="coerce")
        invalid = values.isna() | values.lt(lower) | values.gt(upper) | values.mod(1).ne(0)
        if invalid.any():
            raise ValueError(f"Strategic-S {component} must be integer {lower}..{upper}")
    caps = rules.get("negative_policy_caps")
    invalid_policy = ~table["negative_policy"].astype(str).isin(set(caps))
    if invalid_policy.any():
        raise ValueError("Unknown Strategic-S negative_policy value")
    for component in ("viable_alternative", "exceptional_integrated_programme"):
        for value in table[component]:
            _as_bool(value)


def derive_strategic_scores(table, rules):
    validate_assessment(table, rules)
    rows = []
    for raw in table.itertuples(index=False):
        row = pd.Series(raw._asdict())
        reference = _apply_policy_cap(
            _mapped_score(row, rules["reference_mapping"]), row, rules, "reference"
        )
        strict = _apply_policy_cap(
            _mapped_score(row, rules["strict_mapping"]), row, rules, "strict"
        )
        if rules.get("invariants", {}).get("strict_must_not_exceed_reference", False) and strict > reference:
            raise ValueError(f"{row['candidate_id']}: S_strict exceeds S_reference")
        rows.append({
            "candidate_id": str(row["candidate_id"]),
            "region_name": str(row["region_name"]),
            "S1": int(row["S1"]),
            "S2": int(row["S2"]),
            "S3": int(row["S3"]),
            "S4": int(row["S4"]),
            "S_reference": int(reference),
            "S_strict": int(strict),
        })
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class StrategicSConfig:
    repo_root: Path
    raw: dict[str, Any]

    @property
    def package_dir(self):
        return (self.repo_root / str(self.raw["package_dir"])).resolve()

    @property
    def assessment_path(self):
        return self.package_dir / str(self.raw["assessment_file"])

    @property
    def rules_path(self):
        return self.package_dir / str(self.raw["rules_file"])

    @property
    def production_score(self):
        return str(self.raw.get("production_score", "S_reference"))

    @property
    def sensitivity_score(self):
        return str(self.raw.get("mapping_sensitivity_score", "S_strict"))

    def derived_table(self):
        assessment = pd.read_csv(self.assessment_path, dtype=str).fillna("")
        return derive_strategic_scores(assessment, load_yaml_mapping(self.rules_path))

    def score_frame(self, variant: str):
        table = self.derived_table()
        variant = str(variant)
        if variant == "REFERENCE":
            score = pd.to_numeric(table[self.production_score], errors="raise")
        elif variant == "COMBINED_STRICT":
            score = pd.to_numeric(table[self.sensitivity_score], errors="raise")
        elif variant in {"S_MINUS1", "ORDINAL_MINUS1"}:
            score = (pd.to_numeric(table[self.production_score], errors="raise") - 1).clip(lower=0)
        elif variant in {"S_MINUS2", "ORDINAL_MINUS2"}:
            score = (pd.to_numeric(table[self.production_score], errors="raise") - 2).clip(lower=0)
        elif variant in {"S_ZERO", "ZERO"}:
            score = pd.Series(0, index=table.index)
        elif variant in {"S_PLUS1", "ORDINAL_PLUS1"}:
            score = (pd.to_numeric(table[self.production_score], errors="raise") + 1).clip(upper=4)
        else:
            raise KeyError(f"Unknown Strategic-S variant: {variant}")
        return pd.DataFrame({
            "candidate_id": table["candidate_id"].astype(str),
            "S_score": score.astype(int),
        })


def load_strategic_s_contract(
    repo_root: Path, config_path: Path | None = None
) -> StrategicSConfig:
    root = Path(repo_root).resolve()
    path = Path(config_path).resolve() if config_path else root / "config/strategic_s.yaml"
    raw = load_yaml_mapping(path)
    required = {"package_dir", "assessment_file", "rules_file", "production_score", "mapping_sensitivity_score"}
    missing = required - set(raw)
    if missing:
        raise ValueError(f"config/strategic_s.yaml missing keys {sorted(missing)}")
    return StrategicSConfig(root, raw)


def validate_locked_strategic_s(
    repo_root: Path,
    candidate_registry_path: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    config = load_strategic_s_contract(repo_root, config_path)
    errors = []
    for path, label in ((config.assessment_path, "assessment"), (config.rules_path, "rules")):
        if not path.exists():
            errors.append(f"strategic-S {label} missing: {path}")
    candidate_path = (
        Path(candidate_registry_path).resolve()
        if candidate_registry_path else config.repo_root / "config/candidates.csv"
    )
    candidates = pd.read_csv(candidate_path, dtype=str).fillna("")
    identifiers = set(candidates["candidate_id"].astype(str))
    names = dict(zip(candidates.candidate_id.astype(str), candidates.region_name.astype(str)))
    try:
        derived = config.derived_table()
        derived_ids = derived.candidate_id.astype(str)
        if derived_ids.duplicated().any() or set(derived_ids) != identifiers:
            errors.append("strategic-S candidate universe differs from candidates.csv")
        derived_names = dict(zip(derived_ids, derived.region_name.astype(str)))
        mismatched = [cid for cid in sorted(identifiers) if derived_names.get(cid) != names.get(cid)]
        if mismatched:
            errors.append(f"strategic-S region_name mismatch for {mismatched}")
    except Exception as exc:
        errors.append(f"strategic-S derivation failed: {type(exc).__name__}: {exc}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "candidate_count": len(identifiers),
        "production_score": config.production_score,
        "mapping_sensitivity_score": config.sensitivity_score,
        "errors": errors,
        "warnings": [],
    }
