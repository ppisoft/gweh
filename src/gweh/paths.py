from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class RepositoryPaths:
    root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> "RepositoryPaths":
        candidates = []
        if start is not None:
            candidates.append(start.resolve())
        else:
            env_root = os.environ.get("GWEH_REPO_ROOT")
            if env_root:
                candidates.append(Path(env_root).expanduser().resolve())
            candidates.append(Path.cwd().resolve())
            candidates.append(Path(__file__).resolve().parents[2])

        seen = set()
        for candidate in candidates:
            for path in (candidate, *candidate.parents):
                if path in seen:
                    continue
                seen.add(path)
                if (
                    (path / "src" / "gweh").is_dir()
                    and (path / "config").is_dir()
                    and (path / "evidence").is_dir()
                    and (path / "provenance").is_dir()
                ):
                    return cls(path)
        raise FileNotFoundError(
            "GWEH repository root not found; run from the repository, set GWEH_REPO_ROOT, or pass --repo"
        )

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def provenance(self) -> Path:
        return self.root / "provenance"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @classmethod
    def resolve(cls, value: str | Path | None = None) -> "DataPaths":
        raw = value or os.environ.get("GWEH_DATA_ROOT")
        if not raw:
            raise ValueError("Set GWEH_DATA_ROOT or pass --data-root")
        return cls(Path(raw).expanduser().resolve())

    def run(self, run_id: str) -> Path:
        return self.root / "runs" / run_id
