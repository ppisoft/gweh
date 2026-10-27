"""Add CF/ACDD metadata without altering any grid data values."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import xarray as xr
import yaml


def publication_attributes(run: Path, metadata_file: Path):
    values = yaml.safe_load(metadata_file.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict):
        raise ValueError("Publication metadata must be a YAML mapping")
    output = {}
    for key, value in values.items():
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"Publication attribute {key} must be text or null")
        output[key] = value.strip()
    output["date_created"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The source identifier is the originating Stage00-08 run, NOT the current code checkout.
    manifest = run / "manifest" / "run_manifest.json"
    if manifest.is_file():
        provenance = json.loads(manifest.read_text(encoding="utf-8"))
        if provenance.get("run_id"):
            output["gweh_source_run_id"] = str(provenance["run_id"])
        if provenance.get("source_code_sha256"):
            output["source_code_sha256"] = str(provenance["source_code_sha256"])
    geometry = run / "05_exclusive" / "exclusive_regions.parquet"
    if geometry.is_file():
        from gweh.manifest import sha256_file
        output["geometry_regions_sha256"] = sha256_file(geometry)
    output["history"] = (
        f"{output['date_created']}: Generated GWEH CF metadata for template grid-cell centre "
        "classification from Stage05 regional geometry and Stage08 catalogue."
    )
    output["Conventions"] = "CF-1.11"
    return output


def apply_metadata(dataset: xr.Dataset, *, run: Path, metadata_file: Path):
    """Update only descriptive global attributes of an existing classification."""
    attrs = publication_attributes(run, metadata_file)
    prior_history = str(dataset.attrs.get("history", "")).strip()
    if prior_history:
        attrs["history"] = prior_history + "\n" + attrs["history"]
    dataset.attrs.update(attrs)
    # Coordinate-axis names are CF attributes, not transformations of the
    # latitude/longitude coordinates. Apply them also during metadata refresh.
    for coordinate, axis in (("lat", "Y"), ("lon", "X")):
        if coordinate in dataset:
            dataset[coordinate].attrs["axis"] = axis
    return dataset


def refresh_file(source: Path, destination: Path, run: Path, metadata_file: Path):
    """Metadata-only copy of a previously checked scientific classification grid."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    with xr.open_dataset(source, decode_cf=False, mask_and_scale=False) as opened:
        data = opened.load()
    original = {name: data[name].values.copy() for name in data.variables}
    apply_metadata(data, run=run, metadata_file=metadata_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data.to_netcdf(destination, engine="scipy", format="NETCDF3_64BIT")
    with xr.open_dataset(destination, decode_cf=False, mask_and_scale=False) as written:
        import numpy as np
        for name in original:
            if name not in written or not np.array_equal(original[name], written[name], equal_nan=True):
                raise RuntimeError(f"CF metadata update altered scientific variable {name}")
    return destination
