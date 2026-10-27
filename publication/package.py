#!/usr/bin/env python3
"""Package one tested software snapshot and one checked public dataset for deposit."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from datetime import date

import yaml

SOFTWARE_DIRS = (
    "config", "evidence", "grid", "src", "tools", "workflow", "validation",
    "omission", "provenance", "metadata", "tests", "verification", "reproducibility", "publication", "viewer",
)
SOFTWARE_FILES = (
    ".gitignore", "RUN.sh", "README.md", "DATASET_README.md", "DATA_RIGHTS.md",
    "DATA_LICENSE.md", "LICENSE", "CITATION.cff", "environment.yml", "CONTRIBUTING.md", "SIGNOFF.md",
)
EXCLUDE_NAMES = {
    "__pycache__", ".pytest_cache", ".git", ".DS_Store", "workflow.local.yaml",
    "PUBLICATION_TODO.md", "MANIFEST_SHA256.csv", ".coverage",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_public(public: Path) -> tuple[int, int]:
    """Verify the complete public result and scientific/validation content."""
    manifest = public / "MANIFEST_SHA256.csv"
    if not manifest.is_file():
        raise FileNotFoundError("Public product has no MANIFEST_SHA256.csv")
    with manifest.open(newline="", encoding="utf-8") as handle:
        records = list(csv.DictReader(handle))
    observed = {p.relative_to(public).as_posix() for p in public.rglob("*")
                if p.is_file() and p.name != "MANIFEST_SHA256.csv"}
    recorded = [r["file"] for r in records]
    if len(recorded) != len(set(recorded)) or set(recorded) != observed:
        raise ValueError("Public manifest is incomplete, duplicated or lists unexpected files")
    for row in records:
        relative = Path(row["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe path in public manifest")
        if sha256(public / relative) != row["sha256"]:
            raise ValueError(f"Public manifest checksum mismatch: {relative}")
    if "PREVIEW_NOT_FOR_DOI.md" in recorded:
        raise ValueError("The QA preview cannot be published; regenerate public from Stage05 and current CF metadata")
    if "validation/merra_provenance/blind/regions.gpkg" in recorded:
        raise ValueError("Redundant V4 blind GeoPackage in public dataset; use compact WKB fingerprints")
    if any(name.startswith("omission/") for name in recorded):
        raise ValueError("Public dataset contains obsolete historical omission outputs; "
                         "rebuild it with the current products exporter")
    if any(name.startswith("metadata/new_candidates/") for name in recorded):
        raise ValueError("Public dataset contains retired candidate-development tables")
    essentials = (
        "GWEH_catalogue.csv", "GWEH_scores.csv", "GWEH_region_metrics.csv",
        "GWEH_market_components.csv", "GWEH_geometries.gpkg",
        "GWEH_grid_region_lookup.csv", "metadata/candidate_nomination_register.csv",
        "metadata/publication.yaml", "metadata/data_dictionary.csv",
        "metadata/gwpt_source_overrides.csv",
        "metadata/strategic_s/strategic_assessment.csv",
        "metadata/strategic_s/strategic_score_rules.yaml",
        "DATA_RIGHTS.md", "DATA_LICENSE.md", "README.md", "CITATION.cff",
        "validation/methodology/merra_protocol.yaml",
        "validation/merra_provenance/blind/roster.csv",
        "validation/merra_provenance/blind/geometry_fingerprints.csv",
        "validation/merra_provenance/supervisor/mapping.csv",
        "validation/merra_provenance/annual/aggregation_context.json",
        "validation/VALIDATION_SCOPE.json", "validation/CF11_IOOS_CHECK.txt",
        "validation/CF_GRID_SHA256.txt",
        "validation/spatial/V1_V2_summary.json", "validation/market/V3_summary.json",
        "validation/merra/V4_summary.json",
    )
    for name in essentials:
        if not (public / name).is_file():
            raise FileNotFoundError(f"Dataset is not complete: {name}")
    grids = list(public.glob("GWEH_grid_*.nc"))
    if len(grids) != 1:
        raise ValueError("Dataset must contain exactly one GWEH grid NetCDF")
    if (public / "validation/CF_GRID_SHA256.txt").read_text().strip() != sha256(grids[0]):
        raise ValueError("CF report cannot be attributed to this exact NetCDF")
    if any(re.search(r"GWEH[0-9]+", name, flags=re.IGNORECASE)
           for name in recorded):
        raise ValueError("Public filenames still contain development labels")
    scope = json.loads((public / "validation/VALIDATION_SCOPE.json").read_text())
    if scope.get("status") != "COMPLETE_REPORT_SET_REQUIRES_SCIENTIFIC_REVIEW":
        raise ValueError("Dataset does not include a complete V1–V4 validation set")
    cf = (public / "validation/CF11_IOOS_CHECK.txt").read_text(encoding="utf-8")
    if "All tests passed!" not in cf or "Corrective Actions" in cf:
        raise ValueError("Attached CF report is not a clean IOOS CF PASS")
    protocol = yaml.safe_load((public / "validation/methodology/merra_protocol.yaml").read_text())
    period = protocol.get("period", {})
    if "blind_seed" not in protocol or not period.get("start") or not period.get("end"):
        raise ValueError("MERRA-2 public protocol lacks blind seed or time interval")
    years = range(date.fromisoformat(period["start"]).year,
                  date.fromisoformat(period["end"]).year + 1)
    expected_annual = {f"year_{y}.csv" for y in years}
    annual_dir = public / "validation/merra_provenance/annual"
    actual_annual = {p.name for p in annual_dir.glob("year_*.csv")}
    if not expected_annual or expected_annual != actual_annual:
        raise ValueError("MERRA-2 annual publication checkpoints do not cover the configured interval")
    context = json.loads((annual_dir / "aggregation_context.json").read_text(encoding="utf-8"))
    if not context.get("blind_geometry_sha256") or Path(str(context.get("merra_root", ""))).is_absolute():
        raise ValueError("MERRA-2 public context lacks geometry identity or exposes a private input path")
    from validation.geometry import verify_public_fingerprints
    verify_public_fingerprints(
        public / "GWEH_geometries.gpkg",
        public / "validation/merra_provenance/supervisor/mapping.csv",
        public / "validation/merra_provenance/blind/geometry_fingerprints.csv",
    )
    # Fail closed if any upstream country capacity values accidentally enter the release.
    for market_file in sorted((public / "validation/market").glob("V3_irena_gwpt_*.csv")):
        with market_file.open(encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
        if header != ["country_key", "surface", "gwpt_mw", "gwpt_rank", "irena_rank"]:
            raise ValueError(f"V3 public table must contain matched ranks only: {market_file}")
    citation = yaml.safe_load((public / "CITATION.cff").read_text(encoding="utf-8"))
    pub_meta = yaml.safe_load((public / "metadata/publication.yaml").read_text(encoding="utf-8"))
    if (citation.get("type") != "dataset" or citation.get("license") != "CC-BY-4.0"
            or citation.get("doi") != pub_meta.get("dataset_doi")):
        raise ValueError("Dataset CITATION.cff must match its DOI and CC-BY-4.0 licence")
    dictionary = list(csv.DictReader((public / "metadata/data_dictionary.csv").open(encoding="utf-8")))
    definitions = {(r["table"], r["field"]) for r in dictionary}
    with (public / "metadata/strategic_s/strategic_assessment.csv").open(encoding="utf-8", newline="") as stream:
        assessment_fields = next(csv.reader(stream))
    with (public / "metadata/gwpt_source_overrides.csv").open(encoding="utf-8", newline="") as stream:
        correction_fields = next(csv.reader(stream))
    correction_table = "metadata/gwpt_source_overrides.csv"
    if not correction_fields or any((correction_table, field) not in definitions
                                    for field in correction_fields):
        raise ValueError("Missing dictionary definitions for GWPT source overrides")
    strategic_table = "metadata/strategic_s/strategic_assessment.csv"
    if not assessment_fields or any((strategic_table, field) not in definitions for field in assessment_fields):
        raise ValueError("Missing dictionary definitions for public Strategic-S assessment")
    if ("metadata/strategic_s/strategic_score_rules.yaml", "*") not in definitions:
        raise ValueError("Missing dictionary definition for Strategic-S rule document")
    register = public / "metadata/source_register.csv"
    if register.is_file():
        with register.open(encoding="utf-8", newline="") as stream:
            fields = next(csv.reader(stream))
        if any(("metadata/source_register.csv", field) not in definitions for field in fields):
            raise ValueError("Missing dictionary definitions for source register")
    annual_schema = None
    for year_file in sorted(annual_dir.glob("year_*.csv")):
        with year_file.open(encoding="utf-8", newline="") as stream:
            actual_fields = next(csv.reader(stream))
        annual_table = "validation/merra_provenance/annual/year_YYYY.csv"
        if not actual_fields or any((annual_table, field) not in definitions for field in actual_fields):
            raise ValueError(f"Missing data dictionary definitions for {year_file.name}")
        if annual_schema is None:
            annual_schema = actual_fields
        elif actual_fields != annual_schema:
            raise ValueError("MERRA annual checkpoint CSV headers are inconsistent")
    blind_table = "validation/merra_provenance/blind/roster.csv"
    with (public / blind_table).open(encoding="utf-8", newline="") as stream:
        blind_fields = next(csv.reader(stream))
    if not blind_fields or any((blind_table, f) not in definitions for f in blind_fields):
        raise ValueError("Missing data dictionary definitions for the blind MERRA roster")
    return len(observed), len(grids)



def check_netcdf_release_identity(public: Path, expected_metadata: dict,
                                  run_manifest: dict, geometry_sha256: str) -> None:
    """Bind archived CF data to the public release and actual scientific run."""
    import xarray as xr

    grids = list(public.glob("GWEH_grid_*.nc"))
    if len(grids) != 1:
        raise ValueError("Expected exactly one release grid")
    with xr.open_dataset(grids[0], decode_cf=False, mask_and_scale=False) as nc:
        attrs = nc.attrs
    required = {
        "product_version": expected_metadata.get("product_version"),
        "id": expected_metadata.get("id"),
        "source_code_sha256": run_manifest.get("source_code_sha256"),
        "geometry_regions_sha256": geometry_sha256,
        **{key: expected_metadata.get(key) for key in
           ("publisher_name", "publisher_url", "dataset_doi", "software_doi")
           if expected_metadata.get(key)},
    }
    for field, expected in required.items():
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError(f"Publication metadata does not define {field}")
        if attrs.get(field) != expected:
            raise ValueError(
                f"NetCDF {field} is {attrs.get(field)!r}, expected {expected!r}; "
                "regenerate CF metadata, rerun IOOS and rebuild the validation archive"
            )


def archive_zip(target: Path, files: list[tuple[Path, str]]) -> None:
    """Create a stable ZIP; filesystem mtimes do not change archive contents."""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                         allowZip64=True) as out:
        for source, name in sorted(files, key=lambda item: item[1]):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if source.name == "RUN.sh" else 0o644) << 16
            with source.open("rb") as stream, out.open(info, "w", force_zip64=True) as member:
                shutil.copyfileobj(stream, member, 1024 * 1024)


def sources(repo: Path):
    files = []
    for file in SOFTWARE_FILES:
        if not (repo / file).is_file():
            raise FileNotFoundError(f"Missing software release file: {file}")
        files.append((repo / file, "GWEH/" + file))
    for directory in SOFTWARE_DIRS:
        for file in (repo / directory).rglob("*"):
            if not file.is_file():
                continue
            relative = file.relative_to(repo)
            if any(part in EXCLUDE_NAMES for part in relative.parts) or file.suffix in EXCLUDE_SUFFIXES:
                continue
            if file.name.startswith("._") or file.name.endswith("~"):
                continue
            files.append((file, "GWEH/" + relative.as_posix()))
    return files


def verify_viewer(viewer_dir: Path, public: Path, repo: Path) -> dict:
    """Verify the optional browser and its source/data hash provenance."""
    viewer_dir, public, repo = map(Path, (viewer_dir, public, repo))
    html_path = viewer_dir / "GWEH_explorer.html"
    record_path = viewer_dir / "VIEWER_BUILD.json"
    if not html_path.is_file() or not record_path.is_file():
        raise FileNotFoundError("Viewer must contain GWEH_explorer.html and VIEWER_BUILD.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if (record.get("status") != "PASS" or record.get("viewer_sha256") != sha256(html_path)
            or record.get("viewer_size_bytes") != html_path.stat().st_size):
        raise ValueError("Viewer content does not match its build record")
    source_names = {
        "template_sha256": repo / "viewer/dashboard_template.html",
        "generator_sha256": repo / "viewer/build_viewer.py",
        "background_sha256": repo / "viewer/assets/natural_earth_110m_countries.geojson",
    }
    for field, path in source_names.items():
        if record.get(field) != sha256(path):
            raise ValueError(f"Viewer source snapshot mismatch: {field}")
    manifest = {
        row["file"]: row["sha256"]
        for row in csv.DictReader((public / "MANIFEST_SHA256.csv").open(encoding="utf-8", newline=""))
    }
    # The document and build record must identify the current dataset version.
    meta = yaml.safe_load((public / "metadata/publication.yaml").read_text(encoding="utf-8"))
    if record.get("dataset_version") != str(meta.get("product_version")):
        raise ValueError("Viewer dataset version does not match public release metadata")
    for name, expected in record.get("input_sha256", {}).items():
        if name not in manifest or manifest[name] != expected:
            raise ValueError(f"Viewer is derived from outdated or different public data: {name}")
    if not record.get("input_sha256"):
        raise ValueError("Viewer build record does not identify source data")
    return record


def package(repo: Path, public: Path, run: Path, output: Path,
            viewer_dir: Path | None = None) -> Path:
    """Write a new DOI-draft directory; never edit inputs or publish remotely."""
    repo, public, run, output = (Path(x).resolve() for x in (repo, public, run, output))
    if output.exists():
        raise FileExistsError("Choose a new deposit output directory: " + str(output))
    check_public(public)
    metadata = yaml.safe_load((repo / "metadata/publication.yaml").read_text())
    version = metadata.get("product_version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Publication product_version must be a release triplet, e.g. 1.0.0")
    for label in ("publisher_name", "publisher_url", "dataset_doi", "software_doi"):
        if not isinstance(metadata.get(label), str) or not metadata[label].strip():
            raise ValueError(f"Release metadata require {label} before packaging")
    for label in ("dataset_doi", "software_doi"):
        if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", metadata[label]):
            raise ValueError(f"Invalid reserved DOI: {label}")
    from gweh.manifest import SNAPSHOT_INPUTS, combined_sha256, sha256_file
    # Dataset and code must carry the exact same public release identifiers.
    deposited = yaml.safe_load((public / "metadata/publication.yaml").read_text(encoding="utf-8"))
    if deposited != metadata:
        raise ValueError("Dataset publication metadata differ from this software checkout")
    citation = yaml.safe_load((repo / "CITATION.cff").read_text(encoding="utf-8"))
    if citation.get("doi") != metadata.get("software_doi") or citation.get("license") != "MIT":
        raise ValueError("CITATION.cff must match the software DOI and MIT licence")
    run_manifest = json.loads((run / "manifest/run_manifest.json").read_text())
    actual_hash = combined_sha256(
        (p.relative_to(repo).as_posix(), sha256_file(p))
        for p in sorted((repo / "src").rglob("*.py"))
    )
    if actual_hash != run_manifest.get("source_code_sha256"):
        raise ValueError("Software science core differs from the code used for the data run")
    for relative in SNAPSHOT_INPUTS:
        if sha256_file(repo / relative) != run_manifest.get("snapshot_sha256", {}).get(relative):
            raise ValueError(f"Software scientific input differs from the data run: {relative}")
    check_netcdf_release_identity(
        public, metadata, run_manifest,
        sha256_file(run / "05_exclusive/exclusive_regions.parquet"),
    )
    if viewer_dir is not None:
        verify_viewer(viewer_dir, public, repo)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_release_", dir=output.parent) as temp:
        temp_dir = Path(temp)
        software = temp_dir / f"GWEH_v{version}_software.zip"
        dataset = temp_dir / f"GWEH_v{version}_dataset.zip"
        archive_zip(software, sources(repo))
        archive_zip(dataset, [
            (file, "GWEH_dataset/" + file.relative_to(public).as_posix())
            for file in public.rglob("*") if file.is_file()
        ])
        shutil.copy2(public / "README.md", temp_dir / "README_DATA.md")
        if viewer_dir is not None:
            shutil.copy2(viewer_dir / "GWEH_explorer.html", temp_dir / "GWEH_explorer.html")
            shutil.copy2(viewer_dir / "VIEWER_BUILD.json", temp_dir / "VIEWER_BUILD.json")
            shutil.copy2(repo / "viewer/README.md", temp_dir / "VIEWER_README.md")
        with (temp_dir / "SHA256SUMS.txt").open("w", encoding="utf-8") as stream:
            deposit_files = [software, dataset, temp_dir / "README_DATA.md"]
            if viewer_dir is not None:
                deposit_files.extend([temp_dir / "GWEH_explorer.html",
                                      temp_dir / "VIEWER_BUILD.json",
                                      temp_dir / "VIEWER_README.md"])
            for file in sorted(deposit_files):
                stream.write(f"{sha256(file)}  {file.name}\n")
        temp_dir.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--public-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--viewer-dir", type=Path, help="Verified optional companion viewer directory")
    args = parser.parse_args()
    print(f"[done] publication draft files: {package(args.repo, args.public_dir, args.run_dir, args.output, args.viewer_dir)}")


if __name__ == "__main__":
    main()
