#!/usr/bin/env python3
"""Independent MERRA-2 corroboration for the current GWEH candidate polygons.

prepare:  make outcome-blind geometry and a separate candidate-ID mapping
aggregate: aggregate hourly MERRA-2 over the blind polygons, checkpointed by year
compare:   join completed blind aggregates to current GWA4 metrics and selection tiers

The candidate population is always read from the supplied GWEH run. No
MERRA-derived value changes GWEH geometry, scores or selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from scipy.stats import spearmanr

RD = 287.05
DATE_TOKEN = re.compile(r"(?<!\d)((?:19|20)\d{6})(?!\d)")
METRICS = ("ws50", "wpd50", "ws10")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _new_directory(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Choose a new output directory: {path}")
    path.mkdir(parents=True)


def _unique(path: Path, cols: set[str]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"candidate_id": str})
    missing = cols - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    if df.candidate_id.isna().any() or df.candidate_id.duplicated().any():
        raise ValueError(f"{path}: missing or duplicated candidate_id")
    return df


def _boolean(series: pd.Series, label: str) -> pd.Series:
    """Parse explicit 0/1 or true/false flags; never silently coerce missing data."""
    value = series.astype("string").str.strip().str.lower()
    accepted = {"1": True, "0": False, "true": True, "false": False}
    invalid = ~value.isin(accepted)
    if invalid.any():
        bad = sorted(set(value.loc[invalid].fillna("<missing>").tolist()))
        raise ValueError(f"Invalid or missing {label} flags: {bad}")
    return value.map(accepted).astype(bool)


def prepare(run_dir: Path, output: Path, seed: int = 0) -> dict:
    """Export blind polygons; never pass GWA or selection metadata to aggregation."""
    cat = _unique(run_dir / "08_catalogue/catalogue.csv", {"candidate_id"})
    polygons = gpd.read_parquet(run_dir / "05_exclusive/exclusive_regions.parquet")
    if polygons.crs is None:
        raise ValueError("Stage05 polygons have no CRS")
    if (polygons.candidate_id.isna().any() or polygons.candidate_id.duplicated().any()
            or set(cat.candidate_id) != set(polygons.candidate_id.astype(str))):
        raise ValueError("Stage05 polygons and Stage08 candidate roster differ")
    if polygons.geometry.isna().any() or polygons.geometry.is_empty.any():
        raise ValueError("Empty candidate geometry")
    candidate_ids = sorted(cat.candidate_id.tolist())
    shuffled = list(candidate_ids)
    random.Random(seed).shuffle(shuffled)
    mapping = {cid: f"MV-{i:05d}" for i, cid in enumerate(shuffled, 1)}
    blind = polygons[["candidate_id", "geometry"]].to_crs("EPSG:4326").copy()
    blind["validation_id"] = blind.candidate_id.astype(str).map(mapping)
    blind = blind[["validation_id", "geometry"]].sort_values("validation_id")

    _new_directory(output)
    (output / "blind").mkdir()
    (output / "supervisor").mkdir()
    blind.to_file(output / "blind/regions.gpkg", layer="regions", driver="GPKG", index=False)
    pd.DataFrame({"validation_id": sorted(mapping.values())}).to_csv(
        output / "blind/roster.csv", index=False
    )
    pd.DataFrame({"candidate_id": candidate_ids,
                  "validation_id": [mapping[c] for c in candidate_ids]}).to_csv(
        output / "supervisor/mapping.csv", index=False
    )
    info = {
        "method": "outcome_blind_regional_merra2_resource_aggregation",
        "candidate_count": len(candidate_ids),
        "geometry_file": "blind/regions.gpkg",
        "spatial_rule": "MERRA_grid_cell_center_within_region_polygon",
        "weighting": "cos_latitude_and_valid_hourly_samples",
        "source_run": run_dir.name,
        "note": "Only blind/ is passed to aggregate; supervisor mapping is used after aggregation.",
    }
    (output / "README.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def _date_from_filename(name: str) -> date | None:
    match = DATE_TOKEN.search(name)
    if not match:
        return None
    try:
        return date.fromisoformat(f"{match[1][:4]}-{match[1][4:6]}-{match[1][6:8]}")
    except ValueError:
        return None


def _daily_files(root: Path, start: date, end: date) -> dict[date, Path]:
    if end < start:
        raise ValueError("End of MERRA period precedes start")
    files = {}
    for path in sorted((*root.rglob("*.nc"), *root.rglob("*.nc4"))):
        when = _date_from_filename(path.name)
        if when is None or not start <= when <= end:
            continue
        if when in files:
            raise ValueError(f"Two MERRA files for {when}: {files[when]} and {path}")
        files[when] = path
    current = start
    absent = []
    while current <= end:
        if current not in files:
            absent.append(current.isoformat())
        current += timedelta(days=1)
    if absent:
        raise ValueError(f"Missing {len(absent)} MERRA daily files; first missing: {absent[:8]}")
    return files


def _coords(ds: xr.Dataset) -> tuple[np.ndarray, np.ndarray]:
    names = set(ds.variables)
    y = "lat" if "lat" in names else "latitude"
    x = "lon" if "lon" in names else "longitude"
    if y not in names or x not in names:
        raise ValueError("MERRA file lacks latitude/longitude coordinate")
    lat = np.asarray(ds[y].values, dtype=float)
    lon = np.asarray(ds[x].values, dtype=float)
    if lat.ndim != 1 or lon.ndim != 1:
        raise ValueError("MERRA coordinates must be 1-D")
    return lat, lon


def _tlatlon(ds: xr.Dataset, name: str, shape: tuple[int, int, int]) -> np.ndarray:
    if name not in ds:
        raise ValueError(f"Missing MERRA variable {name}")
    data = ds[name]
    dims = list(data.dims)
    axis = [next((d for d in dims if d.lower() in aliases), None)
            for aliases in ({"time"}, {"lat", "latitude"}, {"lon", "longitude"})]
    if None in axis or len(dims) != 3 or len(set(axis)) != 3:
        raise ValueError(f"{name}: expected time/lat/lon dimensions, found {dims}")
    x = np.asarray(data.transpose(*axis).values, dtype=np.float64)
    if x.shape != shape:
        raise ValueError(f"{name}: shape {x.shape} differs from {shape}")
    x[~np.isfinite(x)] = np.nan
    return x


def _grid_membership(blind_dir: Path, lat: np.ndarray, lon: np.ndarray):
    regions = gpd.read_file(blind_dir / "regions.gpkg", layer="regions").to_crs("EPSG:4326")
    if (regions.validation_id.isna().any() or regions.validation_id.duplicated().any()
            or len(regions) == 0):
        raise ValueError("Invalid blind region identifiers")
    ids = sorted(regions.validation_id.astype(str))
    roster = pd.read_csv(blind_dir / "roster.csv", dtype=str)
    if (list(roster.columns) != ["validation_id"]
            or roster.validation_id.isna().any()
            or roster.validation_id.duplicated().any()
            or set(roster.validation_id) != set(ids)):
        raise ValueError("Blind roster and geometries disagree")
    lon180 = np.where(lon > 180.0, lon - 360.0, lon)
    xx, yy = np.meshgrid(lon180, lat)
    pts = gpd.GeoDataFrame(
        {"flat": np.arange(xx.size)},
        geometry=gpd.points_from_xy(xx.ravel(), yy.ravel()), crs="EPSG:4326",
    )
    owner = np.full(len(pts), -1, dtype=int)
    lookup = {vid: i for i, vid in enumerate(ids)}
    for region in regions.itertuples(index=False):
        candidates = pts.iloc[np.asarray(pts.sindex.query(region.geometry, predicate="intersects"), dtype=int)]
        inside = candidates.loc[candidates.geometry.within(region.geometry), "flat"].to_numpy(dtype=int)
        i = lookup[str(region.validation_id)]
        if np.any((owner[inside] >= 0) & (owner[inside] != i)):
            raise ValueError("A MERRA cell centre lies within multiple GWEH polygons")
        owner[inside] = i
    selected = np.flatnonzero(owner >= 0)
    indices = owner[selected]
    weights = np.maximum(np.cos(np.deg2rad(yy.ravel()[selected])), 0.0)
    counts = np.bincount(indices, minlength=len(ids))
    return ids, selected, indices, weights, counts


def _accumulate(array: np.ndarray, flat: np.ndarray, gids: np.ndarray,
                weights: np.ndarray, nregion: int) -> tuple[np.ndarray, np.ndarray]:
    """Accumulate numerator/denominator, without averaging wind before cubing."""
    values = array.reshape(array.shape[0], -1)[:, flat]
    valid = np.isfinite(values)
    numerator_by_cell = np.nansum(values * weights[None, :], axis=0)
    denominator_by_cell = np.sum(valid * weights[None, :], axis=0)
    return (np.bincount(gids, weights=numerator_by_cell, minlength=nregion),
            np.bincount(gids, weights=denominator_by_cell, minlength=nregion))


def _file_signature(dated_paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for p in dated_paths:
        st = p.stat()
        h.update(f"{p.relative_to(root)}\t{st.st_size}\t{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def aggregate(blind_dir: Path, merra_root: Path, output: Path, *, start: date, end: date,
              hours_per_day: int = 24, overwrite_years=False, progress_every=100) -> dict:
    """One output per year; the next invocation resumes completed matching years."""
    files = _daily_files(merra_root, start, end)
    by_year: dict[int, list[tuple[date, Path]]] = {}
    for d, path in files.items():
        by_year.setdefault(d.year, []).append((d, path))
    for entries in by_year.values():
        entries.sort()
    first_file = files[min(files)]
    with xr.open_dataset(first_file, decode_times=False) as ds:
        lat, lon = _coords(ds)
        optional_10m = "U10M" in ds and "V10M" in ds
    ids, selected, groups, weights, counts = _grid_membership(blind_dir, lat, lon)
    if not len(selected):
        raise ValueError("No MERRA cell centres fall within any candidate geometry")
    output.mkdir(parents=True, exist_ok=True)
    context_file = output / "aggregation_context.json"
    context = {"blind_geometry_sha256": _sha256(blind_dir / "regions.gpkg"),
               "merra_root": str(merra_root.resolve()), "start": start.isoformat(),
               "end": end.isoformat(), "hours_per_day": hours_per_day,
               "optional_10m": optional_10m, "region_count": len(ids),
               "merra_grid_shape": [len(lat), len(lon)]}
    if context_file.exists():
        if json.loads(context_file.read_text()) != context:
            raise ValueError("Existing MERRA checkpoints have a different geometry/input period/grid contract")
    else:
        if list(output.glob("year_*.csv")):
            raise ValueError("Year checkpoints exist without aggregation_context.json")
        context_file.write_text(json.dumps(context, indent=2) + "\n")
    results = {"status": "COMPUTED", "years": [], "candidate_count": len(ids),
               "candidates_with_merra_centres": int(np.sum(counts > 0)),
               "candidate_count_no_merra_centres": int(np.sum(counts == 0))}
    nregion = len(ids)
    for year, entries in sorted(by_year.items()):
        dest = output / f"year_{year}.csv"
        signatures = _file_signature([f for _, f in entries], merra_root)
        if dest.exists() and not overwrite_years:
            old = pd.read_csv(dest)
            if (set(old.validation_id) != set(ids) or len(old) != nregion
                    or set(old.input_signature) != {signatures}
                    or set(old.year) != {year} or set(old.days_processed) != {len(entries)}):
                raise ValueError(f"{dest}: checkpoint differs from current inputs; remove it or use --overwrite-years")
            print(f"[resume] {year} ({len(entries)} days)", flush=True)
        else:
            totals = {key: np.zeros(nregion, dtype=np.float64) for key in
                      ("ws50_n", "ws50_d", "wpd50_n", "wpd50_d", "ws10_n", "ws10_d")}
            steps = 0
            for i, (when, path) in enumerate(entries, 1):
                with xr.open_dataset(path, decode_times=False) as ds:
                    y, x = _coords(ds)
                    if not (np.array_equal(y, lat) and np.array_equal(x, lon)):
                        raise ValueError(f"{path}: grid changed across MERRA files")
                    if "time" not in ds.dims or ds.sizes["time"] != hours_per_day:
                        raise ValueError(f"{path}: expected {hours_per_day} hourly records")
                    shape = (hours_per_day, len(lat), len(lon))
                    u = _tlatlon(ds, "U50M", shape)
                    v = _tlatlon(ds, "V50M", shape)
                    ws = np.sqrt(u * u + v * v)
                    del u, v
                    n, d = _accumulate(ws, selected, groups, weights, nregion)
                    totals["ws50_n"] += n; totals["ws50_d"] += d
                    ps = _tlatlon(ds, "PS", shape)
                    t = _tlatlon(ds, "T2M", shape)
                    q = _tlatlon(ds, "QV2M", shape)
                    good = (np.isfinite(ps) & np.isfinite(t) & np.isfinite(q)
                            & (ps > 0) & (t > 0) & (q >= 0))
                    rho = np.full(shape, np.nan, dtype=np.float64)
                    rho[good] = ps[good] / (RD * t[good] * (1 + 0.61 * q[good]))
                    wpd = 0.5 * rho * np.power(ws, 3)
                    n, d = _accumulate(wpd, selected, groups, weights, nregion)
                    totals["wpd50_n"] += n; totals["wpd50_d"] += d
                    if optional_10m:
                        u10 = _tlatlon(ds, "U10M", shape)
                        v10 = _tlatlon(ds, "V10M", shape)
                        n, d = _accumulate(np.sqrt(u10 * u10 + v10 * v10),
                                           selected, groups, weights, nregion)
                        totals["ws10_n"] += n; totals["ws10_d"] += d
                steps += hours_per_day
                if progress_every and i % progress_every == 0:
                    print(f"[{year}] {i}/{len(entries)} days", flush=True)
            frame = pd.DataFrame({"validation_id": ids, "year": year,
                                  "merra_center_cells": counts, "days_processed": len(entries),
                                  "time_steps": steps, "input_signature": signatures, **totals})
            tmp = dest.with_suffix(".partial")
            frame.to_csv(tmp, index=False)
            tmp.replace(dest)
            print(f"[done] MERRA {year}: {len(entries)} days, {steps} hours", flush=True)
        results["years"].append(year)
    return results


def _spearman(frame: pd.DataFrame, a="mean_wpd50_proxy", b="WPD100_mean_aw") -> dict:
    valid = frame[[a, b]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(valid) < 3 or valid[a].nunique() <= 1 or valid[b].nunique() <= 1:
        return {"n": int(len(valid)), "spearman_rho": None,
                "nominal_p_value_spatial_dependence_not_corrected": None}
    rho, p = spearmanr(valid[a], valid[b])
    ranks_a = valid[a].rank(method="average", ascending=False)
    ranks_b = valid[b].rank(method="average", ascending=False)
    displacement = (ranks_a - ranks_b).abs()
    return {"n": int(len(valid)), "spearman_rho": float(rho),
            "nominal_p_value_spatial_dependence_not_corrected": float(p),
            "median_absolute_rank_displacement": float(displacement.median()),
            "maximum_absolute_rank_displacement": float(displacement.max())}


def compare(annual_dir: Path, mapping_file: Path, catalogue: Path,
            metrics: Path, output: Path, *, roster: Path) -> dict:
    """Unblind only after annual MERRA aggregation has completed."""
    cat = _unique(catalogue, {"candidate_id", "evidence_selection_status"})
    met = _unique(metrics, {"candidate_id", "WPD100_mean_aw"})
    grid_roster = _unique(roster, {
        "candidate_id", "climate_analysis_eligible", "primary_climate_panel",
    })
    universe = set(cat.candidate_id)
    if universe != set(met.candidate_id) or universe != set(grid_roster.candidate_id):
        raise ValueError("Catalogue, grid roster and GWA metrics have different candidate IDs")
    if "climate_analysis_eligible" in cat or "primary_climate_panel" in cat:
        raise ValueError("Pass the raw Stage08 catalogue; climate tiers must come from --roster")
    grid_roster = grid_roster[["candidate_id", "climate_analysis_eligible", "primary_climate_panel"]].copy()
    grid_roster["climate_analysis_eligible"] = _boolean(
        grid_roster["climate_analysis_eligible"], "climate_analysis_eligible"
    )
    grid_roster["primary_climate_panel"] = _boolean(
        grid_roster["primary_climate_panel"], "primary_climate_panel"
    )
    roster_with_selection = grid_roster.merge(
        cat[["candidate_id", "evidence_selection_status"]], on="candidate_id", validate="one_to_one"
    )
    chosen_mask = roster_with_selection.evidence_selection_status.eq("EVIDENCE_SELECTED")
    if not roster_with_selection.evidence_selection_status.isin(
        ["EVIDENCE_SELECTED", "EVIDENCE_NOT_SELECTED"]
    ).all():
        raise ValueError("Invalid evidence_selection_status")
    if (roster_with_selection.primary_climate_panel &
        ~roster_with_selection.climate_analysis_eligible).any():
        raise ValueError("Primary climate regions must be climate eligible")
    if (roster_with_selection.climate_analysis_eligible & ~chosen_mask).any():
        raise ValueError("Only evidence-selected candidates can be climate eligible")
    mapping = pd.read_csv(mapping_file, dtype=str)
    if (set(mapping.columns) != {"candidate_id", "validation_id"}
            or mapping.candidate_id.isna().any() or mapping.validation_id.isna().any()
            or mapping.candidate_id.duplicated().any() or mapping.validation_id.duplicated().any()
            or set(mapping.candidate_id) != set(cat.candidate_id)):
        raise ValueError("Blind mapping and current candidate universe differ")
    context = json.loads((annual_dir / "aggregation_context.json").read_text())
    annual_files = sorted(annual_dir.glob("year_*.csv"))
    if not annual_files:
        raise ValueError("No independently aggregated MERRA annual checkpoints")
    annual = pd.concat([pd.read_csv(f, dtype={"validation_id": str}) for f in annual_files],
                       ignore_index=True)
    expected_years = set(range(date.fromisoformat(context["start"]).year,
                               date.fromisoformat(context["end"]).year + 1))
    if (set(annual.year.astype(int)) != expected_years or annual.duplicated(["validation_id", "year"]).any()
            or set(annual.validation_id) != set(mapping.validation_id)
            or len(annual) != len(mapping) * len(expected_years)):
        raise ValueError("MERRA annual checkpoints are incomplete or have duplicate IDs")
    # No year may silently contain only a partial time window, or a different
    # number of MERRA centres than neighbouring years.
    expected_days = {}
    day = date.fromisoformat(context["start"])
    last = date.fromisoformat(context["end"])
    while day <= last:
        expected_days[day.year] = expected_days.get(day.year, 0) + 1
        day += timedelta(days=1)
    for year, part in annual.groupby("year"):
        count = expected_days[int(year)]
        if (not part.days_processed.eq(count).all()
                or not part.time_steps.eq(count * context["hours_per_day"]).all()):
            raise ValueError(f"MERRA year {year} is incomplete despite the checkpoint")
    if annual.groupby("validation_id").merra_center_cells.nunique().gt(1).any():
        raise ValueError("MERRA spatial membership changed between years")
    weighted_cols = ("ws50_n", "ws50_d", "wpd50_n", "wpd50_d", "ws10_n", "ws10_d")
    for field in weighted_cols:
        values = pd.to_numeric(annual[field], errors="coerce")
        if values.isna().any() or (values < 0).any():
            raise ValueError(f"MERRA annual {field} contains invalid or negative totals")
    sums = annual.groupby("validation_id", as_index=False)[list(weighted_cols)].sum()
    for metric, num, den in (("mean_ws50", "ws50_n", "ws50_d"),
                             ("mean_wpd50_proxy", "wpd50_n", "wpd50_d"),
                             ("mean_ws10", "ws10_n", "ws10_d")):
        sums[metric] = sums[num].div(sums[den].replace(0, np.nan))
    cells = annual.groupby("validation_id", as_index=False).merra_center_cells.first()
    results = mapping.merge(sums, on="validation_id", validate="one_to_one").merge(
        cells, on="validation_id", validate="one_to_one"
    ).merge(cat, on="candidate_id", validate="one_to_one").merge(
        grid_roster, on="candidate_id", validate="one_to_one"
    ).merge(
        met[["candidate_id", "WPD100_mean_aw"] +
            (["WPD100_median_aw"] if "WPD100_median_aw" in met else [])],
        on="candidate_id", validate="one_to_one"
    ).sort_values("candidate_id")
    results["merra_covered"] = results.merra_center_cells.gt(0) & results.mean_wpd50_proxy.notna()
    w10, w50, p50 = [results[k].to_numpy(dtype=float) for k in
                     ("mean_ws10", "mean_ws50", "mean_wpd50_proxy")]
    good = np.isfinite(w10) & np.isfinite(w50) & (w10 > 0) & (w50 > 0)
    alpha = np.full(len(results), np.nan)
    alpha[good] = np.log(w50[good] / w10[good]) / np.log(5.0)
    results["mean_wpd100_powerlaw_proxy"] = np.where(good, p50 * np.power(2.0, 3 * alpha), np.nan)
    chosen = results.evidence_selection_status.eq("EVIDENCE_SELECTED")
    climate = results["climate_analysis_eligible"]
    primary = results["primary_climate_panel"]
    populations = {"all_candidates": results, "evidence_selected": results.loc[chosen],
                   "climate_eligible": results.loc[climate],
                   "primary_climate": results.loc[primary]}
    comparisons = {}
    for label, frame in populations.items():
        primary_stat = _spearman(frame)
        sensitivities = {"ws50_vs_gwa_mean": _spearman(frame, "mean_ws50", "WPD100_mean_aw"),
                         "powerlaw_wpd100_vs_gwa_mean": _spearman(
                             frame, "mean_wpd100_powerlaw_proxy", "WPD100_mean_aw")}
        if "WPD100_median_aw" in frame:
            sensitivities["wpd50_vs_gwa_median"] = _spearman(
                frame, "mean_wpd50_proxy", "WPD100_median_aw")
            sensitivities["ws50_vs_gwa_median"] = _spearman(
                frame, "mean_ws50", "WPD100_median_aw")
        comparisons[label] = {**primary_stat, "population_n": len(frame),
                              "sensitivities": sensitivities}
    summary = {
        "status": "COMPUTED", "method": "fresh_independent_merra2_hourly_aggregation",
        "period": [context["start"], context["end"]],
        "candidate_count": len(results),
        "population_sizes": {label: len(frame) for label, frame in populations.items()},
        "climate_tier_source": "Current GWEH grid candidate roster supplied with --roster",
        "candidates_with_merra_centres": int(results.merra_covered.sum()),
        "uncovered_candidate_ids": results.loc[~results.merra_covered, "candidate_id"].tolist(),
        "comparison": comparisons,
        "notes": [
            "Outcome-blind MERRA aggregation precedes joining candidate IDs and GWA metrics.",
            "MERRA-2 50m WPD proxy and GWA4 100m WPD have different heights and methods.",
            "Nominal p-values do not account for spatial dependence.",
            "No MERRA values are imputed for regions with no MERRA grid-cell centre.",
        ],
    }
    _new_directory(output)
    view = ["candidate_id", "validation_id", "merra_center_cells", "merra_covered",
            "mean_ws50", "mean_ws10", "mean_wpd50_proxy", "mean_wpd100_powerlaw_proxy",
            "WPD100_mean_aw", "evidence_selection_status"]
    view += [c for c in ("climate_analysis_eligible", "primary_climate_panel", "WPD100_median_aw")
             if c in results.columns]
    results[view].to_csv(output / "V4_merra_candidate_values.csv", index=False)
    annual_public = annual.merge(mapping, on="validation_id", validate="many_to_one")
    for metric, num, den in (("mean_ws50", "ws50_n", "ws50_d"),
                             ("mean_wpd50_proxy", "wpd50_n", "wpd50_d"),
                             ("mean_ws10", "ws10_n", "ws10_d")):
        annual_public[metric] = annual_public[num].div(annual_public[den].replace(0, np.nan))
    annual_public[["candidate_id", "year", "merra_center_cells", "days_processed",
                   "mean_ws50", "mean_wpd50_proxy", "mean_ws10"]].to_csv(
        output / "V4_merra_yearly.csv", index=False)
    (output / "V4_summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("prepare", help="Create outcome-blind geometry from current run")
    q.add_argument("--run-dir", type=Path, required=True)
    q.add_argument("--output", type=Path, required=True)
    q.add_argument("--seed", type=int, default=0)
    q = sub.add_parser("aggregate", help="Aggregate raw daily/hourly MERRA-2; resume yearly")
    q.add_argument("--blind-dir", type=Path, required=True)
    q.add_argument("--merra-root", type=Path, required=True)
    q.add_argument("--start", required=True)
    q.add_argument("--end", required=True)
    q.add_argument("--hours-per-day", type=int, default=24)
    q.add_argument("--output", type=Path, required=True)
    q.add_argument("--overwrite-years", action="store_true")
    q.add_argument("--progress-every", type=int, default=100)
    q = sub.add_parser("compare", help="Compare completed MERRA aggregates with current GWA4")
    q.add_argument("--annual-dir", type=Path, required=True)
    q.add_argument("--mapping", type=Path, required=True)
    q.add_argument("--catalogue", type=Path, required=True)
    q.add_argument("--roster", type=Path, required=True,
                   help="Current complete GWEH_grid_candidate_roster.csv, including climate flags")
    q.add_argument("--metrics", type=Path, required=True)
    q.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.command == "prepare":
        result = prepare(a.run_dir, a.output, a.seed)
    elif a.command == "aggregate":
        result = aggregate(a.blind_dir, a.merra_root, a.output,
                           start=date.fromisoformat(a.start), end=date.fromisoformat(a.end),
                           hours_per_day=a.hours_per_day, overwrite_years=a.overwrite_years,
                           progress_every=a.progress_every)
    else:
        result = compare(a.annual_dir, a.mapping, a.catalogue, a.metrics, a.output, roster=a.roster)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
