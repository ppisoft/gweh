from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union

from ..geography.common import buffer_km, clean_polygonal


def read_stage_vector(stage_dir: Path, stem: str) -> gpd.GeoDataFrame:
    """Read the canonical GeoParquet output of an upstream core stage."""
    path = stage_dir / f"{stem}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Vector input not found: {path}")
    return gpd.read_parquet(path).to_crs("EPSG:4326")


def read_gwpt(stage_dir: Path) -> gpd.GeoDataFrame:
    path = stage_dir / "gwpt.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Stage-01 GWPT output not found: {path}")
    return gpd.read_parquet(path).to_crs("EPSG:4326")


def boolean_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes", "y", "t"})


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    geometry = make_valid(geometry)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return clean_polygonal(geometry)
    if isinstance(geometry, GeometryCollection):
        parts = [g for g in geometry.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        return clean_polygonal(unary_union(parts)) if parts else None
    return None


def union_polygonal(geometries):
    parts = [polygonal(g) for g in geometries if g is not None and not g.is_empty]
    parts = [g for g in parts if g is not None and not g.is_empty]
    return polygonal(unary_union(parts)) if parts else None


def safe_intersection(left, right, *, label: str = "intersection"):
    if left is None or right is None or left.is_empty or right.is_empty:
        return None
    try:
        result = left.intersection(right)
    except Exception:
        result = make_valid(left).intersection(make_valid(right))
    result = polygonal(result)
    if result is not None and not result.is_valid:
        raise RuntimeError(f"Invalid geometry after {label}")
    return result


def safe_difference(left, right, *, label: str = "difference"):
    if left is None or left.is_empty:
        return None
    if right is None or right.is_empty:
        return polygonal(left)
    try:
        result = left.difference(right)
    except Exception:
        result = make_valid(left).difference(make_valid(right))
    result = polygonal(result)
    if result is not None and not result.is_valid:
        raise RuntimeError(f"Invalid geometry after {label}")
    return result


def spatial_subset(points: gpd.GeoDataFrame, geometry) -> gpd.GeoDataFrame:
    if points.empty or geometry is None or geometry.is_empty:
        return points.iloc[0:0].copy()
    try:
        positions = points.sindex.query(geometry, predicate="intersects")
        return points.iloc[np.asarray(positions, dtype=int)].copy()
    except Exception:
        return points.loc[points.geometry.intersects(geometry)].copy()


def eligibility_geometry(geometry, tolerance_km: float):
    """Geometry widened by the declared positional tolerance.

    Project coordinates carry positional uncertainty and the reference
    coastline is generalized, so a point may fall marginally outside an
    otherwise correct domain. The tolerance is applied to point selection
    only; polygons are never widened by it.
    """
    if geometry is None or geometry.is_empty:
        return geometry
    tolerance_km = float(tolerance_km)
    if tolerance_km <= 0:
        return geometry
    widened = buffer_km(geometry, tolerance_km)
    return widened if widened is not None and not widened.is_empty else geometry


def spatial_subset_tolerant(points: gpd.GeoDataFrame, geometry, tolerance_km: float) -> gpd.GeoDataFrame:
    return spatial_subset(points, eligibility_geometry(geometry, tolerance_km))
