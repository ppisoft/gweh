"""Independent compact release retains V4 WKB identity without a duplicate GPKG."""
import hashlib

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, box

from validation.geometry import create_blind_fingerprints, verify_public_fingerprints


def test_compact_v4_fingerprints_roundtrip_and_mismatch_rejection(tmp_path):
    shape1 = MultiPolygon([box(0, 0, 1, 1)])
    shape2 = MultiPolygon([box(2, 2, 3, 3)])
    public = tmp_path / "public.gpkg"
    blind = tmp_path / "blind.gpkg"
    mapping = tmp_path / "mapping.csv"
    output = tmp_path / "geometry_fingerprints.csv"
    gpd.GeoDataFrame({"candidate_id": ["one", "two"]},
                     geometry=[shape1, shape2], crs="EPSG:4326").to_file(
        public, layer="regions", driver="GPKG", index=False)
    gpd.GeoDataFrame({"validation_id": ["MV-00002", "MV-00001"]},
                     geometry=[shape1, shape2], crs="EPSG:4326").to_file(
        blind, layer="regions", driver="GPKG", index=False)
    pd.DataFrame({"candidate_id": ["one", "two"],
                  "validation_id": ["MV-00002", "MV-00001"]}).to_csv(mapping, index=False)
    assert create_blind_fingerprints(blind, mapping, output) == 2
    assert verify_public_fingerprints(public, mapping, output) == 2
    assert list(pd.read_csv(output).columns) == ["validation_id", "wkb_sha256"]
    # Modifying a hash must break publication, even if the manifest is refreshed.
    table = pd.read_csv(output)
    table.loc[0, "wkb_sha256"] = hashlib.sha256(b"not actual WKB").hexdigest()
    table.to_csv(output, index=False)
    with pytest.raises(ValueError, match="WKB differs"):
        verify_public_fingerprints(public, mapping, output)
    # A missing candidate likewise breaks the identity check.
    table.iloc[1:].to_csv(output, index=False)
    with pytest.raises(ValueError, match="Incomplete V4"):
        verify_public_fingerprints(public, mapping, output)


def test_reject_duplicate_mapping(tmp_path):
    from validation.geometry import _read_mapping
    mapping = tmp_path / "mapping.csv"
    pd.DataFrame({"candidate_id": ["one", "one"],
                  "validation_id": ["a", "b"]}).to_csv(mapping, index=False)
    with pytest.raises(ValueError, match="unique"):
        _read_mapping(mapping)
