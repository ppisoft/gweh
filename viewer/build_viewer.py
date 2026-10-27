#!/usr/bin/env python3
"""Generate the optional offline GWEH data viewer from a checked public dataset."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
import yaml
from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon, shape

MAP_WIDTH = 1200
MAP_HEIGHT = 610
VIEW_TOLERANCE_DEGREES = 0.07
MIN_VISIBLE_RING_AREA_PX2 = 0.12
INPUT_TABLES = (
    "GWEH_catalogue.csv",
    "GWEH_region_metrics.csv",
    "GWEH_market_components.csv",
    "GWEH_geometries.gpkg",
    "GWEH_grid_region_lookup.csv",
    "metadata/data_dictionary.csv",
    "metadata/publication.yaml",
)
BACKGROUND = Path(__file__).parent / "assets/natural_earth_110m_countries.geojson"
TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def sha256_bytes(content: bytes) -> str:
    """Return a content checksum independently of file metadata."""
    return hashlib.sha256(content).hexdigest()


def sha256_file(filename: Path) -> str:
    """Stream a file into a SHA-256 digest."""
    h = hashlib.sha256()
    with filename.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _manifest(content: bytes) -> dict[str, str]:
    """Parse the authoritative public manifest, rejecting invalid keys."""
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    found = {}
    for row in rows:
        key, digest = row.get("file", ""), row.get("sha256", "")
        if not key or not digest or key in found or key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("Invalid, duplicated, or unsafe dataset manifest entry")
        found[key] = digest
    if not found:
        raise ValueError("Dataset manifest is empty")
    return found


def _input_names(manifest: dict[str, str]) -> tuple[str, ...]:
    """Find exactly one grid NetCDF without assuming its model or dimensions."""
    grids = sorted(name for name in manifest if name.startswith("GWEH_grid_") and name.endswith(".nc") and "/" not in name)
    if len(grids) != 1:
        raise ValueError("Exactly one public GWEH_grid_*.nc is required")
    for name in INPUT_TABLES:
        if name not in manifest:
            raise ValueError(f"Required dataset member is not in manifest: {name}")
    return INPUT_TABLES + (grids[0],)


@contextmanager
def dataset_files(source: Path):
    """Load manifest-verified members from either ZIP or extracted data tree."""
    source = source.resolve()
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(prefix="gweh_viewer_") as scratch:
            prefix = "GWEH_dataset/"
            try:
                manifest = _manifest(archive.read(prefix + "MANIFEST_SHA256.csv"))
            except KeyError as exc:
                raise ValueError("Dataset ZIP has no public manifest") from exc
            names = _input_names(manifest)
            files = {}
            for name in names:
                try:
                    content = archive.read(prefix + name)
                except KeyError as exc:
                    raise ValueError(f"Dataset member missing: {name}") from exc
                if sha256_bytes(content) != manifest[name]:
                    raise ValueError(f"Public checksum mismatch: {name}")
                if name.endswith((".gpkg", ".nc")):
                    file = Path(scratch) / Path(name).name
                    file.write_bytes(content)
                    files[name] = file
                else:
                    files[name] = io.BytesIO(content)
            yield files, manifest, names
    elif source.is_dir():
        root = source / "GWEH_dataset" if (source / "GWEH_dataset").is_dir() else source
        try:
            manifest = _manifest((root / "MANIFEST_SHA256.csv").read_bytes())
        except FileNotFoundError as exc:
            raise ValueError("Dataset directory has no public manifest") from exc
        names = _input_names(manifest)
        files = {}
        for name in names:
            path = root / name
            if sha256_file(path) != manifest[name]:
                raise ValueError(f"Public checksum mismatch: {name}")
            files[name] = path
        yield files, manifest, names
    else:
        raise ValueError("--dataset must be an extracted public directory or dataset ZIP")


def scalar(value):
    """Convert pandas/numpy scalars to strict JSON-compatible data."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def json_for_script(value) -> str:
    """Emit inert JSON suitable for embedding in an HTML script element."""
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


class Robinson:
    """Single, consistent Robinson projection for land, region polygons, and grid."""

    def __init__(self):
        crs = CRS.from_proj4("+proj=robin +lon_0=0 +datum=WGS84 +units=m +no_defs +type=crs")
        self.forward = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)
        xmin = self.forward.transform(-180, 0)[0]
        xmax = self.forward.transform(180, 0)[0]
        ymin = self.forward.transform(0, -90)[1]
        ymax = self.forward.transform(0, 90)[1]
        self.xmin, self.width = xmin, xmax - xmin
        self.ymax, self.height = ymax, ymax - ymin
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Robinson projection has invalid bounds")

    def point(self, lon, lat):
        x, y = self.forward.transform(lon, lat)
        xx = (np.asarray(x, dtype=float) - self.xmin) * MAP_WIDTH / self.width
        yy = (self.ymax - np.asarray(y, dtype=float)) * MAP_HEIGHT / self.height
        if not np.isfinite(xx).all() or not np.isfinite(yy).all():
            raise ValueError("Non-finite projected coordinates")
        return xx, yy


def _ring(coords, projection: Robinson, minimum_area=MIN_VISIBLE_RING_AREA_PX2):
    """Remove snapped duplicates and invisible SVG rings in display coordinates."""
    values = np.asarray(coords, dtype=float)
    x, y = projection.point(values[:, 0], values[:, 1])
    snapped = [(round(float(a), 2), round(float(b), 2)) for a, b in zip(x, y)]
    unique = []
    for point in snapped:
        if not unique or unique[-1] != point:
            unique.append(point)
    if len(unique) > 1 and unique[-1] == unique[0]:
        unique.pop()
    if len(unique) < 3 or len(set(unique)) < 3:
        return None
    points = np.asarray(unique)
    area = abs(np.dot(points[:, 0], np.roll(points[:, 1], -1))
               - np.dot(points[:, 1], np.roll(points[:, 0], -1))) / 2
    if area < minimum_area:
        return None
    return "M" + "L".join(f"{a:g},{b:g}" for a, b in unique) + "Z"


def polygon_path(geometry, projection: Robinson) -> str:
    """Simplify display-only polygons without changing any canonical GWEH geometry."""
    if not isinstance(geometry, (Polygon, MultiPolygon)):
        raise ValueError("GWEH geometries must be polygons")
    simplified = geometry.simplify(VIEW_TOLERANCE_DEGREES, preserve_topology=True)
    polygons = [simplified] if isinstance(simplified, Polygon) else list(simplified.geoms)
    rings = []
    for polygon in polygons:
        exterior = _ring(polygon.exterior.coords, projection)
        if exterior is None:
            continue
        rings.append(exterior)
        for interior in polygon.interiors:
            hole = _ring(interior.coords, projection)
            if hole is not None:
                rings.append(hole)
    if not rings:
        # Extremely small regions still need a clickable target at global extent.
        point = geometry.representative_point()
        x, y = projection.point(point.x, point.y)
        px, py = float(x), float(y)
        rings.append(f"M{px-0.65:.2f},{py-0.65:.2f}L{px+0.65:.2f},{py-0.65:.2f}"
                     f"L{px+0.65:.2f},{py+0.65:.2f}L{px-0.65:.2f},{py+0.65:.2f}Z")
    return " ".join(rings)


def background_paths(projection: Robinson):
    """Project the bundled public-domain Natural Earth country polygons."""
    geojson = json.loads(BACKGROUND.read_text(encoding="utf-8"))
    land, borders = [], []
    for feature in geojson["features"]:
        geom = shape(feature["geometry"])
        polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms)
        for poly in polys:
            ring = _ring(poly.exterior.coords, projection, minimum_area=0.05)
            if ring:
                land.append(ring)
                borders.append(ring)
    return " ".join(land), " ".join(borders)


def grid_edges(values):
    """Return cell bounds from monotonically increasing center coordinates."""
    a = np.asarray(values, dtype=float)
    if a.ndim != 1 or a.size < 2 or not np.isfinite(a).all() or not (np.diff(a) > 0).all():
        raise ValueError("Grid latitude/longitude must be finite ascending 1-D coordinates")
    return np.r_[a[0] - (a[1] - a[0]) / 2, (a[1:] + a[:-1]) / 2,
                 a[-1] + (a[-1] - a[-2]) / 2]


def cell_path(lon0, lon1, lat0, lat1, projection: Robinson):
    """Draw one cell, splitting at the antimeridian into subpaths if necessary."""
    if lon0 < 180 < lon1:
        parts = [(lon0, 180), (-180, lon1-360)]
    elif lon0 < -180 < lon1:
        parts = [(lon0 + 360, 180), (-180, lon1)]
    else:
        # Normalize convention 0…360 while keeping the cell continuous.
        shift = 360 if lon0 >= 180 else 0
        parts = [(lon0-shift, lon1-shift)]
    subpaths = []
    for lo, hi in parts:
        if hi <= lo:
            continue
        x, y = projection.point([lo, hi, hi, lo], [lat0, lat0, lat1, lat1])
        subpaths.append("M" + "L".join(f"{float(a):.2f},{float(b):.2f}" for a, b in zip(x, y)) + "Z")
    return " ".join(subpaths)


def glossary(data_dictionary: pd.DataFrame) -> dict[str, str]:
    """Read descriptions of regional indicators for the detail panel."""
    required = {"table", "field", "definition"}
    if not required.issubset(data_dictionary):
        raise ValueError("Data dictionary lacks mandatory metadata columns")
    definitions = {}
    for table_name in ("catalogue", "region_metrics", "market_components"):
        for row in data_dictionary.loc[data_dictionary.table.eq(table_name)].itertuples(index=False):
            definitions.setdefault(str(row.field), str(row.definition))
    # Short domain explanation where the dictionary only supplies a field label.
    definitions["market_maturity"] = (
        "Market class (separate from selection): CORE has substantial operating capacity "
        "and deployment evidence; EMERGING has some deployment or a strong project pipeline; "
        "PROSPECTIVE comprises other candidates. See the deposited selection rules.")
    definitions["selection_pathway"] = (
        "How the candidate entered the evidence-selected panel: primary resource, "
        "primary market/project/resource, absolute market/resource, or not selected.")
    definitions["climate_analysis_eligible"] = (
        "Selected region with at least two grid-cell centres in the specific reference grid. "
        "Not a condition for vector evidence selection.")
    definitions["primary_climate_panel"] = (
        "Selected region represented by at least 12 cell centres on the current model grid; "
        "different from the primary evidence-selection pathway.")
    definitions["market_components"] = (
        "Onshore/offshore project components used by the market peer groups, including mixed regions.")
    return definitions


def generate(source: Path, output: Path, record_path: Path | None = None):
    """Build a deterministic standalone viewer and optional audit record."""
    if not BACKGROUND.is_file():
        raise FileNotFoundError(f"Missing bundled Natural Earth background: {BACKGROUND}")
    with dataset_files(source) as (files, manifest, names):
        catalogue = pd.read_csv(files["GWEH_catalogue.csv"])
        metrics = pd.read_csv(files["GWEH_region_metrics.csv"])
        components = pd.read_csv(files["GWEH_market_components.csv"])
        lookup = pd.read_csv(files["GWEH_grid_region_lookup.csv"])
        dictionary = pd.read_csv(files["metadata/data_dictionary.csv"])
        publication = yaml.safe_load(
            files["metadata/publication.yaml"].read_text(encoding="utf-8")
            if isinstance(files["metadata/publication.yaml"], Path)
            else files["metadata/publication.yaml"].getvalue().decode("utf-8")
        )
        geometry = gpd.read_file(files["GWEH_geometries.gpkg"], layer="regions")
        if geometry.crs is None or geometry.crs.to_epsg() != 4326:
            raise ValueError("GeoPackage region layer must be EPSG:4326")
        if not geometry.is_valid.all() or geometry.geometry.is_empty.any():
            raise ValueError("GeoPackage region geometries must be valid and non-empty")
        frames = (catalogue, metrics, geometry)
        if not all(len(f) > 0 and f.candidate_id.is_unique for f in frames):
            raise ValueError("Public data have empty or duplicate candidate IDs")
        candidates = set(catalogue.candidate_id)
        if any(set(f.candidate_id) != candidates for f in frames):
            raise ValueError("Candidate IDs differ among metrics, catalogue and geometries")
        if not lookup.grid_id.is_unique or not lookup.candidate_id.is_unique:
            raise ValueError("Grid lookup is not uniquely keyed")
        if not set(lookup.candidate_id).issubset(candidates):
            raise ValueError("Grid lookup references unknown candidate IDs")
        combined = geometry.merge(
            catalogue.drop(columns=["region_name", "macroregion", "surface_type"], errors="ignore"),
            on="candidate_id", validate="one_to_one"
        ).merge(
            metrics.drop(columns=["region_name", "macroregion", "surface_type"], errors="ignore"),
            on="candidate_id", validate="one_to_one"
        )
        component_groups = {}
        for cid, subset in components.groupby("candidate_id"):
            component_groups[str(cid)] = [
                {key: scalar(value) for key, value in row.items()
                 if key not in ("region_name", "candidate_surface_type")}
                for row in subset.to_dict("records")
            ]
        projection = Robinson()
        columns = list(catalogue.columns) + [
            key for key in metrics if key not in ("candidate_id", "region_name", "macroregion", "surface_type")
        ]
        shapes, labels, anchors, records = [], [], [], []
        for _, region in combined.iterrows():
            cid = str(region.candidate_id)
            escaped = html.escape(cid, quote=True)
            record = {field: scalar(region.get(field)) for field in columns}
            record["market_components"] = component_groups.get(cid, [])
            records.append(record)
            shapes.append(f'<path class="region" data-id="{escaped}" d="{polygon_path(region.geometry, projection)}" fill-rule="evenodd"/>')
            location = region.geometry.representative_point()
            x, y = projection.point(location.x, location.y)
            anchors.append(f'<circle class="anchor" data-id="{escaped}" cx="{float(x):.2f}" cy="{float(y):.2f}" r="2.5"/>')
            labels.append(f'<text class="region-label" data-id="{escaped}" x="{float(x):.2f}" y="{float(y):.2f}">{html.escape(str(region.region_name))}</text>')

        grid_name = next(name for name in names if name.endswith(".nc"))
        with xr.open_dataset(files[grid_name], decode_cf=False, mask_and_scale=False) as nc:
            ids = np.asarray(nc["gweh_region_id"].values)
            lon, lat = np.asarray(nc.lon.values), np.asarray(nc.lat.values)
            if ids.shape != (len(lat), len(lon)):
                raise ValueError("NetCDF region IDs do not match grid coordinate dimensions")
            if not np.issubdtype(ids.dtype, np.integer):
                raise ValueError("NetCDF grid region IDs must be integers")
            lon_edges, lat_edges = grid_edges(lon), grid_edges(lat)
            mapped = dict(zip(lookup.grid_id.astype(int), lookup.candidate_id.astype(str)))
            cells, cell_counts = [], {cid: 0 for cid in mapped.values()}
            for i, j in np.argwhere(ids > 0):
                gid = int(ids[i, j])
                if gid not in mapped:
                    raise ValueError(f"Grid ID {gid} not present in public lookup")
                cid = mapped[gid]
                path = cell_path(lon_edges[j], lon_edges[j+1], lat_edges[i], lat_edges[i+1], projection)
                cells.append(f'<path class="grid-cell" data-id="{html.escape(cid, quote=True)}" d="{path}"/>')
                cell_counts[cid] += 1
            for row in lookup.itertuples(index=False):
                if cell_counts[str(row.candidate_id)] != int(row.cell_count):
                    raise ValueError(f"Grid lookup count does not match NetCDF: {row.candidate_id}")
            if len(cells) != int(lookup.cell_count.sum()):
                raise ValueError("Active grid-cell total differs from lookup sum")
        selected = sum(r["evidence_selection_status"] == "EVIDENCE_SELECTED" for r in records)
        eligible = sum(int(r["climate_analysis_eligible"]) == 1 for r in records)
        primary = sum(int(r["primary_climate_panel"]) == 1 for r in records)
        land, borders = background_paths(projection)
        version = str(publication.get("product_version", "unknown"))
        provenance = {
            "dataset_version": version,
            "projection": "Robinson",
            "background": "Natural Earth 110m low-resolution countries (public domain)",
            "background_sha256": sha256_file(BACKGROUND),
            "file_sha256": {name: manifest[name] for name in names},
        }
        template = TEMPLATE.read_text(encoding="utf-8")
        substitutions = {
            "__BACKGROUND__": land,
            "__COUNTRIES__": borders,
            "__REGIONS__": "\n".join(shapes),
            "__GRID__": "\n".join(cells),
            "__DOTS__": "\n".join(anchors),
            "__LABELS__": "\n".join(labels),
            "__RECORDS__": json_for_script(records),
            "__GLOSSARY__": json_for_script(glossary(dictionary)),
            "__COUNTS__": json_for_script({"candidates": len(records), "selected": selected,
                                           "eligible": eligible, "primary": primary, "cells": len(cells)}),
            "__PROVENANCE__": json_for_script(provenance),
            "__VERSION__": html.escape(version),
            "__GRID_DESCRIPTION__": html.escape(f"{len(lat)} × {len(lon)} grid"),
            "__GRID_FILENAME__": html.escape(grid_name),
            "__CANDIDATE_COUNT__": str(len(records)),
        }
        for marker, content in substitutions.items():
            if template.count(marker) < 1:
                raise ValueError(f"Missing viewer template marker: {marker}")
            template = template.replace(marker, content)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(template, encoding="utf-8")
        output_hash = sha256_file(output)
        report = {
            "status": "PASS",
            "viewer_sha256": output_hash,
            "template_sha256": sha256_file(TEMPLATE),
            "generator_sha256": sha256_file(Path(__file__)),
            "background_sha256": sha256_file(BACKGROUND),
            "input_sha256": provenance["file_sha256"],
            "candidate_count": len(records), "evidence_selected": selected,
            "climate_eligible": eligible, "primary_climate_panel": primary,
            "active_grid_cell_count": len(cells),
            "viewer_size_bytes": output.stat().st_size,
            "dataset_version": version,
        }
        if record_path:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Created {output}: {len(records)} regions, {len(cells)} grid cells, {output.stat().st_size:,} bytes")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="Public GWEH dataset ZIP or directory")
    parser.add_argument("--out", type=Path, default=Path("GWEH_explorer.html"))
    parser.add_argument("--record", type=Path, help="Optional reproducibility record JSON")
    args = parser.parse_args()
    generate(args.dataset, args.out, args.record)


if __name__ == "__main__":
    main()
