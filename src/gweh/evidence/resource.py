from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from rasterio.windows import Window, transform as window_transform
from shapely.geometry import mapping

from ..geography.common import geodesic_area_km2
from ..geometry.support import area_array_km2, geometry_for_raster, geometry_read_window


@dataclass(frozen=True)
class HistogramSpec:
    minimum: float
    maximum: float
    width: float

    @property
    def edges(self) -> np.ndarray:
        return np.arange(self.minimum, self.maximum + self.width, self.width, dtype=float)


class AreaWeightedAccumulator:
    def __init__(self, spec: HistogramSpec, thresholds: Iterable[float]):
        self.spec = spec
        self.edges = spec.edges
        self.histogram = np.zeros(len(self.edges) - 1, dtype=float)
        self.thresholds = [float(value) for value in thresholds]
        self.threshold_area = {value: 0.0 for value in self.thresholds}
        self.area_km2 = 0.0
        self.value_area_sum = 0.0
        self.valid_pixels = 0

    def update(self, values: np.ndarray, area_km2: np.ndarray, eligible: np.ndarray) -> None:
        valid = eligible & np.isfinite(values) & np.isfinite(area_km2) & (area_km2 > 0.0) & (values >= 0.0)
        if not valid.any():
            return
        raw = np.asarray(values[valid], dtype=float)
        area = np.asarray(area_km2[valid], dtype=float)
        below = raw < float(self.spec.minimum)
        if below.any():
            observed_min = float(np.min(raw[below]))
            raise ValueError(
                "Resource values below configured histogram minimum "
                f"{self.spec.minimum}: observed minimum={observed_min}"
            )
        observed_max = float(np.max(raw))
        if observed_max >= float(self.edges[-1]):
            required_bins = int(np.floor((observed_max - self.spec.minimum) / self.spec.width)) + 1
            current_bins = len(self.histogram)
            if required_bins > current_bins:
                self.histogram = np.pad(self.histogram, (0, required_bins - current_bins))
                self.edges = self.spec.minimum + np.arange(required_bins + 1, dtype=float) * self.spec.width
        self.valid_pixels += int(raw.size)
        self.area_km2 += float(area.sum())
        self.value_area_sum += float(np.dot(raw, area))
        for threshold in self.thresholds:
            self.threshold_area[threshold] += float(area[raw >= threshold].sum())
        bins = np.floor((raw - self.spec.minimum) / self.spec.width).astype(int)
        bins = np.clip(bins, 0, len(self.histogram) - 1)
        self.histogram += np.bincount(bins, weights=area, minlength=len(self.histogram))[: len(self.histogram)]

    def quantile(self, probability: float) -> float:
        if self.area_km2 <= 0.0 or self.histogram.sum() <= 0.0:
            return float("nan")
        cumulative = np.cumsum(self.histogram)
        target = float(probability) * cumulative[-1]
        index = int(np.searchsorted(cumulative, target, side="left"))
        index = min(max(index, 0), len(self.histogram) - 1)
        previous = cumulative[index - 1] if index > 0 else 0.0
        in_bin = self.histogram[index]
        fraction = 0.5 if in_bin <= 0.0 else float(np.clip((target - previous) / in_bin, 0.0, 1.0))
        return float(self.edges[index] + fraction * self.spec.width)

    def result(self) -> dict[str, float | int]:
        result = {
            "valid_pixel_count": int(self.valid_pixels),
            "resource_valid_area_km2": float(self.area_km2),
            "mean_aw": float(self.value_area_sum / self.area_km2) if self.area_km2 > 0.0 else float("nan"),
            "median_aw": self.quantile(0.50),
            "p75_aw": self.quantile(0.75),
        }
        for threshold in self.thresholds:
            label = str(threshold).rstrip("0").rstrip(".")
            area = float(self.threshold_area[threshold])
            result[f"A{label}_km2"] = area
            result[f"F{label}"] = area / self.area_km2 if self.area_km2 > 0.0 else float("nan")
        return result


def _windows(base, chunk_size):
    col0, row0 = int(base.col_off), int(base.row_off)
    col1 = col0 + int(base.width)
    row1 = row0 + int(base.height)
    for row in range(row0, row1, int(chunk_size)):
        for col in range(col0, col1, int(chunk_size)):
            yield Window(
                col,
                row,
                min(int(chunk_size), col1 - col),
                min(int(chunk_size), row1 - row),
            )


def summarize_wpd(
    dataset,
    geometry,
    *,
    chunk_size: int,
    histogram_spec: HistogramSpec,
    thresholds: Iterable[float],
) -> dict[str, float | int]:
    accumulator = AreaWeightedAccumulator(histogram_spec, thresholds)
    raster_geometry = geometry_for_raster(geometry, dataset)
    base_window = geometry_read_window(dataset, raster_geometry)
    if base_window is None:
        result = accumulator.result()
        result["surface_geom_area_km2"] = geodesic_area_km2(geometry)
        return result

    for window in _windows(base_window, chunk_size):
        transform = window_transform(window, dataset.transform)
        mask = rasterize(
            [(mapping(raster_geometry), 1)],
            out_shape=(int(window.height), int(window.width)),
            transform=transform,
            fill=0,
            all_touched=False,
            dtype="uint8",
        ).astype(bool)
        if not mask.any():
            continue
        array = dataset.read(1, window=window, masked=True)
        values = np.asarray(array.filled(np.nan), dtype=float)
        scale = float(dataset.scales[0]) if dataset.scales and dataset.scales[0] is not None else 1.0
        offset = float(dataset.offsets[0]) if dataset.offsets and dataset.offsets[0] is not None else 0.0
        values = values * scale + offset
        eligible = mask & ~np.ma.getmaskarray(array)
        accumulator.update(values, area_array_km2(dataset, window), eligible)

    result = accumulator.result()
    result["surface_geom_area_km2"] = geodesic_area_km2(geometry)
    return result


def sample_project_wpd(dataset, points: gpd.GeoDataFrame) -> dict[str, float | int]:
    if points.empty:
        return {"n": 0, "median": float("nan"), "mean_capacity_weighted": float("nan")}

    projected = points.to_crs(dataset.crs)
    values = []
    for sample in dataset.sample([(point.x, point.y) for point in projected.geometry], indexes=1, masked=True):
        values.append(np.nan if np.ma.is_masked(sample[0]) else float(sample[0]))
    values_array = np.asarray(values, dtype=float)
    scale = float(dataset.scales[0]) if dataset.scales and dataset.scales[0] is not None else 1.0
    offset = float(dataset.offsets[0]) if dataset.offsets and dataset.offsets[0] is not None else 0.0
    values_array = values_array * scale + offset

    weights = pd.to_numeric(points["capacity_mw"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    valid = np.isfinite(values_array)
    if not valid.any():
        return {"n": 0, "median": float("nan"), "mean_capacity_weighted": float("nan")}

    weighted_mean = (
        float(np.average(values_array[valid], weights=weights[valid]))
        if weights[valid].sum() > 0.0
        else float(np.nanmean(values_array[valid]))
    )
    return {
        "n": int(valid.sum()),
        "median": float(np.nanmedian(values_array[valid])),
        "mean_capacity_weighted": weighted_mean,
    }
