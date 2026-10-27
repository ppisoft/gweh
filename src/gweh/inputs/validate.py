from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyogrio
import rasterio

from ..manifest import combined_sha256, sha256_file
from ..sources import SourceArtifact, artifact_files, load_source_registry, resolve_artifacts


def _relative(path, root):
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _validate_gwpt(path, metadata):
    schema = metadata.get("schema", {})
    xls = pd.ExcelFile(path)
    expected_sheets = set(schema.get("expected_sheets", []))
    missing_sheets = sorted(expected_sheets - set(xls.sheet_names))
    sheet = str(schema.get("primary_sheet", schema.get("sheet", "Data")))
    frame = pd.read_excel(path, sheet_name=sheet)
    required = set(schema.get("required_fields", []))
    missing_columns = sorted(required - set(frame.columns))

    phase = frame.get("GEM phase ID", pd.Series(dtype=object))
    capacity = pd.to_numeric(frame.get("Capacity (MW)"), errors="coerce")
    latitude = pd.to_numeric(frame.get("Latitude"), errors="coerce")
    longitude = pd.to_numeric(frame.get("Longitude"), errors="coerce")
    coordinates_ok = latitude.between(-90, 90, inclusive="both") & longitude.between(-180, 180, inclusive="both")

    return {
        "schema_ok": not missing_sheets and not missing_columns,
        "sheet_names": list(xls.sheet_names),
        "missing_sheets": missing_sheets,
        "missing_columns": missing_columns,
        "rows": int(len(frame)),
        "unique_phase_ids": int(phase.nunique(dropna=True)),
        "duplicate_phase_id_rows": int(phase.duplicated(keep=False).sum()),
        "invalid_coordinate_rows": int((~coordinates_ok).sum()),
        "capacity_total_mw": float(capacity.sum()),
    }


def _validate_vector(path, expected_crs):
    layers = pyogrio.list_layers(path)
    layer_names = [str(row[0]) for row in layers]
    layer = layer_names[0] if layer_names else None
    if layer is None:
        return {"schema_ok": False, "layers": [], "error": "no vector layers"}
    info = pyogrio.read_info(path, layer=layer)
    crs = str(info.get("crs") or "")
    fields = [str(field) for field in info.get("fields", [])]
    crs_ok = bool(crs) and (expected_crs is None or crs.upper() == expected_crs.upper())
    return {
        "schema_ok": crs_ok,
        "layers": layer_names,
        "crs": crs,
        "fields": fields,
        "feature_count": int(info.get("features", 0)),
    }


def _validate_raster(path):
    with rasterio.open(path) as dataset:
        transform = dataset.transform
        axis_aligned = abs(float(transform.b)) <= 1.0e-12 and abs(float(transform.d)) <= 1.0e-12
        return {
            "schema_ok": dataset.count >= 1 and dataset.crs is not None and axis_aligned,
            "driver": dataset.driver,
            "crs": dataset.crs.to_string() if dataset.crs else "",
            "width": int(dataset.width),
            "height": int(dataset.height),
            "count": int(dataset.count),
            "dtype": str(dataset.dtypes[0]) if dataset.dtypes else "",
            "nodata": dataset.nodata,
            "bounds": [float(value) for value in dataset.bounds],
            "axis_aligned_transform": bool(axis_aligned),
            "transform": [float(value) for value in tuple(transform)[:6]],
        }


def validate_dataset(dataset_id: str, metadata: dict[str, Any], artifacts: list[SourceArtifact]) -> dict[str, Any]:
    existing = [artifact.path for artifact in artifacts if artifact.dataset_id == dataset_id and artifact.path.exists()]
    if dataset_id == "GWPT" and existing:
        return _validate_gwpt(existing[0], metadata)
    if dataset_id in {"EEZ", "NaturalEarth", "NaturalEarth10m"} and existing:
        expected_crs = metadata.get("expected_crs")
        reports = {path.name: _validate_vector(path, str(expected_crs) if expected_crs else None) for path in existing}
        if dataset_id == "EEZ":
            required = set(metadata.get("schema", {}).get("polygon_required_fields", []))
            polygon = next((report for name, report in reports.items() if name == "eez_v12.gpkg"), None)
            if polygon is not None and not required.issubset(set(polygon.get("fields", []))):
                polygon["schema_ok"] = False
                polygon["missing_fields"] = sorted(required - set(polygon.get("fields", [])))
        return {"schema_ok": all(report["schema_ok"] for report in reports.values()), "artifacts": reports}
    if dataset_id == "GWA4" and existing:
        reports = {path.name: _validate_raster(path) for path in existing}
        return {"schema_ok": all(report["schema_ok"] for report in reports.values()), "artifacts": reports}
    return {"schema_ok": bool(existing), "note": "existence check only"}


def run_input_validation(
    *,
    data_root: Path,
    source_registry_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    registry = load_source_registry(source_registry_path)
    artifacts = resolve_artifacts(registry, data_root)
    raw_root = data_root / "raw"
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw input directory not found: {raw_root}")

    errors = []
    warnings = []
    selected_files = set()
    for artifact in artifacts:
        files = artifact_files(artifact.path)
        if not files:
            errors.append(f"{artifact.dataset_id}/{artifact.artifact_id}: missing {artifact.path}")
        selected_files.update(files)

    # Production hashes exactly the files declared by the production source registry.
    # External validation inputs are intentionally separate from production inputs.
    hash_cache = {}
    for path in sorted(selected_files):
        if path.name == ".DS_Store" or path.name.startswith("._"):
            continue
        hash_cache[path.resolve()] = sha256_file(path)

    source_rows = []
    for artifact in artifacts:
        files = artifact_files(artifact.path)
        group_items = [(_relative(path, data_root), hash_cache.get(path.resolve(), "")) for path in files]
        group_items_hashed = [(name, digest) for name, digest in group_items if digest]
        source_rows.append(
            {
                "dataset_id": artifact.dataset_id,
                "artifact_id": artifact.artifact_id,
                "roles": ";".join(artifact.roles),
                "relative_path": _relative(artifact.path, data_root),
                "exists": artifact.path.exists(),
                "size_bytes": int(artifact.path.stat().st_size) if artifact.path.exists() else 0,
                "sha256": hash_cache.get(artifact.path.resolve(), "") if artifact.path.exists() else "",
                "artifact_group_sha256": combined_sha256(group_items_hashed) if group_items_hashed else "",
                "artifact_group_files": ";".join(name for name, _ in group_items),
            }
        )

    schema_reports = {}
    for dataset_id, metadata in registry.items():
        dataset_artifacts = [artifact for artifact in artifacts if artifact.dataset_id == dataset_id]
        available = any(artifact.path.exists() for artifact in dataset_artifacts)
        try:
            report = validate_dataset(dataset_id, metadata, dataset_artifacts)
            report["available"] = available
        except Exception as exc:
            report = {"schema_ok": False, "available": available, "error": f"{type(exc).__name__}: {exc}"}
        schema_reports[dataset_id] = report
        if not report.get("schema_ok", False):
            errors.append(f"{dataset_id}: source schema or metadata check failed")

    pd.DataFrame(source_rows).to_csv(output_dir / "source_manifest.csv", index=False)

    qc = {
        "stage": "00_inputs",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_artifact_count": len(source_rows),
        "hashed_file_count": len(hash_cache),
        "schema": schema_reports,
        "warnings": warnings,
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }

    if errors:
        raise RuntimeError("Stage 00 input validation failed: " + "; ".join(errors))
    return qc
