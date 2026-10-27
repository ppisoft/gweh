"""Geometry and raster-support helpers used during candidate delineation."""
from __future__ import annotations

import math
import geopandas as gpd
import numpy as np
import pandas as pd
from affine import Affine
from pyproj import CRS
from rasterio.errors import WindowError
from rasterio.features import geometry_window, rasterize, shapes
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform
from rasterio.warp import Resampling
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform

from ..geography.common import (
    GEOD,
    buffer_km,
    geodesic_area_km2,
    local_aeqd_crs,
    project_geometry,
    union_geometries,
)
from .common import polygonal, safe_intersection, spatial_subset_tolerant, union_polygonal


def split_parts(geometry) -> list:
    geometry = polygonal(geometry)
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return list(geometry.geoms)


def component_capacity_clusters(points: gpd.GeoDataFrame, link_km: float) -> list[tuple[float, int, object]]:
    if points.empty:
        return []

    support = union_geometries(points.geometry.tolist())
    if support is None or support.is_empty:
        return []

    local = local_aeqd_crs(support)
    projected = points.to_crs(local).copy()
    radius_m = float(link_km) * 500.0
    connected = union_polygonal([geom.buffer(radius_m) for geom in projected.geometry])
    clusters = []
    for component in split_parts(connected):
        mask = projected.geometry.map(component.covers)
        capacity = float(pd.to_numeric(projected.loc[mask, "capacity_mw"], errors="coerce").fillna(0.0).sum())
        geometry = project_geometry(component, local, "EPSG:4326")
        clusters.append((capacity, int(mask.sum()), polygonal(geometry)))
    return sorted(clusters, key=lambda item: item[0], reverse=True)


def select_capacity_clusters(
    clusters: list[tuple[float, int, object]],
    target_fraction: float,
    total_capacity_mw: float,
):
    if not clusters:
        return None
    if total_capacity_mw <= 0:
        return clusters[0][2]
    selected = []
    selected_capacity = 0.0
    for capacity, _, geometry in clusters:
        selected.append(geometry)
        selected_capacity += float(capacity)
        if selected_capacity / total_capacity_mw >= float(target_fraction):
            break
    return union_polygonal(selected)


def compact_support_envelope(
    geometry,
    *,
    margin_km: float,
    closing_km: float,
    simplify_km: float,
):
    if geometry is None or geometry.is_empty:
        return geometry
    local = local_aeqd_crs(geometry)
    projected = polygonal(project_geometry(geometry, "EPSG:4326", local))
    close_m = max(0.0, float(closing_km)) * 1000.0
    if close_m:
        projected = polygonal(projected.buffer(close_m).buffer(-close_m))
    margin_m = max(0.0, float(margin_km)) * 1000.0
    if margin_m:
        projected = polygonal(projected.buffer(margin_m))
    simplify_m = max(0.0, float(simplify_km)) * 1000.0
    if simplify_m and projected is not None and not projected.is_empty:
        projected = polygonal(projected.simplify(simplify_m, preserve_topology=True))
    return polygonal(project_geometry(projected, local, "EPSG:4326"))


def _shift_negative_longitudes_to_360(geometry):
    def shift(x, y, z=None):
        values = np.asarray(x, dtype=float) if not np.isscalar(x) else x
        shifted = np.where(values < 0, values + 360.0, values) if not np.isscalar(x) else (x + 360.0 if x < 0 else x)
        return (shifted, y) if z is None else (shifted, y, z)

    return shapely_transform(shift, geometry)


def geometry_for_raster(geometry, dataset):
    target = geometry
    crs = CRS.from_user_input(dataset.crs)
    if crs.is_geographic and dataset.bounds.left >= -1.0e-6 and dataset.bounds.right > 180.0 + 1.0e-6:
        target = _shift_negative_longitudes_to_360(target)
    return project_geometry(target, "EPSG:4326", dataset.crs)


def geometry_read_window(dataset, geometry):
    try:
        window = geometry_window(dataset, [mapping(geometry)], pad_x=0, pad_y=0)
    except (WindowError, ValueError):
        return None
    window = window.round_offsets().round_lengths()
    full = Window(0, 0, dataset.width, dataset.height)
    try:
        return window.intersection(full)
    except WindowError:
        return None


def area_array_km2(dataset, window) -> np.ndarray:
    transform = window_transform(window, dataset.transform)
    height, width = int(window.height), int(window.width)
    crs = CRS.from_user_input(dataset.crs)
    if crs.is_geographic:
        if abs(float(transform.b)) > 1.0e-12 or abs(float(transform.d)) > 1.0e-12:
            raise ValueError("Geographic raster area calculation requires an axis-aligned transform")
        x0, x1 = transform.c, transform.c + transform.a
        row_areas = np.empty(height, dtype=float)
        for row in range(height):
            y0 = transform.f + row * transform.e
            y1 = transform.f + (row + 1) * transform.e
            area, _ = GEOD.polygon_area_perimeter([x0, x1, x1, x0], [y0, y0, y1, y1])
            row_areas[row] = abs(area) / 1.0e6
        return np.broadcast_to(row_areas[:, None], (height, width))

    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    factors = [axis.unit_conversion_factor or 1.0 for axis in crs.axis_info[:2]]
    fx = factors[0] if factors else 1.0
    fy = factors[1] if len(factors) > 1 else fx
    return np.full((height, width), pixel_area * fx * fy / 1.0e6, dtype=float)


def coarse_resource_envelope(
    dataset,
    domain,
    *,
    threshold_w_m2: float,
    coarse_resource_grid_km: float,
    min_patch_km2: float,
):
    raster_geometry = geometry_for_raster(domain, dataset)
    base_window = geometry_read_window(dataset, raster_geometry)
    if base_window is None:
        return None

    bounds = window_bounds(base_window, dataset.transform)
    crs = CRS.from_user_input(dataset.crs)
    if crs.is_geographic:
        lat0 = float(domain.representative_point().y)
        dy = max(float(coarse_resource_grid_km) / 111.32, abs(dataset.transform.e))
        dx = max(
            float(coarse_resource_grid_km) / (111.32 * max(0.15, math.cos(math.radians(lat0)))),
            abs(dataset.transform.a),
        )
        out_width = max(1, int(math.ceil(abs(bounds[2] - bounds[0]) / dx)))
        out_height = max(1, int(math.ceil(abs(bounds[3] - bounds[1]) / dy)))
    else:
        unit = crs.axis_info[0].unit_conversion_factor or 1.0
        target_native = float(coarse_resource_grid_km) * 1000.0 / unit
        out_width = max(1, int(math.ceil(abs(bounds[2] - bounds[0]) / target_native)))
        out_height = max(1, int(math.ceil(abs(bounds[3] - bounds[1]) / target_native)))

    out_width = min(out_width, 1600)
    out_height = min(out_height, 1600)
    array = dataset.read(
        1,
        window=base_window,
        out_shape=(out_height, out_width),
        masked=True,
        resampling=Resampling.average,
    )
    values = np.asarray(array.filled(np.nan), dtype=float)
    scale = float(dataset.scales[0]) if dataset.scales and dataset.scales[0] is not None else 1.0
    offset = float(dataset.offsets[0]) if dataset.offsets and dataset.offsets[0] is not None else 0.0
    values = values * scale + offset

    base_transform = window_transform(base_window, dataset.transform)
    transform = base_transform * Affine.scale(base_window.width / out_width, base_window.height / out_height)
    domain_mask = rasterize(
        [(mapping(raster_geometry), 1)],
        out_shape=(out_height, out_width),
        transform=transform,
        fill=0,
        all_touched=False,
        dtype="uint8",
    ).astype(bool)
    good = domain_mask & ~np.ma.getmaskarray(array) & np.isfinite(values) & (values >= float(threshold_w_m2))
    if not good.any():
        return None

    polygons = []
    for geom_mapping, value in shapes(good.astype("uint8"), mask=good, transform=transform):
        if value != 1:
            continue
        geometry = polygonal(project_geometry(shape(geom_mapping), dataset.crs, "EPSG:4326"))
        if geometry is not None and geodesic_area_km2(geometry) >= float(min_patch_km2):
            polygons.append(geometry)
    return union_polygonal(polygons)


def select_resource_support(
    envelope,
    project_anchor,
    *,
    link_km: float,
    area_fraction_no_projects: float,
):
    if envelope is None or envelope.is_empty:
        return None
    parts = split_parts(envelope)
    areas = [geodesic_area_km2(part) for part in parts]
    if project_anchor is not None and not project_anchor.is_empty:
        zone = buffer_km(project_anchor, float(link_km))
        selected = [part for part in parts if part.intersects(zone)]
        if not selected:
            selected = [parts[int(np.argmax(areas))]]
    else:
        order = np.argsort(areas)[::-1]
        total = sum(areas)
        selected = []
        accumulated = 0.0
        for index in order:
            selected.append(parts[index])
            accumulated += areas[index]
            if total <= 0 or accumulated / total >= float(area_fraction_no_projects):
                break
    return union_polygonal(selected)


def point_distance_km(points: gpd.GeoDataFrame, geometry) -> np.ndarray:
    if points.empty:
        return np.array([], dtype=float)
    local = local_aeqd_crs(geometry)
    target = project_geometry(geometry, "EPSG:4326", local)
    projected = points.to_crs(local)
    return np.asarray([float(point.distance(target)) / 1000.0 for point in projected.geometry], dtype=float)


def apply_anchor_guard(
    final_geometry,
    component_domains: dict[str, object],
    active_points: gpd.GeoDataFrame,
    *,
    candidate_surface: str,
    guard_km: float,
    position_tolerance_km: float,
):
    final_geometry = polygonal(final_geometry)
    base = polygonal(component_domains.get("primary"))
    if final_geometry is None or base is None or active_points.empty or float(guard_km) <= 0:
        return final_geometry, False

    # The denominator uses the same positional tolerance as membership.
    denominator = spatial_subset_tolerant(active_points, base, position_tolerance_km)
    if candidate_surface != "mixed":
        denominator = denominator.loc[
            denominator["project_surface_type_effective"].astype(str).str.lower().eq(candidate_surface)
        ].copy()
    if denominator.empty:
        return final_geometry, False

    inside = denominator.geometry.intersects(final_geometry)
    missing = denominator.loc[~inside].copy()
    if missing.empty:
        return final_geometry, False

    missing["distance_km"] = point_distance_km(missing, final_geometry)
    eligible = missing.loc[missing["distance_km"].le(float(guard_km))].copy()
    if eligible.empty:
        return final_geometry, False

    onshore = polygonal(component_domains.get("onshore"))
    offshore = polygonal(component_domains.get("offshore"))
    guards = []
    for row in eligible.itertuples(index=False):
        clip = base
        if candidate_surface == "mixed":
            if onshore is not None and row.geometry.intersects(onshore):
                clip = onshore
            elif offshore is not None and row.geometry.intersects(offshore):
                clip = offshore
        guard = safe_intersection(buffer_km(row.geometry, float(guard_km)), clip, label="anchor guard clip")
        if guard is not None and not guard.is_empty:
            guards.append(guard)

    if not guards:
        return final_geometry, False

    guarded = union_polygonal([final_geometry, *guards])
    guarded = safe_intersection(guarded, base, label="anchor guard domain clip")
    if guarded is None or guarded.is_empty:
        return final_geometry, False
    return guarded, bool(eligible.geometry.intersects(guarded).any())

