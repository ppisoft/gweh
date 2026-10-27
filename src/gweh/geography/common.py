from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable

import geopandas as gpd
import numpy as np
from pyproj import CRS, Geod, Transformer
from shapely import make_valid, segmentize
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import transform, unary_union


GEOD = Geod(ellps="WGS84")

_COUNTRY_ALIASES = {
    "uk": "united kingdom",
    "great britain": "united kingdom",
    "venezuela bolivarian republic of": "venezuela",
    "viet nam": "vietnam",
    "korea rep": "south korea",
    "republic of korea": "south korea",
    "russian federation": "russia",
    "turkiye": "turkey",
    "united states of america": "united states",
    "us": "united states",
    "usa": "united states",
    "korea republic of": "south korea",
    "aland islands": "aland",
}

_ADMIN1_ALIASES = {
    "apulia": "puglia",
    "puglia": "puglia",
    "guangxi zhuang": "guangxi",
    "guangxi zhuang autonomous region": "guangxi",
}

_ADMIN_NAME_FIELDS = (
    "ADMIN",
    "NAME",
    "NAME_LONG",
    "SOVEREIGNT",
    "BRK_NAME",
    "FORMAL_EN",
)

_EEZ_NAME_FIELDS = (
    "TERRITORY1",
    "SOVEREIGN1",
    "TERRITORY2",
    "SOVEREIGN2",
    "TERRITORY3",
    "SOVEREIGN3",
    "GEONAME",
    "ISO_TER1",
    "ISO_SOV1",
    "ISO_TER2",
    "ISO_SOV2",
)

_ADMIN1_NAME_FIELDS = (
    # Feature-level names/codes.
    "name_en", "name", "nameascii", "gn_name", "woe_name", "postal", "iso_3166_2",
    # Natural Earth also uses grouping fields when the polygon layer is finer
    # than the first-order unit used by the study (e.g. Italian provinces
    # grouped into regions). These are legitimate Admin-1 aliases, not a
    # fallback to fuzzy spatial matching.
    "region", "region_big", "region_sub",
    "NAME_EN", "NAME", "NAMEASCII", "GN_NAME", "WOE_NAME", "POSTAL", "ISO_3166_2",
    "REGION", "REGION_BIG", "REGION_SUB",
)
_ADMIN1_COUNTRY_FIELDS = (
    "admin", "geonunit", "adm0_name", "sovereignt",
    "ADMIN", "GEONUNIT", "ADM0_NAME", "SOVEREIGNT",
)


def normalize_country_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return _COUNTRY_ALIASES.get(text, text)


def normalize_admin1_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return _ADMIN1_ALIASES.get(text, text)


def _polygon_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    geometry = make_valid(geometry)
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        parts = []
        for item in geometry.geoms:
            parts.extend(_polygon_parts(item))
        return parts
    return []


def clean_polygonal(geometry):
    parts = _polygon_parts(geometry)
    if not parts:
        return None
    merged = unary_union(parts)
    if isinstance(merged, Polygon):
        return merged
    if isinstance(merged, MultiPolygon):
        return merged
    return unary_union(_polygon_parts(merged))


def clean_geometry(geometry):
    if geometry is None or geometry.is_empty:
        return None
    return make_valid(geometry)


def union_geometries(geometries: Iterable):
    items = [item for item in geometries if item is not None and not item.is_empty]
    if not items:
        return None
    return unary_union(items)


AREA_MAX_SEGMENT_DEG = 0.05


def geodesic_area_km2(geometry) -> float:
    """Additive WGS84 area after deterministic ring densification."""
    total = 0.0
    for part in _polygon_parts(geometry):
        dense = segmentize(part, max_segment_length=AREA_MAX_SEGMENT_DEG)
        oriented = orient(dense, sign=1.0)
        area, _ = GEOD.geometry_area_perimeter(oriented)
        total += abs(float(area)) / 1.0e6
    return total


def local_aeqd_crs(geometry) -> CRS:
    """Azimuthal equidistant CRS centred on the geometry centroid.

    The centroid is used rather than a representative point so that buffers,
    morphological closing and simplification are centred consistently.
    """
    point = geometry.centroid
    if point.is_empty:
        point = geometry.representative_point()
    return CRS.from_proj4(
        f"+proj=aeqd +lat_0={point.y:.10f} +lon_0={point.x:.10f} +datum=WGS84 +units=m +no_defs"
    )


def project_geometry(geometry, src: CRS | str, dst: CRS | str):
    transformer = Transformer.from_crs(src, dst, always_xy=True)
    return transform(transformer.transform, geometry)


def buffer_km(geometry, distance_km: float):
    if geometry is None or geometry.is_empty:
        return None
    local = local_aeqd_crs(geometry)
    projected = project_geometry(geometry, "EPSG:4326", local)
    buffered = projected.buffer(float(distance_km) * 1000.0)
    return clean_polygonal(project_geometry(buffered, local, "EPSG:4326"))


def coastal_band(source_geometry, distance_km: float, envelope=None):
    """Band of the given width around the production coastline geometry.

    The local base window remains the deterministic baseline. At high latitude an
    additional longitude-aware window may admit coastline segments that the
    degree-based base window would miss. Those supplemental segments are
    buffered separately and unioned onto the base band, so the correction is
    monotonic: widening preselection can add support but cannot remove support
    already produced by the base window.
    """
    if source_geometry is None or float(distance_km) <= 0:
        return None
    if envelope is None or envelope.is_empty:
        return buffer_km(source_geometry, float(distance_km))

    west, south, east, north = envelope.bounds
    distance_km = float(distance_km)
    base_margin = min(10.0, max(2.0, distance_km / 50.0))
    base_window = box(
        west - base_margin,
        south - base_margin,
        east + base_margin,
        north + base_margin,
    )
    base_source = source_geometry.intersection(base_window)
    base_band = buffer_km(base_source, distance_km)

    lat_margin = max(base_margin, distance_km / 111.32)
    edge_latitude = min(89.0, max(abs(south - lat_margin), abs(north + lat_margin)))
    lon_scale = max(0.05, math.cos(math.radians(edge_latitude)))
    lon_margin = max(base_margin, distance_km / (111.32 * lon_scale))
    if lat_margin <= base_margin and lon_margin <= base_margin:
        return base_band

    expanded_window = box(
        west - lon_margin,
        south - lat_margin,
        east + lon_margin,
        north + lat_margin,
    )
    expanded_source = source_geometry.intersection(expanded_window)
    supplemental_source = expanded_source.difference(base_window)
    supplemental_band = buffer_km(supplemental_source, distance_km)

    if base_band is None or base_band.is_empty:
        return supplemental_band
    if supplemental_band is None or supplemental_band.is_empty:
        return base_band
    return clean_polygonal(unary_union([base_band, supplemental_band]))


def boxes_union(boxes: list[list[float]]):
    if not boxes:
        raise ValueError("At least one bounding box is required")
    geometries = []
    for values in boxes:
        if len(values) != 4:
            raise ValueError(f"Invalid bounding box: {values}")
        west, south, east, north = map(float, values)
        if not (-180.0 <= west < east <= 180.0 and -90.0 <= south < north <= 90.0):
            raise ValueError(f"Invalid bounding box: {values}")
        geometries.append(box(west, south, east, north))
    return clean_polygonal(unary_union(geometries))


def name_key_fields(frame: gpd.GeoDataFrame, kind: str) -> list[str]:
    preferred = _ADMIN_NAME_FIELDS if kind == "admin0" else _EEZ_NAME_FIELDS
    return [field for field in preferred if field in frame.columns]


def add_match_keys(frame: gpd.GeoDataFrame, kind: str) -> gpd.GeoDataFrame:
    fields = name_key_fields(frame, kind)
    if not fields:
        raise ValueError(f"No country-name fields found in {kind} geography")
    out = frame.copy()

    def keys_for_row(row) -> str:
        keys = []
        for field in fields:
            value = normalize_country_name(row.get(field, ""))
            if value and value not in keys:
                keys.append(value)
        return ";".join(keys)

    out["match_keys"] = out.apply(keys_for_row, axis=1)
    return out




def _casefold_fields(frame: gpd.GeoDataFrame, preferred: tuple[str, ...]) -> list[str]:
    lookup = {str(column).casefold(): str(column) for column in frame.columns}
    out = []
    for field in preferred:
        actual = lookup.get(str(field).casefold())
        if actual and actual not in out:
            out.append(actual)
    return out


def add_admin1_match_keys(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add normalized first-order administrative and parent-country keys.

    Natural Earth Admin-1 schemas vary slightly between releases, so matching is
    deliberately case-insensitive and accepts the common `name`/`name_en` and
    `admin`/`geonunit` field families.
    """
    name_fields = _casefold_fields(frame, _ADMIN1_NAME_FIELDS)
    country_fields = _casefold_fields(frame, _ADMIN1_COUNTRY_FIELDS)
    if not name_fields:
        raise ValueError("No Admin-1 name fields found in first-order geography")
    if not country_fields:
        raise ValueError("No Admin-1 parent-country fields found in first-order geography")
    out = frame.copy()

    def keys(row, fields, normalizer):
        values = []
        for field in fields:
            value = normalizer(row.get(field, ""))
            if value and value not in values:
                values.append(value)
        return ";".join(values)

    out["admin1_match_keys"] = out.apply(lambda row: keys(row, name_fields, normalize_admin1_name), axis=1)
    out["admin0_match_keys"] = out.apply(lambda row: keys(row, country_fields, normalize_country_name), axis=1)
    return out


def admin1_match_mask(frame: gpd.GeoDataFrame, country: str, names: list[str]) -> np.ndarray:
    if "admin1_match_keys" not in frame.columns or "admin0_match_keys" not in frame.columns:
        raise ValueError("Admin-1 geography lacks normalized match keys")
    country_key = normalize_country_name(country)
    wanted = {normalize_admin1_name(name) for name in names if str(name).strip()}
    if not country_key or not wanted:
        return np.zeros(len(frame), dtype=bool)

    def row_matches(row) -> bool:
        parent = {item for item in str(row["admin0_match_keys"] or "").split(";") if item}
        unit = {item for item in str(row["admin1_match_keys"] or "").split(";") if item}
        return country_key in parent and bool(unit & wanted)

    return frame.apply(row_matches, axis=1).to_numpy(dtype=bool)


def resolve_admin1_declarations(frame: gpd.GeoDataFrame, declarations: list[dict]) -> dict[str, int]:
    resolution = {}
    for declaration in declarations or []:
        country = str(declaration.get("country", "")).strip()
        names = [str(value).strip() for value in declaration.get("names", []) if str(value).strip()]
        for name in names:
            key = f"{country}::{name}"
            resolution[key] = int(admin1_match_mask(frame, country, [name]).sum())
    return resolution


def union_admin1_geometry(frame: gpd.GeoDataFrame, declarations: list[dict]):
    selected_parts = []
    for declaration in declarations or []:
        country = str(declaration.get("country", "")).strip()
        names = [str(value).strip() for value in declaration.get("names", []) if str(value).strip()]
        mask = admin1_match_mask(frame, country, names)
        if mask.any():
            selected_parts.extend(frame.loc[mask, "geometry"].tolist())
    return clean_polygonal(unary_union(selected_parts)) if selected_parts else None

def country_match_mask(frame: gpd.GeoDataFrame, names: list[str]) -> np.ndarray:
    wanted = [normalize_country_name(name) for name in names if str(name).strip()]
    if not wanted:
        return np.ones(len(frame), dtype=bool)
    if "match_keys" not in frame.columns:
        raise ValueError("Geography table lacks match_keys")

    def row_matches(value: object) -> bool:
        keys = {item for item in str(value or "").split(";") if item}
        return any(wanted_name in keys for wanted_name in wanted)

    return frame["match_keys"].map(row_matches).to_numpy(dtype=bool)


def resolved_country_keys(frame: gpd.GeoDataFrame, names: list[str]) -> list[str]:
    """Resolve declared jurisdictions to equivalent source territory names.

    Marine Regions rows can contain several territory/sovereign pairs. When a
    declared name matches one pair, only that pair is expanded; unrelated
    co-claimants in the same row are not admitted to the membership claim.
    """
    wanted = {normalize_admin1_name(name) for name in names if str(name).strip()}
    if not wanted:
        return []

    keys = set()
    has_eez_pairs = any(field in frame.columns for field in ("TERRITORY1", "SOVEREIGN1"))
    if has_eez_pairs:
        pair_fields = [
            ("TERRITORY1", "SOVEREIGN1"),
            ("TERRITORY2", "SOVEREIGN2"),
            ("TERRITORY3", "SOVEREIGN3"),
        ]
        for row in frame.to_dict(orient="records"):
            for territory_field, sovereign_field in pair_fields:
                pair = {
                    normalize_country_name(row.get(territory_field, "")),
                    normalize_country_name(row.get(sovereign_field, "")),
                }
                pair.discard("")
                if pair & wanted:
                    keys.update(pair)
        return sorted(keys)

    selected = frame.loc[country_match_mask(frame, names)]
    for value in selected.get("match_keys", []).astype(str):
        keys.update(item for item in value.split(";") if item)
    return sorted(keys)


def union_country_geometry(frame: gpd.GeoDataFrame, names: list[str]):
    if not names:
        return clean_polygonal(unary_union(frame.geometry.tolist()))
    selected = frame.loc[country_match_mask(frame, names)]
    if selected.empty:
        return None
    return clean_polygonal(unary_union(selected.geometry.tolist()))
