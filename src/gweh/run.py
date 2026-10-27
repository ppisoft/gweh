from __future__ import annotations

from dataclasses import dataclass
import json
from contextlib import contextmanager
from pathlib import Path
import shutil
from typing import Iterator

from .paths import DataPaths


@dataclass(frozen=True)
class RunContext:
    data: DataPaths
    run_id: str

    @property
    def root(self) -> Path:
        return self.data.run(self.run_id)

    @property
    def snapshot(self) -> Path:
        return self.root / "config_snapshot"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest" / "run_manifest.json"

    def manifest(self) -> dict:
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"Run manifest not found: {self.manifest_path}")
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def config(self, name: str) -> Path:
        return self.snapshot / "config" / name

    def stage_dir(self, stage_name: str) -> Path:
        return self.root / stage_name

    def require_stage(self, stage_name: str) -> dict:
        path = self.stage_dir(stage_name) / "_stage.json"
        if not path.exists():
            raise RuntimeError(f"Required stage is not complete: {stage_name}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "PASS":
            raise RuntimeError(f"Required stage did not pass: {stage_name}")
        return payload

    @contextmanager
    def stage_workspace(self, stage_name: str) -> Iterator[Path]:
        final = self.stage_dir(stage_name)
        if final.exists() and any(final.iterdir()):
            raise FileExistsError(f"Stage output already exists: {final}")
        temp = self.root / f".{stage_name}.tmp"
        if temp.exists():
            shutil.rmtree(temp)
        temp.mkdir(parents=True)
        try:
            yield temp
        except Exception:
            failed = self.root / f"{stage_name}.failed"
            if failed.exists():
                shutil.rmtree(failed, ignore_errors=True)
            try:
                temp.replace(failed)
            except OSError:
                shutil.rmtree(temp, ignore_errors=True)
            raise
        else:
            if final.exists():
                final.rmdir()
            temp.replace(final)
