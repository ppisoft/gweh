#!/usr/bin/env python3
"""Independent country-capacity comparison: prepared GWPT versus IRENASTAT.

National reporting is not identical to a >=10-MW project inventory. Results are
contextual rank/coverage evidence, not a correction to selection scores.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import re
import unicodedata
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml


COUNTRY_ALIASES = {"uk": "united kingdom", "great britain": "united kingdom",
                   "republic of korea": "south korea", "korea rep": "south korea",
                   "russian federation": "russia", "turkiye": "turkey",
                   "united states of america": "united states", "usa": "united states",
                   "viet nam": "vietnam"}


def country_key(value, aliases):
    raw = unicodedata.normalize("NFKD", str(value if pd.notna(value) else ""))
    raw = re.sub(r"[^a-z0-9]+", " ", raw.encode("ascii", "ignore").decode().lower()).strip()
    key = COUNTRY_ALIASES.get(raw, raw)
    return aliases.get(key, key)


def market_comparison(gwpt: pd.DataFrame, irena: pd.DataFrame, *, year: int,
                      config: dict):
    source = config["source_roles"]["IRENASTAT"]
    aliases = {country_key(k, {}): country_key(v, {}) for k, v in source.get("country_aliases", {}).items()}
    exclude = {country_key(s, aliases) for s in source.get("exclude_country_keys", [])}
    x = irena.loc[pd.to_numeric(irena["Year"], errors="coerce").eq(year)].copy()
    allowed = {v.casefold(): k for k, values in source["leaf_technologies"].items() for v in values}
    x["surface"] = x["Technology"].astype(str).str.strip().str.casefold().map(allowed)
    x = x.loc[x.surface.notna()].copy()
    x["country_key"] = x["Country/area"].map(lambda z: country_key(z, aliases))
    x = x.loc[~x.country_key.isin(exclude)]
    # Several source files may contain overlapping snapshots. Summing duplicate
    # national total records would manufacture extra installed capacity.
    if "Grid connection" in x.columns:
        normalized_connection = x["Grid connection"].fillna("").astype(str).str.strip().str.casefold()
        if pd.DataFrame({"country": x.country_key, "surface": x.surface,
                         "grid_connection": normalized_connection}).duplicated().any():
            raise ValueError("IRENA input contains duplicate country/technology/grid-connection "
                             "rows; choose a single non-overlapping source snapshot")
    x["mw"] = pd.to_numeric(
        x["Electricity capacity statistics"].astype(str).str.replace(",", "", regex=False).str.replace(" ", "", regex=False),
        errors="coerce",
    )
    keys = ["country_key", "surface"]
    if "Grid connection" in x.columns:
        # Use the reported country/technology total, or component sum when no
        # total exists. Never add a national total to its grid subcomponents.
        is_total = x["Grid connection"].astype(str).str.strip().str.casefold().isin(
            {"total", "all", "all grid connections"}
        )
        totals = x.loc[is_total].groupby(keys).mw.sum(min_count=1)
        components = x.loc[~is_total].groupby(keys).mw.sum(min_count=1)
        joint = totals.to_frame("total").join(components.to_frame("components"), how="inner")
        joint = joint.dropna()
        if not joint.empty:
            relative_difference = (joint.total - joint.components).abs() / joint.total.replace(0, np.nan)
            if relative_difference.gt(0.01).any():
                raise RuntimeError("IRENA total and grid-component values disagree by more than 1%")
        irena_pairs = totals.combine_first(components)
    else:
        irena_pairs = x.groupby(keys).mw.sum(min_count=1)
    ir = irena_pairs.reset_index(name="irena_mw")

    operating = gwpt["is_operating"]
    if not pd.api.types.is_bool_dtype(operating.dtype):
        raise ValueError("GWPT is_operating must contain boolean values")
    source_surface = gwpt["surface_type_source"].astype(str).str.lower()
    base = gwpt.loc[operating & source_surface.isin(["onshore", "offshore"])].copy()
    base["country_key"] = base["country_area"].map(lambda z: country_key(z, aliases))
    base["surface"] = base["surface_type_source"].astype(str).str.lower()
    gw = base.groupby(keys).capacity_mw.sum(min_count=1).reset_index(name="gwpt_mw")
    comparison = ir.merge(gw, on=keys, how="outer", validate="one_to_one")
    positive = comparison.loc[comparison.irena_mw.gt(0) & comparison.gwpt_mw.gt(0)].copy()
    observed = comparison.loc[comparison.irena_mw.gt(0) & comparison.gwpt_mw.notna()]
    rho = positive.irena_mw.corr(positive.gwpt_mw, method="spearman") if len(positive) >= 3 else None
    result = {
        "year": year,
        "population_unit": "country_x_wind_technology_onshore_or_offshore",
        "irena_positive_pair_count": int(comparison.irena_mw.gt(0).sum()),
        "observed_gwpt_pair_count": len(observed),
        "positive_pair_country_technology_count": len(positive),
        "positive_pair_country_count": int(positive.country_key.nunique()),
        "spearman_positive_pairs": float(rho) if rho is not None else None,
        "median_gwpt_irena_ratio": float((positive.gwpt_mw / positive.irena_mw).median()) if len(positive) else None,
    }
    return result, comparison


def run(run_dir: Path, irena_files: list[Path], config_file: Path, output: Path):
    if output.exists():
        raise FileExistsError(output)
    # Fail immediately on an unwritable destination rather than after a long
    # spatial / source comparison has completed.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_write_test_", dir=output.parent):
        pass
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    frames = [pd.read_csv(file, encoding="cp1252", skiprows=1) for file in irena_files]
    if not frames:
        raise ValueError("No IRENASTAT files provided")
    irena = pd.concat(frames, ignore_index=True)
    gwpt = gpd.read_parquet(run_dir / "01_ingest/gwpt.parquet")
    outputs = {}
    output.mkdir(parents=True)
    irena_config = config["source_roles"]["IRENASTAT"]
    years = list(dict.fromkeys(int(irena_config[key]) for key in
                               ("canonical_year", "sensitivity_year") if key in irena_config))
    if not years:
        years = sorted(pd.to_numeric(irena["Year"], errors="coerce").dropna().astype(int).unique(), reverse=True)
    for year in years:
        result, comparison = market_comparison(gwpt, irena, year=year, config=config)
        outputs[str(year)] = result
        positive = comparison.loc[comparison.irena_mw.gt(0) & comparison.gwpt_mw.gt(0)].copy()
        # Export paired ranks only. Full IRENASTAT capacities stay in memory and
        # are neither deposited nor recoverable from a published GWPT/ratio pair.
        published = positive[["country_key", "surface", "gwpt_mw"]].copy()
        published["gwpt_rank"] = positive.gwpt_mw.rank(method="average").to_numpy()
        published["irena_rank"] = positive.irena_mw.rank(method="average").to_numpy()
        published.to_csv(output / f"V3_irena_gwpt_{year}.csv", index=False)
    summary = {"status": "COMPUTED", "published_table_scope": "paired_ranks_only_no_irena_capacity_values", "population_candidate_count":
               len(pd.read_csv(run_dir / "08_catalogue/catalogue.csv")),
               "results": outputs, "limitations":
               "GWPT >=10 MW project inventory and national IRENA totals describe different populations."}
    (output / "V3_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--irena", type=Path, nargs="+", required=True)
    p.add_argument("--config", type=Path, default=Path(__file__).parent / "config/sources.yaml")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(run(a.run_dir, a.irena, a.config, a.output), indent=2))


if __name__ == "__main__":
    main()
