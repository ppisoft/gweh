from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .yamlio import load_yaml_mapping


@dataclass(frozen=True)
class SourceArtifact:
    dataset_id: str
    artifact_id: str
    path: Path
    roles: tuple[str, ...]


def load_source_registry(path: Path) -> dict[str, dict[str, Any]]:
    data = load_yaml_mapping(path)
    sources = data.get("sources")
    if not isinstance(sources, dict):
        raise ValueError(f"Invalid source registry: {path}")
    return sources


def resolve_artifacts(registry: dict[str, dict[str, Any]], data_root: Path) -> list[SourceArtifact]:
    artifacts = []
    for dataset_id, metadata in registry.items():
        roles = tuple(metadata.get("roles", []))
        common = {
            "dataset_id": dataset_id,
            "roles": roles,
        }

        files = metadata.get("files", [])
        labels = metadata.get("artifact_ids", [f"file_{i:02d}" for i in range(1, len(files) + 1)])
        if len(files) != len(labels) or len(set(labels)) != len(labels):
            raise ValueError(f"{dataset_id}: artifact_ids must uniquely label all files")
        for artifact_id, raw_path in zip(labels, files):
            path = (data_root / str(raw_path)).resolve()
            artifacts.append(
                SourceArtifact(
                    artifact_id=str(artifact_id),
                    path=path,
                    **common,
                )
            )

    return artifacts


def artifact_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".shp":
        files = sorted(p for p in path.parent.glob(f"{path.stem}.*") if p.is_file())
        return files or [path]

    sidecars = [path]
    for suffix in (".aux.xml", ".ovr"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            sidecars.append(candidate)
    return sidecars
