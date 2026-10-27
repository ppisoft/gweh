"""Small analytic examples for the independent hourly MERRA-2 validation."""
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from shapely.geometry import box

from validation.merra import aggregate, compare, prepare


def _inputs(tmp_path: Path, monkeypatch):
    root = tmp_path / "science"
    (root / "05_exclusive").mkdir(parents=True)
    (root / "08_catalogue").mkdir()
    names = ["test_onshore", "test_offshore", "no_merra_centres"]
    geoms = gpd.GeoDataFrame({"candidate_id": names}, geometry=[
        box(-0.5, -0.5, 0.5, 0.5),
        box(19.5, 9.5, 20.5, 10.5),
        box(4.0, 4.0, 5.0, 5.0),
    ], crs="EPSG:4326")
    # Local test environment has Fiona, but production GeoParquet support is pyarrow.
    monkeypatch.setattr(gpd, "read_parquet", lambda _: geoms.copy())
    catalogue = pd.DataFrame({
        "candidate_id": names,
        "evidence_selection_status": ["EVIDENCE_SELECTED", "EVIDENCE_SELECTED", "EVIDENCE_NOT_SELECTED"],
        "climate_analysis_eligible": [1, 1, 0],
        "primary_climate_panel": [1, 0, 0],
    })
    metrics = pd.DataFrame({"candidate_id": names,
                            "WPD100_mean_aw": [85.0, 45.0, 5.0]})
    catalogue[["candidate_id", "evidence_selection_status"]].to_csv(
        root / "08_catalogue/catalogue.csv", index=False)
    catalogue[["candidate_id", "evidence_selection_status"]].to_csv(
        tmp_path / "catalogue.csv", index=False)
    catalogue[["candidate_id", "climate_analysis_eligible", "primary_climate_panel"]].to_csv(
        tmp_path / "roster.csv", index=False)
    metrics.to_csv(tmp_path / "metrics.csv", index=False)
    prep = tmp_path / "prep"
    assert prepare(root, prep)["candidate_count"] == len(names)
    blind = gpd.read_file(prep / "blind/regions.gpkg")
    assert list(blind.columns) == ["validation_id", "geometry"]
    assert "candidate_id" not in (prep / "blind/roster.csv").read_text()
    raw = tmp_path / "MERRA"
    raw.mkdir()
    lat = np.array([0., 10.])
    lon = np.array([0., 10., 20.])
    for day, speed in ((1, 3.0), (2, 5.0)):
        dims = ("time", "lat", "lon")
        shape = (24, len(lat), len(lon))
        winds = np.zeros(shape) + speed
        winds[:, 1, 2] = 4.0
        ps = np.ones(shape) * 100000.0
        t = np.ones(shape) * 300.0
        q = np.ones(shape) * 0.01
        ds = xr.Dataset({
            "U50M": (dims, winds), "V50M": (dims, np.zeros(shape)),
            "PS": (dims, ps), "T2M": (dims, t), "QV2M": (dims, q),
            "U10M": (dims, winds / 1.5), "V10M": (dims, np.zeros(shape)),
        }, coords={"time": np.arange(24), "lat": lat, "lon": lon})
        ds.to_netcdf(raw / f"MERRA2_400.tavg1_2d_slv_Nx.2008010{day}.nc", engine="scipy")
    return root, prep, raw


def test_fresh_merra_hourly_wpd_dynamic_roster_and_coverage(tmp_path, monkeypatch):
    root, prep, raw = _inputs(tmp_path, monkeypatch)
    checkpoint = tmp_path / "annual"
    assert aggregate(prep / "blind", raw, checkpoint,
                     start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)["candidate_count"] == 3
    # Restart should skip the accepted yearly checkpoint and preserve its contents.
    before = (checkpoint / "year_2008.csv").read_bytes()
    assert aggregate(prep / "blind", raw, checkpoint,
                     start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)["status"] == "COMPUTED"
    assert (checkpoint / "year_2008.csv").read_bytes() == before
    summary = compare(checkpoint, prep / "supervisor/mapping.csv",
                      tmp_path / "catalogue.csv", tmp_path / "metrics.csv", tmp_path / "comparison",
                      roster=tmp_path / "roster.csv")
    assert summary["candidate_count"] == 3
    assert summary["candidates_with_merra_centres"] == 2
    assert summary["population_sizes"] == {
        "all_candidates": 3, "evidence_selected": 2, "climate_eligible": 2,
        "primary_climate": 1,
    }
    assert summary["uncovered_candidate_ids"] == ["no_merra_centres"]
    result = pd.read_csv(tmp_path / "comparison/V4_merra_candidate_values.csv").set_index("candidate_id")
    density = 100000.0 / (287.05 * 300.0 * (1 + 0.61 * 0.01))
    expected = 0.5 * density * (3**3 + 5**3) / 2
    assert result.loc["test_onshore", "mean_ws50"] == pytest.approx(4.0)
    assert result.loc["test_onshore", "mean_wpd50_proxy"] == pytest.approx(expected)
    assert result.loc["test_offshore", "mean_ws50"] == pytest.approx(4.0)
    assert result.loc["test_offshore", "mean_wpd50_proxy"] == pytest.approx(0.5 * density * 4**3)
    assert pd.isna(result.loc["no_merra_centres", "mean_wpd50_proxy"])
    assert summary["comparison"]["all_candidates"]["n"] == 2
    assert summary["comparison"]["all_candidates"]["spearman_rho"] is None


def test_merra_missing_daily_file_is_not_silently_accepted(tmp_path, monkeypatch):
    _, prep, raw = _inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Missing 1 MERRA daily files"):
        aggregate(prep / "blind", raw, tmp_path / "out",
                  start=date(2008, 1, 1), end=date(2008, 1, 3), progress_every=0)
    assert not (tmp_path / "out/year_2008.csv").exists()


def test_merra_yearly_checkpoint_rejects_changed_source(tmp_path, monkeypatch):
    _, prep, raw = _inputs(tmp_path, monkeypatch)
    checkpoint = tmp_path / "annual"
    aggregate(prep / "blind", raw, checkpoint,
              start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)
    f = next(raw.glob("*20080102*"))
    f.touch()
    with pytest.raises(ValueError, match="checkpoint differs"):
        aggregate(prep / "blind", raw, checkpoint,
                  start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)


def test_merra_comparison_rejects_mismatched_universe(tmp_path, monkeypatch):
    _, prep, raw = _inputs(tmp_path, monkeypatch)
    checkpoint = tmp_path / "annual"
    aggregate(prep / "blind", raw, checkpoint,
              start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)
    frame = pd.read_csv(tmp_path / "catalogue.csv")
    frame = frame.iloc[:-1]
    frame.to_csv(tmp_path / "wrong.csv", index=False)
    with pytest.raises(ValueError, match="different candidate IDs|differ"):
        compare(checkpoint, prep / "supervisor/mapping.csv", tmp_path / "wrong.csv",
                tmp_path / "metrics.csv", tmp_path / "broken", roster=tmp_path / "roster.csv")


def test_deepowt_ground_truth_polygon_centroid_is_projected(tmp_path):
    import warnings
    from validation.spatial import _points
    geo = gpd.GeoDataFrame(geometry=[box(12.0, 48.0, 12.2, 48.1)], crs="EPSG:4326")
    source = tmp_path / "gt.gpkg"
    geo.to_file(source, layer="ground_truth", driver="GPKG")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        points, _ = _points(source, "DeepOWT_GT_LOCAL", ground_truth=True)
    assert len(points) == 1
    assert points.geometry.iloc[0].geom_type == "Point"
    assert 12.0 < points.geometry.x.iloc[0] < 12.2
    assert 48.0 < points.geometry.y.iloc[0] < 48.1
    assert not any("geographic CRS" in str(w.message) for w in captured)


def test_merra_rejects_missing_or_inconsistent_climate_roster(tmp_path, monkeypatch):
    _, prep, raw = _inputs(tmp_path, monkeypatch)
    checkpoint = tmp_path / "annual"
    aggregate(prep / "blind", raw, checkpoint,
              start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)
    roster = pd.read_csv(tmp_path / "roster.csv")
    roster.loc[0, "climate_analysis_eligible"] = pd.NA
    roster.to_csv(tmp_path / "bad_roster.csv", index=False)
    with pytest.raises(ValueError, match="Invalid or missing"):
        compare(checkpoint, prep / "supervisor/mapping.csv",
                tmp_path / "catalogue.csv", tmp_path / "metrics.csv",
                tmp_path / "comparison_bad", roster=tmp_path / "bad_roster.csv")
    assert not (tmp_path / "comparison_bad").exists()


def test_merra_rejects_wrong_primary_and_climate_classification(tmp_path, monkeypatch):
    _, prep, raw = _inputs(tmp_path, monkeypatch)
    checkpoint = tmp_path / "annual"
    aggregate(prep / "blind", raw, checkpoint,
              start=date(2008, 1, 1), end=date(2008, 1, 2), progress_every=0)
    roster = pd.read_csv(tmp_path / "roster.csv")
    roster.loc[0, "primary_climate_panel"] = 1
    roster.loc[0, "climate_analysis_eligible"] = 0
    roster.to_csv(tmp_path / "bad_roster.csv", index=False)
    with pytest.raises(ValueError, match="Primary climate"):
        compare(checkpoint, prep / "supervisor/mapping.csv",
                tmp_path / "catalogue.csv", tmp_path / "metrics.csv",
                tmp_path / "comparison_bad", roster=tmp_path / "bad_roster.csv")
