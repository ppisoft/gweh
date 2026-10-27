from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Iterable, Mapping

from .paths import DataPaths, RepositoryPaths
from .yamlio import load_yaml_mapping


SNAPSHOT_INPUTS = (
    "config/boundary.yaml",
    "config/candidates.csv",
    "config/domains.yaml",
    "config/geometry.yaml",
    "config/geometry_anchor_exclusions.csv",
    "config/gwpt_source_overrides.csv",
    "config/partition.yaml",
    "config/scoring.yaml",
    "config/selection.yaml",
    "config/sources.yaml",
    "config/strategic_s.yaml",
    "config/surface_policy.yaml",
    "config/surface_source_corrections.csv",
    "evidence/strategic_s/strategic_assessment.csv",
    "evidence/strategic_s/strategic_score_rules.yaml",
)
RUN_DIRS = (
    "manifest",
    "config_snapshot",
    "00_inputs",
    "01_ingest",
    "02_geography",
    "03_candidates",
    "04_delineation",
    "05_exclusive",
    "06_metrics",
    "07_scores",
    "08_catalogue",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, block_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def combined_sha256(items: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(items):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot_files(repo: RepositoryPaths, target: Path) -> dict[str, str]:
    hashes = {}
    for relative in SNAPSHOT_INPUTS:
        source = repo.root / relative
        if not source.is_file():
            raise FileNotFoundError(f"Missing input for run snapshot: {source}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        hashes[relative] = sha256_file(destination)
    return hashes


def init_run(
    repo: RepositoryPaths,
    data: DataPaths,
    run_id: str,
) -> Path:
    run_dir = data.run(run_id)
    if run_dir.exists():
        raise FileExistsError(f"Run already exists: {run_dir}")
    for name in RUN_DIRS:
        (run_dir / name).mkdir(parents=True, exist_ok=True)

    boundary = load_yaml_mapping(repo.config / "boundary.yaml")
    selected_boundary = str(boundary["name"])
    hashes = snapshot_files(repo, run_dir / "config_snapshot")
    payload = {
        "product": "GWEH",
        "source_code_sha256": combined_sha256(
            (p.relative_to(repo.root).as_posix(), sha256_file(p))
            for p in sorted((repo.root / "src").rglob("*.py"))
        ),
        "run_id": run_id,
        "boundary_profile": selected_boundary,
        "created_utc": utc_now(),
        "repository_root": str(repo.root),
        "data_root": str(data.root),
        "snapshot_sha256": hashes,
        "stages": {},
    }
    output = run_dir / "manifest" / "run_manifest.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_dir


def update_run_stage(run_manifest: Path, stage: str, stage_manifest: Mapping[str, object]) -> None:
    payload = json.loads(run_manifest.read_text(encoding="utf-8"))
    payload.setdefault("stages", {})[stage] = {
        "status": stage_manifest.get("status"),
        "completed_utc": stage_manifest.get("completed_utc"),
        "manifest": f"{stage}/_stage.json",
    }
    run_manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_stage_manifest(
    path: Path,
    *,
    stage: str,
    run_id: str,
    started_utc: str,
    completed_utc: str,
    config_keys_consumed: Iterable[str],
    notes: Mapping[str, object],
) -> dict[str, object]:
    """Record stage status plus the small provenance needed to reproduce it."""
    payload = {
        "stage": stage,
        "run_id": run_id,
        "started_utc": started_utc,
        "completed_utc": completed_utc,
        "status": "PASS",
        "config_keys_consumed": list(config_keys_consumed),
        "notes": dict(notes),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload

