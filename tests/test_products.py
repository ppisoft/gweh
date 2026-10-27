"""Public export keeps both scientific polygon layers and all candidates."""
import csv
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box, MultiPolygon, Polygon

from products import export_products, _public_polygon_layer, _verify_geopackage_layer

ROOT = Path(__file__).resolve().parents[1]


def test_public_products_from_completed_science(monkeypatch, tmp_path):
    run = tmp_path / "run"
    sources = {
        "08_catalogue/catalogue.csv": ROOT / "tests/fixtures/08_catalogue/catalogue.csv",
        "07_scores/scores.csv": ROOT / "tests/fixtures/07_scores/scores.csv",
        "06_metrics/region_metrics.csv": ROOT / "tests/fixtures/06_metrics/region_metrics.csv",
        "06_metrics/market_components.csv": ROOT / "tests/fixtures/06_metrics/market_components.csv",
    }
    for rel, source in sources.items():
        destination = run / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    for name in ("exclusive_regions", "exclusive_denominators"):
        (run / "05_exclusive").mkdir(exist_ok=True)
        (run / "05_exclusive" / f"{name}.parquet").write_text("synthetic parcel")

    catalogue = pd.read_csv(sources["08_catalogue/catalogue.csv"])
    identifiers = catalogue.candidate_id.tolist()
    polygons = [box(i * 3.0, 0.0, i * 3.0 + 1.0, 1.0) for i in range(len(identifiers))]
    # The exporter must not alter native geometry or drop non-selected candidates.
    frame = gpd.GeoDataFrame({"candidate_id": identifiers}, geometry=polygons, crs="EPSG:4326")
    monkeypatch.setattr(gpd, "read_parquet", lambda _p: frame.copy())

    output = tmp_path / "public"
    roster = tmp_path / "grid_roster.csv"
    pd.DataFrame({
        "candidate_id": identifiers,
        "climate_grid_center_count": [15] * len(identifiers),
        "climate_analysis_eligible": [1] * len(identifiers),
        "primary_climate_panel": [1] * len(identifiers),
        "climate_use_tier": ["PRIMARY_CLIMATE_N_GE12"] * len(identifiers),
    }).to_csv(roster, index=False)
    export_products(run, output, ROOT, climate_roster=roster)
    assert output.is_dir()
    assert (output / "DATA_LICENSE.md").is_file()
    assert (output / "metadata/strategic_s/source_database.csv").is_file()
    assert (output / "metadata/strategic_s/candidate_source_evidence.csv").is_file()
    assert (output / "metadata/candidate_nomination_register.csv").is_file()
    assert (output / "metadata/NOMINATION_METHODS.md").is_file()
    assert (output / "metadata/publication.yaml").is_file()
    assert "metadata/" in (output / "DATA_RIGHTS.md").read_text()
    assert "`metadata/strategic_s/" in (output / "metadata/NOMINATION_METHODS.md").read_text()
    assert not (output / "metadata/nomination_source_catalogue.csv").exists()
    assert (output / "metadata/gwpt_source_overrides.csv").is_file()
    assert not (output / "metadata/new_candidates").exists()

    public_catalogue = pd.read_csv(output / "GWEH_catalogue.csv")
    assert len(public_catalogue) == len(catalogue)
    assert "climate_analysis_eligible" in public_catalogue.columns
    assert set(gpd.list_layers(output / "GWEH_geometries.gpkg").name) == {"regions", "denominators"}
    assert len(gpd.read_file(output / "GWEH_geometries.gpkg", layer="regions")) == len(catalogue)
    assert len(gpd.read_file(output / "GWEH_geometries.gpkg", layer="denominators")) == len(catalogue)
    for layer in ("regions", "denominators"):
        exported = gpd.read_file(output / "GWEH_geometries.gpkg", layer=layer)
        assert exported.geom_type.eq("MultiPolygon").all()
        assert exported.crs.to_epsg() == 4326
    with (output / "MANIFEST_SHA256.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    names = {p.relative_to(output).as_posix() for p in output.rglob("*")
             if p.is_file() and p.name != "MANIFEST_SHA256.csv"}
    assert {row["file"] for row in rows} == names
    assert not (run / "05_exclusive" / "GWEH_geometries.gpkg").exists()

    # The public product must accept the freshly aggregated V4 filenames.
    evidence = tmp_path / "validation"
    files = ["VALIDATION_SCOPE.json", "CF11_IOOS_CHECK.txt",
             "spatial/V1_V2_summary.json", "spatial/V1_V2_candidate_containment.csv",
             "market/V3_summary.json", "market/V3_irena_gwpt_2025.csv",
             "merra/V4_summary.json", "merra/V4_merra_candidate_values.csv",
             "merra/V4_merra_yearly.csv",
             "methodology/VALIDATION_METHODS.md",
             "methodology/external_sources.yaml",
             "methodology/merra_protocol.yaml",
             "merra_provenance/README.json",
             "merra_provenance/blind/geometry_fingerprints.csv",
             "merra_provenance/blind/roster.csv",
             "merra_provenance/annual/aggregation_context.json",
             "merra_provenance/annual/year_2008.csv",
             "merra_provenance/supervisor/mapping.csv"]
    for name in files:
        file = evidence / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('test fixture')
    # The compact evidence must match the exported MultiPolygon WKB, one-to-one.
    import hashlib
    mapping = pd.DataFrame({"candidate_id": identifiers,
                            "validation_id": [f"MV-{i:05d}" for i in range(1, len(identifiers) + 1)]})
    mapping.to_csv(evidence / "merra_provenance/supervisor/mapping.csv", index=False)
    pd.DataFrame({"validation_id": mapping.validation_id}).to_csv(
        evidence / "merra_provenance/blind/roster.csv", index=False)
    published_shapes = _public_polygon_layer(frame).set_index("candidate_id")
    pd.DataFrame({"validation_id": mapping.validation_id,
                  "wkb_sha256": [hashlib.sha256(published_shapes.loc[cid].geometry.wkb).hexdigest()
                                 for cid in identifiers]}).to_csv(
        evidence / "merra_provenance/blind/geometry_fingerprints.csv", index=False)
    (evidence / "VALIDATION_SCOPE.json").write_text(
        '{"status": "COMPLETE_REPORT_SET_REQUIRES_SCIENTIFIC_REVIEW"}'
    )
    (evidence / "methodology/merra_protocol.yaml").write_bytes(
        (ROOT / "validation/config/merra.yaml").read_bytes()
    )
    (evidence / "merra_provenance/annual/aggregation_context.json").write_text(
        '{"blind_geometry_sha256":"abc123","merra_root":"<external MERRA-2 input directory>"}'
    )
    with_validation = tmp_path / "public_with_validation"
    export_products(run, with_validation, ROOT, climate_roster=roster,
                    validation_dir=evidence)
    assert (with_validation / "CITATION.cff").is_file()
    assert (with_validation / "validation/methodology/VALIDATION_METHODS.md").read_bytes() == (ROOT / "publication/dataset_docs/VALIDATION_METHODS.md").read_bytes()
    assert not (with_validation / "omission").exists()
    assert (with_validation / "validation/merra/V4_merra_candidate_values.csv").is_file()
    assert (with_validation / "validation/merra_provenance/annual/year_2008.csv").is_file()
    assert (with_validation / "validation/merra_provenance/blind/geometry_fingerprints.csv").is_file()
    assert not (with_validation / "validation/merra_provenance/blind/regions.gpkg").exists()


def test_strategic_source_registers_referential_integrity():
    root = Path(__file__).resolve().parents[1]
    sources = pd.read_csv(root / "evidence/strategic_s/source_database.csv", dtype=str)
    links = pd.read_csv(root / "evidence/strategic_s/candidate_source_evidence.csv", dtype=str)
    candidates = set(pd.read_csv(root / "config/candidates.csv").candidate_id)
    assert sources.source_id.is_unique
    assert set(links.source_id) <= set(sources.source_id)
    assert set(links.candidate_id) <= candidates


def test_native_polygon_multipolygon_contract_is_exact(tmp_path):
    """Both real GPKG layers must preserve every vertex, including holes."""
    frame = gpd.GeoDataFrame(
        {"candidate_id": ["single", "multi", "with_hole"]},
        geometry=[
            box(0, 0, 1, 1),
            MultiPolygon([box(3, 0, 4, 1), box(5, 0, 6, 1)]),
            Polygon([(7, 0), (9, 0), (9, 2), (7, 2)],
                    [[(7.5, 0.5), (8, 0.5), (8, 1), (7.5, 1)]]),
        ],
        crs="EPSG:4326",
    )
    native_bytes = [geom.wkb for geom in frame.geometry]
    frame_types = frame.geom_type.tolist()
    prepared = _public_polygon_layer(frame)
    # The reference Stage05 GeoDataFrame must not be mutated in memory.
    assert frame.geom_type.tolist() == frame_types
    assert [geom.wkb for geom in frame.geometry] == native_bytes
    assert prepared.geom_type.eq("MultiPolygon").all()
    expected = tmp_path / "public.gpkg"
    for layer in ("regions", "denominators"):
        prepared.to_file(expected, layer=layer, driver="GPKG",
                         mode="a" if expected.exists() else "w", index=False)
    for layer in ("regions", "denominators"):
        readback = gpd.read_file(expected, layer=layer)
        assert readback.geom_type.eq("MultiPolygon").all()
        _verify_geopackage_layer(prepared, readback, layer)

    # Zero-tolerance verification must reject a real but tiny vertex shift.
    changed = gpd.read_file(expected, layer="regions")
    changed.loc[changed.candidate_id.eq("single"), "geometry"] = MultiPolygon([
        box(0, 0, 1.0 + 1e-10, 1)
    ])
    with pytest.raises(RuntimeError, match="single"):
        _verify_geopackage_layer(prepared, changed, "regions")

    wrong_crs = gpd.read_file(expected, layer="regions").to_crs("EPSG:3857")
    with pytest.raises(RuntimeError, match="CRS"):
        _verify_geopackage_layer(prepared, wrong_crs, "regions")

    duplicated = gpd.read_file(expected, layer="regions")
    duplicated.loc[2, "candidate_id"] = "single"
    with pytest.raises(RuntimeError, match="duplicate"):
        _verify_geopackage_layer(prepared, duplicated, "regions")

    changed_attributes = gpd.read_file(expected, layer="regions")
    changed_attributes["publication_label"] = "not in original schema"
    with pytest.raises(RuntimeError, match="attribute columns"):
        _verify_geopackage_layer(prepared, changed_attributes, "regions")


def test_native_geoparquet_roundtrip_when_arrow_is_available(tmp_path):
    """Native file-I/O regression is exercised on meop40 (requires pyarrow)."""
    pytest.importorskip("pyarrow")
    native = gpd.GeoDataFrame(
        {"candidate_id": ["polygon", "multipolygon"]},
        geometry=[box(0, 0, 1, 1),
                  MultiPolygon([box(2, 0, 3, 1), box(4, 0, 5, 1)])],
        crs="EPSG:4326",
    )
    source = tmp_path / "input.parquet"
    native.to_parquet(source, index=False)
    readback = gpd.read_parquet(source)
    assert readback.geom_type.tolist() == ["Polygon", "MultiPolygon"]
    prepared = _public_polygon_layer(readback)
    target = tmp_path / "output.gpkg"
    prepared.to_file(target, layer="regions", driver="GPKG", index=False)
    exported = gpd.read_file(target, layer="regions")
    _verify_geopackage_layer(prepared, exported, "regions")


def test_source_override_export_definitions_are_complete():
    register = pd.read_csv(ROOT / "config/gwpt_source_overrides.csv")
    glossary = pd.read_csv(ROOT / "metadata/data_dictionary.csv")
    entries = set(glossary.loc[glossary.table.eq("metadata/gwpt_source_overrides.csv"), "field"])
    assert set(register.columns) == entries
    assert not glossary.table.str.contains("metadata/new_candidates/", regex=False).any()
