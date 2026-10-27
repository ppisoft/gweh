"""Small synthetic-grid regression without any external GWPT/GWA data."""

from __future__ import annotations

import csv
import hashlib
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from shapely.geometry import box

from viewer.build_viewer import generate


def _dataset(root):
    root.mkdir()
    ids = ["small_east", "small_west"]
    geometry = gpd.GeoDataFrame({
        "candidate_id": ids,
        "region_name": ["Small East", "Small West"],
        "macroregion": ["Synthetic", "Synthetic"],
        "surface_type": ["onshore", "onshore"],
        "geometry": [box(10, 38, 19, 58), box(-12, 38, 8, 58)],
    }, crs="EPSG:4326")
    geometry.to_file(root / "GWEH_geometries.gpkg", layer="regions", driver="GPKG")
    pd.DataFrame({
        "candidate_id": ids,
        "region_name": ["Small East", "Small West"],
        "macroregion": ["Synthetic", "Synthetic"],
        "surface_type": ["onshore", "onshore"],
        "evidence_selection_status": ["EVIDENCE_SELECTED"]*2,
        "market_maturity": ["CORE", "EMERGING"],
        "selection_pathway": ["PRIMARY_RESOURCE", "ABSOLUTE_MARKET_RESOURCE"],
        "climate_analysis_eligible": [1, 1],
        "primary_climate_panel": [0, 0],
        "climate_grid_center_count": [2, 3],
        "climate_use_tier": ["NARROW_CLIMATE_2_7"]*2,
    }).to_csv(root / "GWEH_catalogue.csv", index=False)
    pd.DataFrame({"candidate_id": ids, "R_score": [3, 2], "WPD100_mean_aw": [520, 340]
                  }).to_csv(root / "GWEH_region_metrics.csv", index=False)
    pd.DataFrame({"candidate_id": ids, "market_component": ["onshore"]*2,
                  "operating_mw_component": [300, 180]}
                 ).to_csv(root / "GWEH_market_components.csv", index=False)
    pd.DataFrame({"grid_id": [1, 2], "candidate_id": ids, "cell_count": [2, 3]}
                 ).to_csv(root / "GWEH_grid_region_lookup.csv", index=False)
    a = np.array([[0, 0, 0, 0], [2, 2, 1, 0], [0, 2, 1, 0]], dtype=np.int16)
    xr.Dataset({"gweh_region_id": (("lat", "lon"), a)},
               coords={"lat": [30.0, 45.0, 60.0], "lon": [-10.0, 0.0, 10.0, 20.0]}
               ).to_netcdf(root / "GWEH_grid_alternative.nc")
    (root / "metadata").mkdir()
    pd.DataFrame([{"table": "catalogue", "field": "market_maturity", "definition": "Market class",
                   "unit_or_domain": "category"}]).to_csv(root / "metadata/data_dictionary.csv", index=False)
    (root / "metadata/publication.yaml").write_text("product_version: 1.0.0\n")
    with (root / "MANIFEST_SHA256.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["file", "sha256"])
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.name != "MANIFEST_SHA256.csv":
                writer.writerow([f.relative_to(root).as_posix(), hashlib.sha256(f.read_bytes()).hexdigest()])


def test_viewer_supports_different_grid_and_candidate_count(tmp_path):
    source = tmp_path / "source"
    _dataset(source)
    first = tmp_path / "from_dir.html"
    report = generate(source, first)
    assert report["candidate_count"] == 2
    assert report["active_grid_cell_count"] == 5
    assert first.read_text().count('class="region"') == 2
    assert first.read_text().count('class="grid-cell"') == 5
    zippath = tmp_path / "dataset.zip"
    with zipfile.ZipFile(zippath, "w") as z:
        for f in source.rglob("*"):
            if f.is_file():
                z.write(f, "GWEH_dataset/" + f.relative_to(source).as_posix())
    second = tmp_path / "from_zip.html"
    generate(zippath, second)
    assert first.read_bytes() == second.read_bytes()
