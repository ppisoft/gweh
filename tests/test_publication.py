"""Archive checks must reject incomplete data before producing deposit ZIPs."""
import csv
import json
import re
from pathlib import Path

import pytest

from publication.package import check_public, check_netcdf_release_identity, sha256, sources

ROOT = Path(__file__).resolve().parents[1]


def test_source_archive_excludes_working_config_and_historical_scaffolding():
    names = [name for _, name in sources(ROOT)]
    assert "GWEH/RUN.sh" in names
    assert "GWEH/src/gweh/pipeline.py" in names
    assert "GWEH/omission/screen.py" in names
    assert not any(name.startswith("GWEH/omission/") and name not in {
        "GWEH/omission/screen.py", "GWEH/omission/README.md",
        "GWEH/omission/example_expert_proposals.csv",
    } for name in names)
    assert "GWEH/validation/merra.py" in names
    assert "GWEH/publication/package.py" in names
    assert "GWEH/verification/acceptance.py" in names
    assert "GWEH/CONTRIBUTING.md" in names and "GWEH/SIGNOFF.md" in names
    assert "GWEH/config/gwpt_source_overrides.csv" in names
    assert not any("new_candidate_" in name or "publication/README.md" in name
                   for name in names)
    assert not any("workflow.local.yaml" in name or "PUBLICATION_TODO" in name
                   or re.search(r"GWEH[0-9]+", name) for name in names)


def test_release_rejects_manifest_gap(tmp_path):
    output = tmp_path / "data"
    output.mkdir()
    (output / "GWEH_catalogue.csv").write_text("candidate_id\na\n")
    (output / "MANIFEST_SHA256.csv").write_text("file,sha256\n")
    with pytest.raises(ValueError, match="manifest"):
        check_public(output)


def sha256_dummy(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest() + "\n"


def test_release_rejects_unfinished_validation(tmp_path):
    output = tmp_path / "data"
    output.mkdir()
    files = {
        "GWEH_catalogue.csv": "candidate_id\na\n",
        "GWEH_scores.csv": "candidate_id\na\n",
        "GWEH_region_metrics.csv": "candidate_id\na\n",
        "GWEH_market_components.csv": "candidate_id\na\n",
        "GWEH_geometries.gpkg": "not a real GPKG fixture",
        "GWEH_grid_region_lookup.csv": "grid_id,candidate_id\n1,a\n",
        "GWEH_grid_CESM_192x288.nc": "not a real NetCDF fixture",
        "metadata/candidate_nomination_register.csv": "candidate_id\na\n",
        "metadata/source_register.csv": "source_id,derived_product_scope\ns1,aggregates only\n",
        "metadata/gwpt_source_overrides.csv": "phase_id,field\nx,Capacity (MW)\n",
        "metadata/publication.yaml": "id: gweh_grid_fixture_v1.0\nproduct_version: 1.0.0\ndataset_doi: 10.5281/zenodo.999999\n",
        "metadata/strategic_s/strategic_assessment.csv": "candidate_id,S1,S2,S3,S4\na,1,1,1,1\n",
        "metadata/strategic_s/strategic_score_rules.yaml": "version: 1\n",
        "metadata/data_dictionary.csv":
            "table,field,definition,unit_or_domain\n"
            "validation/merra_provenance/annual/year_YYYY.csv,validation_id,Anonymous ID,text\n"
            "validation/merra_provenance/annual/year_YYYY.csv,year,Year,YYYY\n"
            "validation/merra_provenance/blind/roster.csv,validation_id,Anonymous ID,text\n"
            "validation/merra_provenance/blind/geometry_fingerprints.csv,validation_id,Anonymous ID,text\n"
            "validation/merra_provenance/blind/geometry_fingerprints.csv,wkb_sha256,Geometry WKB hash,SHA-256\n"
            "metadata/strategic_s/strategic_assessment.csv,candidate_id,Identifier,text\n"
            "metadata/strategic_s/strategic_assessment.csv,S1,Score,integer\n"
            "metadata/strategic_s/strategic_assessment.csv,S2,Score,integer\n"
            "metadata/strategic_s/strategic_assessment.csv,S3,Score,integer\n"
            "metadata/strategic_s/strategic_assessment.csv,S4,Score,integer\n"
            "metadata/strategic_s/strategic_score_rules.yaml,*,Rule document,YAML\n"
            "metadata/source_register.csv,source_id,Identifier,text\n"
            "metadata/gwpt_source_overrides.csv,phase_id,Identifier,text\n"
            "metadata/gwpt_source_overrides.csv,field,Source field,text\n"
            "metadata/source_register.csv,derived_product_scope,Scope,text\n",
        "DATA_RIGHTS.md": "Derived GWEH rights and source attribution\n",
        "DATA_LICENSE.md": "CC BY 4.0, upstream rights remain\n",
        "README.md": "GWEH reference dataset\n",
        "CITATION.cff": "cff-version: 1.2.0\ntitle: GWEH test\ntype: dataset\nlicense: CC-BY-4.0\ndoi: 10.5281/zenodo.999999\n",
        "validation/methodology/merra_protocol.yaml":
            "blind_seed: 20261018\nperiod:\n  start: '2008-01-01'\n  end: '2008-12-31'\n",
        "validation/merra_provenance/blind/roster.csv": "validation_id\na\n",
        "validation/merra_provenance/blind/geometry_fingerprints.csv": "validation_id,wkb_sha256\na,placeholder\n",
        "validation/merra_provenance/supervisor/mapping.csv": "candidate_id,validation_id\na,a\n",
        "validation/merra_provenance/annual/aggregation_context.json":
            json.dumps({"blind_geometry_sha256": "testgeometry",
                        "merra_root": "<external MERRA-2 input directory>"}),
        "validation/merra_provenance/annual/year_2008.csv": "validation_id,year\na,2008\n",
        "validation/VALIDATION_SCOPE.json": json.dumps({"status": "INCOMPLETE"}),
        "validation/CF11_IOOS_CHECK.txt": "All tests passed!",
        "validation/CF_GRID_SHA256.txt": sha256_dummy("not a real NetCDF fixture"),
        "validation/spatial/V1_V2_summary.json": "{}",
        "validation/market/V3_summary.json": "{}",
        "validation/merra/V4_summary.json": "{}",
    }
    for name, value in files.items():
        dest = output / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(value)
    with (output / "MANIFEST_SHA256.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "sha256"])
        for name in sorted(files):
            writer.writerow([name, sha256(output / name)])
    with pytest.raises(ValueError, match="complete V1"):
        check_public(output)


def test_release_rejects_outdated_grid_identity(tmp_path):
    import numpy as np
    import xarray as xr
    grid = xr.Dataset({"gweh_region_id": (("lat", "lon"), np.zeros((2, 2), dtype=np.int16))},
                      coords={"lat": [0., 1.], "lon": [0., 1.]})
    grid.attrs.update(product_version="1.0.0-rc2", id="gweh_grid_legacy",
                      source_code_sha256="science", geometry_regions_sha256="geometry")
    grid.to_netcdf(tmp_path / "GWEH_grid_demo.nc", engine="scipy")
    with pytest.raises(ValueError, match="product_version"):
        check_netcdf_release_identity(
            tmp_path, {"product_version": "1.0.0", "id": "gweh_grid_demo_v1.0"},
            {"source_code_sha256": "science"}, "geometry")
    grid.attrs.update(product_version="1.0.0", id="gweh_grid_demo_v1.0")
    grid.to_netcdf(tmp_path / "GWEH_grid_demo.nc", engine="scipy")
    check_netcdf_release_identity(
        tmp_path, {"product_version": "1.0.0", "id": "gweh_grid_demo_v1.0"},
        {"source_code_sha256": "science"}, "geometry")


def _complete_public_fixture(tmp_path):
    """Construct a compact valid public release contract for negative tests."""
    public = tmp_path / "public"
    public.mkdir()
    nc = "netcdf fixture bytes"
    expected_files = {
        "GWEH_catalogue.csv": "candidate_id\na\n",
        "GWEH_scores.csv": "candidate_id\na\n",
        "GWEH_region_metrics.csv": "candidate_id\na\n",
        "GWEH_market_components.csv": "candidate_id\na\n",
        "GWEH_geometries.gpkg": "geo package fixture bytes",
        "GWEH_grid_region_lookup.csv": "grid_id,candidate_id\n1,a\n",
        "GWEH_grid_fixture.nc": nc,
        "metadata/candidate_nomination_register.csv": "candidate_id\na\n",
        "metadata/source_register.csv": "source_id,derived_product_scope\ns1,aggregates only\n",
        "metadata/gwpt_source_overrides.csv": "phase_id,field\nx,Capacity (MW)\n",
        "metadata/publication.yaml": "id: gweh_grid_fixture_v1.0\nproduct_version: 1.0.0\ndataset_doi: 10.5281/zenodo.999999\n",
        "metadata/strategic_s/strategic_assessment.csv": "candidate_id,S1,S2,S3,S4\na,1,1,1,1\n",
        "metadata/strategic_s/strategic_score_rules.yaml": "version: 1\n",
        "metadata/data_dictionary.csv":
            "table,field,definition,unit_or_domain\n"
            "validation/merra_provenance/annual/year_YYYY.csv,validation_id,Anonymous ID,text\n"
            "validation/merra_provenance/annual/year_YYYY.csv,year,Year,YYYY\n"
            "validation/merra_provenance/blind/roster.csv,validation_id,Anonymous ID,text\n"
            "validation/merra_provenance/blind/geometry_fingerprints.csv,validation_id,Anonymous ID,text\n"
            "validation/merra_provenance/blind/geometry_fingerprints.csv,wkb_sha256,Geometry WKB hash,SHA-256\n"
            "metadata/strategic_s/strategic_assessment.csv,candidate_id,Identifier,text\n"
            "metadata/strategic_s/strategic_assessment.csv,S1,Score,integer\n"
            "metadata/strategic_s/strategic_assessment.csv,S2,Score,integer\n"
            "metadata/strategic_s/strategic_assessment.csv,S3,Score,integer\n"
            "metadata/strategic_s/strategic_assessment.csv,S4,Score,integer\n"
            "metadata/strategic_s/strategic_score_rules.yaml,*,Rule document,YAML\n"
            "metadata/source_register.csv,source_id,Identifier,text\n"
            "metadata/gwpt_source_overrides.csv,phase_id,Identifier,text\n"
            "metadata/gwpt_source_overrides.csv,field,Source field,text\n"
            "metadata/source_register.csv,derived_product_scope,Scope,text\n",
        "DATA_RIGHTS.md": "Derived GWEH rights and source attribution\n",
        "DATA_LICENSE.md": "CC BY 4.0, upstream rights remain\n",
        "README.md": "GWEH reference dataset\n",
        "CITATION.cff": "cff-version: 1.2.0\ntitle: GWEH test\ntype: dataset\nlicense: CC-BY-4.0\ndoi: 10.5281/zenodo.999999\n",
        "validation/methodology/merra_protocol.yaml":
            "blind_seed: 20261018\nperiod:\n  start: '2008-01-01'\n  end: '2008-12-31'\n",
        "validation/merra_provenance/blind/roster.csv": "validation_id\na\n",
        "validation/merra_provenance/blind/geometry_fingerprints.csv": "validation_id,wkb_sha256\na,placeholder\n",
        "validation/merra_provenance/supervisor/mapping.csv": "candidate_id,validation_id\na,a\n",
        "validation/merra_provenance/annual/aggregation_context.json":
            json.dumps({"blind_geometry_sha256": "testgeometry",
                        "merra_root": "<external MERRA-2 input directory>"}),
        "validation/merra_provenance/annual/year_2008.csv": "validation_id,year\na,2008\n",
        "validation/VALIDATION_SCOPE.json": json.dumps({
            "status": "COMPLETE_REPORT_SET_REQUIRES_SCIENTIFIC_REVIEW"
        }),
        "validation/CF11_IOOS_CHECK.txt": "All tests passed!\n",
        "validation/CF_GRID_SHA256.txt": sha256_dummy(nc),
        "validation/spatial/V1_V2_summary.json": "{}",
        "validation/market/V3_summary.json": "{}",
        "validation/merra/V4_summary.json": "{}",
    }
    for name, content in expected_files.items():
        f = public / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    # The check_public fixture must exercise actual geometry fingerprints,
    # not short-circuit the new scientific release contract with fake GPKG bytes.
    import geopandas as gpd
    from shapely.geometry import MultiPolygon, box
    gpath = public / "GWEH_geometries.gpkg"
    gpath.unlink()
    geometry = MultiPolygon([box(0, 0, 1, 1)])
    gpd.GeoDataFrame({"candidate_id": ["a"]}, geometry=[geometry], crs="EPSG:4326").to_file(
        gpath, layer="regions", driver="GPKG", index=False)
    import hashlib
    fingerprint = hashlib.sha256(gpd.read_file(gpath, layer="regions").geometry.iloc[0].wkb).hexdigest()
    (public / "validation/merra_provenance/blind/geometry_fingerprints.csv").write_text(
        f"validation_id,wkb_sha256\na,{fingerprint}\n")
    with (public / "MANIFEST_SHA256.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "sha256"])
        for name in sorted(expected_files):
            writer.writerow([name, sha256(public / name)])
    assert check_public(public) == (len(expected_files), 1)
    return public, expected_files


def _refresh_manifest(public):
    with (public / "MANIFEST_SHA256.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "sha256"])
        for file in sorted(public.rglob("*")):
            if file.is_file() and file.name != "MANIFEST_SHA256.csv":
                writer.writerow([file.relative_to(public).as_posix(), sha256(file)])


def test_release_rejects_citation_type_or_licence_mismatch(tmp_path):
    public, _ = _complete_public_fixture(tmp_path)
    target = public / "CITATION.cff"
    target.write_text(target.read_text().replace("type: dataset", "type: software"))
    _refresh_manifest(public)
    with pytest.raises(ValueError, match="Dataset CITATION"):
        check_public(public)
    target.write_text(target.read_text().replace("type: software", "type: dataset")
                      .replace("license: CC-BY-4.0", "license: MIT"))
    _refresh_manifest(public)
    with pytest.raises(ValueError, match="Dataset CITATION"):
        check_public(public)


def test_release_rejects_missing_strategic_dictionary_definition(tmp_path):
    public, _ = _complete_public_fixture(tmp_path)
    target = public / "metadata/data_dictionary.csv"
    target.write_text(target.read_text().replace(
        "metadata/strategic_s/strategic_assessment.csv,S1,Score,integer\n", ""))
    _refresh_manifest(public)
    with pytest.raises(ValueError, match="Strategic-S assessment"):
        check_public(public)


def test_release_rejects_missing_source_register_definition(tmp_path):
    public, _ = _complete_public_fixture(tmp_path)
    target = public / "metadata/data_dictionary.csv"
    target.write_text(target.read_text().replace(
        "metadata/source_register.csv,derived_product_scope,Scope,text\n", ""))
    _refresh_manifest(public)
    with pytest.raises(ValueError, match="source register"):
        check_public(public)


def test_complete_release_metadata_does_not_require_omission(tmp_path):
    """The scientific archive has no dependency on historical cohort exports."""
    public, expected_files = _complete_public_fixture(tmp_path)
    assert not (public / "omission").exists()
    missing = public / "validation/merra_provenance/annual/year_2008.csv"
    missing.unlink()
    with (public / "MANIFEST_SHA256.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "sha256"])
        for name in sorted(expected_files):
            if name != "validation/merra_provenance/annual/year_2008.csv":
                writer.writerow([name, sha256(public / name)])
    with pytest.raises(ValueError, match="annual publication checkpoints"):
        check_public(public)
    missing.write_text(expected_files["validation/merra_provenance/annual/year_2008.csv"])
    with (public / "MANIFEST_SHA256.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "sha256"])
        for name in sorted(expected_files):
            writer.writerow([name, sha256(public / name)])
    preview = public / "PREVIEW_NOT_FOR_DOI.md"
    preview.write_text("NOT FOR DOI")
    with (public / "MANIFEST_SHA256.csv").open("a", newline="") as handle:
        csv.writer(handle).writerow(["PREVIEW_NOT_FOR_DOI.md", sha256(preview)])
    with pytest.raises(ValueError, match="QA preview"):
        check_public(public)
    preview.unlink()
    with (public / "MANIFEST_SHA256.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "sha256"])
        for name in sorted(expected_files):
            writer.writerow([name, sha256(public / name)])
    obsolete = public / "omission/obsolete.csv"
    obsolete.parent.mkdir()
    obsolete.write_text("nomination_round,nominated\nlegacy,1\n")
    with (public / "MANIFEST_SHA256.csv").open("a", newline="") as handle:
        csv.writer(handle).writerow([
            "omission/obsolete.csv", sha256(obsolete)
        ])
    with pytest.raises(ValueError, match="obsolete historical omission"):
        check_public(public)


def test_omission_requires_an_actual_screening_request():
    from types import SimpleNamespace
    from workflow.run import omission
    with pytest.raises(ValueError, match="omission.expert_proposals"):
        omission(SimpleNamespace(data={"omission": {}}))


def test_release_identity_includes_reserved_do_is(tmp_path):
    import numpy as np
    import xarray as xr
    grid = xr.Dataset({"gweh_region_id": (("lat", "lon"), np.zeros((1, 1), dtype=np.int16))},
                      coords={"lat": [0.], "lon": [0.]})
    grid.attrs.update(product_version="1.0.0", id="gweh_grid_demo_v1.0",
                      source_code_sha256="science", geometry_regions_sha256="geometry",
                      publisher_name="Zenodo", dataset_doi="10.5281/zenodo.12345",
                      software_doi="10.5281/zenodo.23456", publisher_url="https://zenodo.org")
    path = tmp_path / "GWEH_grid_demo.nc"
    grid.to_netcdf(path, engine="scipy")
    expected = {"product_version": "1.0.0", "id": "gweh_grid_demo_v1.0",
                "dataset_doi": "10.5281/zenodo.12345", "software_doi": "10.5281/zenodo.23456",
                "publisher_name": "Zenodo", "publisher_url": "https://zenodo.org"}
    check_netcdf_release_identity(tmp_path, expected, {"source_code_sha256": "science"}, "geometry")
    expected["dataset_doi"] = "10.5281/zenodo.99999"
    with pytest.raises(ValueError, match="NetCDF dataset_doi"):
        check_netcdf_release_identity(tmp_path, expected, {"source_code_sha256": "science"}, "geometry")
