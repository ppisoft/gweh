#!/usr/bin/env python3
"""Export the selected scientific products of a completed Stage00-08 run."""
from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
import shapely
from shapely.geometry import MultiPolygon, Polygon


def _required_run_files(run: Path) -> dict[str, Path]:
    files = {
        "catalogue": run / "08_catalogue/catalogue.csv",
        "metrics": run / "06_metrics/region_metrics.csv",
        "market": run / "06_metrics/market_components.csv",
        "scores": run / "07_scores/scores.csv",
        "regions": run / "05_exclusive/exclusive_regions.parquet",
        "denominators": run / "05_exclusive/exclusive_denominators.parquet",
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"Missing {name}: {path}")
    return files


def _check_geometry(frame: gpd.GeoDataFrame, candidate_ids: set[str], label: str):
    if "candidate_id" not in frame:
        raise ValueError(f"{label}: missing candidate_id")
    if frame.candidate_id.astype(str).duplicated().any():
        raise ValueError(f"{label}: duplicate candidate_id")
    if set(frame.candidate_id.astype(str)) != candidate_ids:
        raise ValueError(f"{label}: candidate IDs differ from catalogue")
    if frame.crs is None or frame.crs.to_epsg() != 4326:
        raise ValueError(f"{label}: expected native EPSG:4326 geometry")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError(f"{label}: empty or missing geometry")
    if not frame.geometry.is_valid.all():
        raise ValueError(f"{label}: invalid geometry; repair must happen upstream")
    if not frame.geom_type.isin(("Polygon", "MultiPolygon")).all():
        raise ValueError(f"{label}: non-polygonal geometry")


def _public_polygon_layer(source: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Build a uniform MULTIPOLYGON layer without altering any source vertices.

    Stage05 GeoParquet legitimately mixes Polygon and MultiPolygon values.
    GeoPackage stores one declared geometry type per layer. Normalize this
    *before* serialization instead of depending on implicit OGR promotion.
    """
    result = source.copy()
    result.geometry = gpd.GeoSeries(
        [MultiPolygon([geom]) if isinstance(geom, Polygon) else geom
         for geom in source.geometry],
        index=source.index,
        crs=source.crs,
    )
    return result


def _verify_geopackage_layer(expected: gpd.GeoDataFrame,
                             actual: gpd.GeoDataFrame, label: str) -> None:
    """Check an exported MULTIPOLYGON layer with exactly preserved coordinates.

    Ring starts and polygon part ordering are normalized for comparison;
    neither coordinates nor ring topology are snapped or rounded.
    """
    for frame in (expected, actual):
        if "candidate_id" not in frame.columns or frame.candidate_id.duplicated().any():
            raise RuntimeError(f"GeoPackage {label}: missing or duplicate candidate IDs")
        if frame.crs is None or frame.crs.to_epsg() != 4326:
            raise RuntimeError(f"GeoPackage {label}: CRS is not EPSG:4326")
        if not frame.geom_type.eq("MultiPolygon").all():
            raise RuntimeError(f"GeoPackage {label}: expected exclusively MultiPolygon geometry")
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            raise RuntimeError(f"GeoPackage {label}: missing or empty geometry")
        # Geometric validity was checked on the Stage05 input. Exact 0-tolerance
        # coordinate identity below also guards against new invalid vertices;
        # repeating GEOS validity on very large exported multipart coastlines is
        # expensive and adds no independent information here.
    # Preserve the published non-geometric layer attributes as well as IDs.
    attributes = [column for column in expected.columns if column != expected.geometry.name]
    output_attributes = [column for column in actual.columns if column != actual.geometry.name]
    if set(attributes) != set(output_attributes):
        raise RuntimeError(f"GeoPackage {label}: attribute columns changed")
    original_ids = set(expected.candidate_id)
    exported_ids = set(actual.candidate_id)
    if original_ids != exported_ids or len(expected) != len(actual):
        raise RuntimeError(f"GeoPackage {label}: candidate IDs changed")
    source = expected.set_index("candidate_id")
    written = actual.set_index("candidate_id")
    identifiers = sorted(original_ids)
    try:
        pd.testing.assert_frame_equal(
            source.loc[identifiers, [c for c in attributes if c != "candidate_id"]],
            written.loc[identifiers, [c for c in attributes if c != "candidate_id"]],
            check_dtype=False,
        )
    except AssertionError as exc:
        raise RuntimeError(f"GeoPackage {label}: published attributes changed") from exc
    same = shapely.equals_exact(
        shapely.normalize(source.loc[identifiers].geometry.array),
        shapely.normalize(written.loc[identifiers].geometry.array),
        tolerance=0.0,
    )
    if not same.all():
        mismatched = [candidate for candidate, valid in zip(identifiers, same) if not valid]
        raise RuntimeError(
            f"GeoPackage {label}: vertex coordinates changed for "
            f"{len(mismatched)} candidate(s), e.g. {mismatched[:10]}"
        )


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _write_manifest(directory: Path):
    with (directory / "MANIFEST_SHA256.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["file", "sha256"])
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.name != "MANIFEST_SHA256.csv":
                writer.writerow([path.relative_to(directory).as_posix(), _sha256(path)])


def _optional_grid(grid_path: Path | None, lookup_path: Path | None, stage: Path):
    if grid_path is None and lookup_path is None:
        return
    if grid_path is None or lookup_path is None:
        raise ValueError("Both --cf-grid and --cf-lookup must be supplied")
    with xr.open_dataset(grid_path) as grid:
        if grid.attrs.get("Conventions") != "CF-1.11":
            raise ValueError("The supplied grid is not declared CF-1.11")
        if not grid.sizes.get("lat") or not grid.sizes.get("lon"):
            raise ValueError("CF grid must have latitude and longitude coordinates")
        if "gweh_region_id" not in grid:
            raise ValueError("CF grid lacks GWEH region IDs")
    shutil.copy2(grid_path, stage / grid_path.name)
    shutil.copy2(lookup_path, stage / "GWEH_grid_region_lookup.csv")


def export_products(
    run: Path, output: Path, repo: Path, *,
    cf_grid=None, cf_lookup=None, climate_roster=None, validation_dir=None,
):
    """Write a compact public candidate to a new directory; never modify the run."""
    run = Path(run).resolve()
    output = Path(output).resolve()
    repo = Path(repo).resolve()
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}")
    required = _required_run_files(run)
    catalogue = pd.read_csv(required["catalogue"], dtype={"candidate_id": str})
    if catalogue.candidate_id.duplicated().any():
        raise ValueError("Duplicate candidate_id in catalogue")
    identifiers = set(catalogue.candidate_id)
    # Provenance is a publication input, not a numerical stage dependency.
    # Verify that every actual candidate has traceable nomination evidence.
    from nomination import check_provenance
    check_provenance(repo, identifiers)
    if climate_roster is not None:
        roster = pd.read_csv(climate_roster, dtype={"candidate_id": str})
        if roster.candidate_id.duplicated().any() or set(roster.candidate_id) != identifiers:
            raise ValueError("Climate roster candidate IDs differ from science catalogue")
        catalogue = catalogue.merge(roster, on="candidate_id", validate="one_to_one")
    for table_name in ("metrics", "scores"):
        table = pd.read_csv(required[table_name], dtype={"candidate_id": str})
        if table.candidate_id.duplicated().any() or set(table.candidate_id) != identifiers:
            raise ValueError(f"{table_name}: candidate IDs differ from catalogue")

    regions = gpd.read_parquet(required["regions"])
    denominators = gpd.read_parquet(required["denominators"])
    for name, frame in (("regions", regions), ("denominators", denominators)):
        _check_geometry(frame, identifiers, name)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_products_", dir=output.parent) as temporary:
        stage = Path(temporary)
        table_files = (
            ("catalogue", "GWEH_catalogue.csv"),
            ("metrics", "GWEH_region_metrics.csv"),
            ("scores", "GWEH_scores.csv"),
            ("market", "GWEH_market_components.csv"),
        )
        for key, filename in table_files:
            if key == "catalogue" and climate_roster is not None:
                catalogue.to_csv(stage / filename, index=False)
            else:
                shutil.copy2(required[key], stage / filename)
        # The publication schema declares MULTIPOLYGON for both layers.
        # Adapt Stage05 Polygon values explicitly, before calling the driver.
        prepared_layers = {
            "regions": _public_polygon_layer(regions),
            "denominators": _public_polygon_layer(denominators),
        }
        geometry_path = stage / "GWEH_geometries.gpkg"
        for layer, frame in prepared_layers.items():
            frame.to_file(geometry_path, layer=layer, driver="GPKG",
                          mode="a" if geometry_path.exists() else "w", index=False)
        for layer, frame in prepared_layers.items():
            _verify_geopackage_layer(frame, gpd.read_file(geometry_path, layer=layer), layer)

        if climate_roster is not None and cf_grid is not None:
            # The climate mapping must name exactly those eligible scientific
            # candidates. Never attach a grid from an unrelated candidate panel.
            lookup = pd.read_csv(cf_lookup, dtype={"candidate_id": str})
            eligible = set(roster.loc[roster.climate_analysis_eligible.eq(1), "candidate_id"])
            if (lookup.grid_id.duplicated().any() or lookup.candidate_id.duplicated().any()
                    or set(lookup.candidate_id) != eligible):
                raise ValueError("Grid lookup and climate-eligible candidate population disagree")
            with xr.open_dataset(cf_grid, decode_cf=False) as grid_data:
                ids = grid_data["gweh_region_id"].values.astype(int).ravel()
            count = np.bincount(ids, minlength=int(lookup.grid_id.max()) + 1)
            if any(count[int(row.grid_id)] != int(row.cell_count)
                   for row in lookup.itertuples(index=False)):
                raise ValueError("CF mask and grid lookup cell counts disagree")
        _optional_grid(cf_grid, cf_lookup, stage)
        metadata = stage / "metadata"
        metadata.mkdir()
        for name in ("source_register.csv", "candidate_nomination_register.csv"):
            shutil.copy2(repo / "provenance" / name, metadata / name)
        strategic = metadata / "strategic_s"
        strategic.mkdir()
        sources = pd.read_csv(repo / "evidence/strategic_s/source_database.csv", dtype=str)
        links = pd.read_csv(repo / "evidence/strategic_s/candidate_source_evidence.csv", dtype=str)
        if sources.source_id.isna().any() or sources.source_id.duplicated().any():
            raise ValueError("Strategic-S bibliography source IDs are not unique")
        if set(links.candidate_id) - identifiers or set(links.source_id) - set(sources.source_id):
            raise ValueError("Strategic-S evidence has unresolved candidate/source references")
        for name in ("source_database.csv", "candidate_source_evidence.csv", "strategic_assessment.csv", "strategic_score_rules.yaml"):
            shutil.copy2(repo / "evidence/strategic_s" / name, strategic / name)
        shutil.copy2(repo / "config/gwpt_source_overrides.csv",
                     metadata / "gwpt_source_overrides.csv")
        shutil.copy2(repo / "publication/dataset_docs/NOMINATION_METHODS.md", metadata / "NOMINATION_METHODS.md")
        shutil.copy2(repo / "metadata/data_dictionary.csv", metadata / "data_dictionary.csv")
        shutil.copy2(repo / "metadata/publication.yaml", metadata / "publication.yaml")
        shutil.copy2(repo / "DATASET_README.md", stage / "README.md")
        shutil.copy2(repo / "publication/dataset_docs/CITATION.cff", stage / "CITATION.cff")
        shutil.copy2(repo / "publication/dataset_docs/DATA_RIGHTS.md", stage / "DATA_RIGHTS.md")
        shutil.copy2(repo / "DATA_LICENSE.md", stage / "DATA_LICENSE.md")
        if validation_dir is not None:
            evidence = Path(validation_dir)
            scope = evidence / "VALIDATION_SCOPE.json"
            if not scope.is_file():
                raise FileNotFoundError(f"External validation scope is required: {scope}")
            import json
            doc = json.loads(scope.read_text(encoding="utf-8"))
            if doc.get("status") != "COMPLETE_REPORT_SET_REQUIRES_SCIENTIFIC_REVIEW":
                raise ValueError("Attach only a complete V1–V4 report set; scientific review remains separate")
            from validation.prepare import check_merra_protocol, check_public_merra_context
            from validation.geometry import verify_public_fingerprints
            check_merra_protocol(evidence, source=repo / "validation/config/merra.yaml")
            check_public_merra_context(evidence)
            verify_public_fingerprints(
                geometry_path,
                evidence / "merra_provenance/supervisor/mapping.csv",
                evidence / "merra_provenance/blind/geometry_fingerprints.csv",
            )
            if cf_grid is not None:
                signature = evidence / "CF_GRID_SHA256.txt"
                if not signature.is_file() or signature.read_text().strip() != _sha256(Path(cf_grid)):
                    raise ValueError("CF validation was not performed on this exact grid")
            # Only known summary artefacts may enter the DOI-facing dataset.
            allowed = {
                "VALIDATION_SCOPE.json", "CF11_IOOS_CHECK.txt", "CF_GRID_SHA256.txt",
                "spatial/V1_V2_summary.json", "spatial/V1_V2_candidate_containment.csv",
                "market/V3_summary.json",
                "merra/V4_summary.json",
                "merra/V4_merra_candidate_values.csv",
                "merra/V4_merra_yearly.csv",
                "methodology/VALIDATION_METHODS.md",
                "methodology/external_sources.yaml",
                "methodology/merra_protocol.yaml",
                "merra_provenance/README.json",
                "merra_provenance/supervisor/mapping.csv",
                "merra_provenance/blind/roster.csv",
                "merra_provenance/blind/geometry_fingerprints.csv",
                "merra_provenance/annual/aggregation_context.json",
            }
            relative_files = {p.relative_to(evidence).as_posix() for p in evidence.rglob("*") if p.is_file()}
            market_year_files = {s for s in relative_files if s.startswith("market/V3_irena_gwpt_")
                                 and s.endswith(".csv") and "/" not in s[len("market/"): ]}
            merra_year_files = {s for s in relative_files
                                if s.startswith("merra_provenance/annual/year_")
                                and s.endswith(".csv")
                                and "/" not in s[len("merra_provenance/annual/"):]}
            unexpected = relative_files - allowed - market_year_files - merra_year_files
            if unexpected:
                raise ValueError(f"Unexpected file in validation package: {sorted(unexpected)}")
            for relative in sorted(relative_files):
                dest = stage / "validation" / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(evidence / relative, dest)
            # Publication prose is maintained in one repo template; a completed
            # validation archive may predate documentation-only corrections.
            shutil.copy2(repo / "publication/dataset_docs/VALIDATION_METHODS.md",
                         stage / "validation/methodology/VALIDATION_METHODS.md")
        _write_manifest(stage)
        stage.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cf-grid", type=Path)
    parser.add_argument("--cf-lookup", type=Path)
    parser.add_argument("--climate-roster", type=Path)
    parser.add_argument("--validation-dir", type=Path,
                        help="Optional compact external evidence from validation/prepare.py")
    args = parser.parse_args()
    output = export_products(
        args.run_dir, args.output, args.repo,
        cf_grid=args.cf_grid, cf_lookup=args.cf_lookup, climate_roster=args.climate_roster,
        validation_dir=args.validation_dir,
    )
    print(f"[done] public product candidate: {output}")


if __name__ == "__main__":
    main()
