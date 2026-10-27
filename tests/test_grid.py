"""CESM centre membership and one-cell exclusions, using native coordinates."""
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from shapely.geometry import box

from grid import _coordinates, classify_centres, make_grid

ROOT = Path(__file__).resolve().parents[1]


def test_grid_shared_boundary_is_not_double_assigned():
    lat = np.array([0.5])
    lon = np.array([0.5, 1.0, 1.5])
    geometry = gpd.GeoDataFrame(
        {"candidate_id": ["left", "right"]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
        crs="EPSG:4326",
    )
    values = classify_centres(geometry, lat, lon)
    assert values.tolist() == [[1, 0, 2]]


def test_one_cell_selected_region_stays_in_roster_but_not_grid(monkeypatch, tmp_path):
    template = ROOT / "grid/CESM_192x288.nc"
    lat, lon, *_ = _coordinates(template)
    row = len(lat) // 2
    col = len(lon) // 2
    delta_lat = abs(lat[row + 1] - lat[row])
    delta_lon = abs(lon[col + 1] - lon[col])
    x0 = ((lon[col] + 180) % 360) - 180
    x1 = ((lon[col + 1] + 180) % 360) - 180
    y = lat[row]
    # Distinct polygons: one centre and two centres (same latitude row).
    single = box(x0 - 0.3 * delta_lon, y - 0.3 * delta_lat,
                 x0 + 0.3 * delta_lon, y + 0.3 * delta_lat)
    multi = box(x1 - 0.3 * delta_lon, y - 0.3 * delta_lat,
                x1 + 1.3 * delta_lon, y + 0.3 * delta_lat)
    frame = gpd.GeoDataFrame(
        {"candidate_id": ["new_single", "selected_large", "not_selected"]},
        geometry=[single, multi, box(-100, -80, -99, -79)], crs="EPSG:4326",
    )
    monkeypatch.setattr(gpd, "read_parquet", lambda *_: frame)
    run = tmp_path / "run"
    (run / "08_catalogue").mkdir(parents=True)
    pd.DataFrame([
        {"candidate_id": "new_single", "region_name": "Single", "surface_type": "onshore",
         "evidence_selection_status": "EVIDENCE_SELECTED", "market_maturity": "EMERGING"},
        {"candidate_id": "selected_large", "region_name": "Large", "surface_type": "onshore",
         "evidence_selection_status": "EVIDENCE_SELECTED", "market_maturity": "CORE"},
        {"candidate_id": "not_selected", "region_name": "Context", "surface_type": "offshore",
         "evidence_selection_status": "EVIDENCE_NOT_SELECTED", "market_maturity": "PROSPECTIVE"},
    ]).to_csv(run / "08_catalogue/catalogue.csv", index=False)
    out = tmp_path / "grid_output"
    make_grid(run, template, out)
    roster = pd.read_csv(out / "GWEH_grid_candidate_roster.csv").set_index("candidate_id")
    assert roster.loc["new_single", "climate_grid_center_count"] == 1
    assert roster.loc["new_single", "climate_analysis_eligible"] == 0
    assert roster.loc["selected_large", "climate_analysis_eligible"] == 1
    assert pd.isna(roster.loc["not_selected", "climate_grid_center_count"])
    with xr.open_dataset(out / "GWEH_grid_CESM_192x288.nc") as dataset:
        assert dataset.attrs["Conventions"] == "CF-1.11"
        assert dataset.sizes["region"] == 1
        assert int(dataset.gweh_region_mask.sum()) >= 2
        assert int(dataset.gweh_region_cell_count[0]) >= 2


def test_metadata_only_update_preserves_numerical_values(tmp_path):
    from cf_metadata import refresh_file
    # A small, valid numeric classification fixture independent of upstream geometry.
    source = tmp_path / "source.nc"
    destination = tmp_path / "destination.nc"
    xr.Dataset(
        data_vars={"gweh_region_id": (("lat", "lon"), np.array([[0, 1]], dtype="i2"))},
        coords={"lat": np.array([0.0]), "lon": np.array([0.0, 1.0])},
        attrs={"Conventions": "CF-1.11"},
    ).to_netcdf(source, engine="scipy", format="NETCDF3_64BIT")
    refresh_file(source, destination, tmp_path, ROOT / "metadata/publication.yaml")
    with xr.open_dataset(destination, decode_cf=False) as final:
        assert final.attrs["history"].startswith("20")
        assert final.attrs["Conventions"] == "CF-1.11"
        import yaml
        source_metadata = yaml.safe_load((ROOT / "metadata/publication.yaml").read_text())
        assert final.attrs["product_version"] == source_metadata["product_version"]
        assert final.attrs["summary"]
        assert final.attrs["creator_email"] == source_metadata["creator_email"]
        assert final.attrs["id"] == source_metadata["id"]
        assert final.gweh_region_id.values.tolist() == [[0, 1]]
        assert final.lat.attrs["axis"] == "Y"
        assert final.lon.attrs["axis"] == "X"


def test_grid_template_dimensions_follow_input(tmp_path):
    source = tmp_path / "custom_model.nc"
    xr.Dataset(coords={"lat": [0.0, 1.0, 2.0], "lon": [2.0, 3.0, 4.0, 5.0]}).to_netcdf(
        source, engine="scipy", format="NETCDF3_64BIT")
    lat, lon, *_ = _coordinates(source)
    assert len(lat) == 3 and len(lon) == 4


def test_workflow_rejects_stale_publication_grid_metadata(tmp_path):
    """An old rc2 CF grid must not be silently reused for release 1.0.0."""
    import hashlib
    import json
    import yaml
    import pytest
    from workflow.run import Settings, make_grid as workflow_make_grid

    parent = tmp_path / "datasets"
    run = parent / "science_scenarios/experiment/data/runs/science"
    (run / "08_catalogue").mkdir(parents=True)
    (run / "05_exclusive").mkdir()
    (run / "manifest").mkdir()
    pd.DataFrame({"candidate_id": ["sample_region"]}).to_csv(
        run / "08_catalogue/catalogue.csv", index=False
    )
    geometry = run / "05_exclusive/exclusive_regions.parquet"
    geometry.write_bytes(b"geometry scientific source fixture")
    (run / "manifest/run_manifest.json").write_text(
        json.dumps({"source_code_sha256": "science_hash"})
    )
    dest = tmp_path / "grid"
    dest.mkdir()
    pd.DataFrame({
        "candidate_id": ["sample_region"],
        "climate_analysis_eligible": [1], "primary_climate_panel": [1]
    }).to_csv(dest / "GWEH_grid_candidate_roster.csv", index=False)
    (dest / "GWEH_grid_region_lookup.csv").write_text(
        "grid_id,candidate_id\n1,sample_region\n"
    )
    # Outdated id/version are intentional fixture values: grid reuse must fail.
    xr.Dataset(coords={"lat": [0.], "lon": [0.]}, attrs={
        "gweh_source_run_id": "science",
        "source_code_sha256": "science_hash",
        "geometry_regions_sha256": hashlib.sha256(geometry.read_bytes()).hexdigest(),
        "product_version": "1.0.0-rc2",
        "id": "gweh65_cesm_192x288_v1.0",
    }).to_netcdf(dest / "GWEH_grid_CESM_192x288.nc", engine="scipy")
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({
        "data_parent": str(parent), "scenario_id": "experiment",
        "run_id": "science", "grid_dir": str(dest),
    }))
    with pytest.raises(ValueError, match="NEW grid_dir"):
        workflow_make_grid(Settings(config))
