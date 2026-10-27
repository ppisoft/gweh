#!/usr/bin/env python3
"""Package completed independent V1–V4 comparisons and CF report.

Scientific checks here concern the *meaning* and coverage of reports. This is not
an authorship or submission approval workflow, and no candidate count is fixed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import yaml

REQUIRED = {
    "spatial": ("V1_V2_summary.json", "V1_V2_candidate_containment.csv"),
    "market": ("V3_summary.json",),
    "merra": ("V4_summary.json", "V4_merra_candidate_values.csv", "V4_merra_yearly.csv"),
}
SUMMARY = {"spatial": "V1_V2_summary.json", "market": "V3_summary.json", "merra": "V4_summary.json"}


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read JSON report: {path}") from exc


def _expected_sources(config_file: Path) -> set[str]:
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    external = config["source_roles"]["DeepOWT"].get("ground_truth", [])
    return {"GOWIRES", "GRW", "DeepOWT"} | {
        f"DeepOWT_GT_{Path(path).stem}" for path in external
    }


def _check_spatial(path: Path, config_file: Path) -> dict:
    summary = _json(path / SUMMARY["spatial"])
    if summary.get("status") != "COMPUTED":
        raise ValueError(f"V1/V2 is not computed: {path}")
    sources = summary.get("sources", [])
    labels = [item.get("source") for item in sources]
    expected = _expected_sources(config_file)
    if len(set(labels)) != len(labels) or set(labels) != expected:
        raise ValueError(f"V1/V2 external sources differ: missing={sorted(expected-set(labels))}, "
                         f"extra={sorted(set(labels)-expected)}")
    table = pd.read_csv(path / REQUIRED["spatial"][1])
    if table.duplicated(["source", "candidate_id"]).any():
        raise ValueError("V1/V2 duplicate (source, candidate_id) rows")
    for item in sources:
        rows = table.loc[table.source.eq(item["source"])]
        den = int(rows.in_denominator.sum())
        contained = int(rows.in_same_selected_region.sum())
        if (rows.in_same_selected_region > rows.in_denominator).any():
            raise ValueError(f"V1/V2 containment exceeds denominator in {item['source']}")
        if (den != item.get("within_selected_denominators")
                or contained != item.get("inside_same_selected_region")
                or int(item.get("valid_point_count", -1)) <= 0):
            raise ValueError(f"V1/V2 source and regional table differ: {item['source']}")
        fraction = contained / den if den else None
        if fraction is None or abs(fraction - item["conditional_containment"]) > 1e-12:
            raise ValueError(f"V1/V2 conditional containment is inconsistent: {item['source']}")
        if "scoped_region_area_fraction_point_weighted" in item:
            key = "region_area_fraction_of_denominator"
            if key not in rows:
                raise ValueError(f"V1/V2 source {item['source']}: missing region area fractions")
            if rows[key].isna().any() or not rows[key].between(0, 1).all():
                raise ValueError(f"V1/V2 source {item['source']}: invalid area baseline")
            expected = float((rows.in_denominator * rows[key]).sum()) / den
            baseline = item["scoped_region_area_fraction_point_weighted"]
            if (baseline is None or abs(expected - baseline) > 1e-12 or
                    abs((fraction - expected) - item["excess_containment_over_area_fraction"]) > 1e-12):
                raise ValueError(f"V1/V2 source {item['source']}: area baseline differs from detailed data")
    n = summary.get("population_candidate_count")
    return {"status": "COMPUTED", "candidate_count": n, "source_count": len(sources)}


def _check_market(path: Path) -> dict:
    summary = _json(path / SUMMARY["market"])
    if summary.get("status") != "COMPUTED" or not summary.get("results"):
        raise ValueError(f"V3 report is absent/incomplete: {path}")
    for year, record in summary["results"].items():
        file = path / f"V3_irena_gwpt_{year}.csv"
        if not file.is_file():
            raise FileNotFoundError(f"V3 year {year} missing its detailed table: {file}")
        table = pd.read_csv(file)
        if table.duplicated(["country_key", "surface"]).any():
            raise ValueError(f"V3 duplicate country/surface records in year {year}")
        fields = ["country_key", "surface", "gwpt_mw", "gwpt_rank", "irena_rank"]
        if list(table.columns) != fields or table[fields].isna().any().any():
            raise ValueError(f"V3 public rank-only schema mismatch for year {year}")
        if not table.surface.isin(["onshore", "offshore"]).all() or not table.gwpt_mw.gt(0).all():
            raise ValueError(f"V3 published population must contain only positive matched pairs for {year}")
        if len(table) != record.get("positive_pair_country_technology_count"):
            raise ValueError(f"V3 positive pair count differs for year {year}")
        if len(table) >= 3:
            rho = table.irena_rank.corr(table.gwpt_rank, method="spearman")
            if abs(rho - record["spearman_positive_pairs"]) > 1e-12:
                raise ValueError(f"V3 Spearman value differs from published ranks for year {year}")
            if not table.gwpt_rank.equals(table.gwpt_mw.rank(method="average")):
                raise ValueError(f"V3 GWPT ranks inconsistent with published GWPT values in {year}")
    return {"status": "COMPUTED", "candidate_count": summary.get("population_candidate_count"),
            "years": sorted(summary["results"])}


def _booleans(frame: pd.DataFrame, name: str) -> pd.Series:
    val = frame[name].astype("string").str.strip().str.lower()
    if not val.isin(["0", "1", "false", "true"]).all():
        raise ValueError(f"V4 invalid {name} flags")
    return val.isin(["1", "true"])


def _check_merra(path: Path, roster_file: Path | None) -> dict:
    summary = _json(path / SUMMARY["merra"])
    if summary.get("status") != "COMPUTED":
        raise ValueError(f"V4 not computed: {path}")
    values = pd.read_csv(path / "V4_merra_candidate_values.csv", dtype={"candidate_id": str})
    yearly = pd.read_csv(path / "V4_merra_yearly.csv", dtype={"candidate_id": str})
    if values.candidate_id.isna().any() or values.candidate_id.duplicated().any():
        raise ValueError("V4 candidate values have missing/duplicate candidate IDs")
    if len(values) != summary.get("candidate_count"):
        raise ValueError("V4 candidate count differs from individual values")
    if yearly.duplicated(["candidate_id", "year"]).any():
        raise ValueError("V4 duplicate candidate/year rows")
    years = list(range(int(summary["period"][0][:4]), int(summary["period"][1][:4]) + 1))
    if (set(yearly.candidate_id) != set(values.candidate_id)
            or set(yearly.year.astype(int)) != set(years)
            or len(yearly) != len(values) * len(years)):
        raise ValueError("V4 annual regional records are incomplete")
    if roster_file is None:
        raise ValueError("V4 requires the current complete grid roster (--roster); "
                         "without it climate/primary statistics cannot be verified")
    roster = pd.read_csv(roster_file, dtype={"candidate_id": str})
    needed = {"candidate_id", "climate_analysis_eligible", "primary_climate_panel"}
    if not needed <= set(roster) or set(roster.candidate_id) != set(values.candidate_id) or roster.candidate_id.duplicated().any():
        raise ValueError("V4 grid roster and candidate values differ or lack required flags")
    climate = _booleans(roster, "climate_analysis_eligible")
    primary = _booleans(roster, "primary_climate_panel")
    by_id = values.set_index("candidate_id")
    selection = by_id.evidence_selection_status.eq("EVIDENCE_SELECTED")
    if (roster.set_index("candidate_id").index.to_series().map(selection).isna().any()
            or (primary & ~climate).any()
            or (climate & ~roster.candidate_id.map(selection)).any()):
        raise ValueError("V4 climate/primary flags violate evidence selection")
    expected = {"all_candidates": len(values), "evidence_selected": int(selection.sum()),
                "climate_eligible": int(climate.sum()), "primary_climate": int(primary.sum())}
    for name, n in expected.items():
        actual = summary.get("comparison", {}).get(name, {}).get("population_n")
        if actual != n:
            raise ValueError(f"V4 population {name} is incorrectly labelled {actual}; "
                             f"current grid roster requires {n}. Regenerate V4 compare with --roster.")
    # Cross-check the reported statistics against the current roster by ID,
    # not merely the population counts. Otherwise an incorrect 58-member
    # subgroup could pass a count-only check.
    mask_by_id = pd.DataFrame({
        "candidate_id": roster.candidate_id,
        "climate_eligible": climate.to_numpy(dtype=bool),
        "primary_climate": primary.to_numpy(dtype=bool),
    }).set_index("candidate_id")
    ordered = values.set_index("candidate_id").join(mask_by_id, validate="one_to_one")
    subsets = {
        "all_candidates": pd.Series(True, index=ordered.index),
        "evidence_selected": ordered.evidence_selection_status.eq("EVIDENCE_SELECTED"),
        "climate_eligible": ordered.climate_eligible,
        "primary_climate": ordered.primary_climate,
    }
    for name, mask in subsets.items():
        part = ordered.loc[mask, ["mean_wpd50_proxy", "WPD100_mean_aw"]].apply(
            pd.to_numeric, errors="coerce"
        ).dropna()
        record = summary["comparison"][name]
        if len(part) != record.get("n"):
            raise ValueError(f"V4 {name} has {len(part)} matched resource pairs, "
                             f"but the summary reports {record.get('n')}")
        observed = (float(part.mean_wpd50_proxy.corr(part.WPD100_mean_aw, method="spearman"))
                    if len(part) >= 3 else None)
        reported = record.get("spearman_rho")
        if observed is None:
            if reported is not None:
                raise ValueError(f"V4 {name} correlation should be undefined")
        elif reported is None or abs(observed - float(reported)) > 1e-12:
            raise ValueError(f"V4 {name} Spearman correlation differs from its regional data")
    # Source WPD/WindSpeed annual aggregates are not recomputed here; their
    # scientific review uses the independently stored blinded annual evidence.
    return {"status": "COMPUTED", "candidate_count": len(values), "population_sizes": expected,
            "years": years}


def _cf_passed(report: Path) -> bool:
    return "All tests passed!" in report.read_text(encoding="utf-8")


def _public_merra_context(original: dict) -> dict:
    """Remove only the local filesystem source root from the public context."""
    if not isinstance(original, dict) or not original.get("blind_geometry_sha256"):
        raise ValueError("MERRA aggregation context lacks blinded geometry provenance")
    redacted = dict(original)
    redacted["merra_root"] = "<external MERRA-2 input directory>"
    return redacted


def check_public_merra_context(archive: Path) -> None:
    """Ensure a deposit archive has an anonymized, location-free context."""
    context = Path(archive) / "merra_provenance/annual/aggregation_context.json"
    if not context.is_file():
        raise FileNotFoundError(f"Missing annual MERRA-2 provenance: {context}")
    metadata = _json(context)
    if not metadata.get("blind_geometry_sha256"):
        raise ValueError("MERRA provenance has no blind geometry checksum")
    if Path(str(metadata.get("merra_root", ""))).is_absolute():
        raise ValueError("Validation archive contains a private absolute MERRA-2 source path; "
                         "choose a new archive directory and regenerate the public copy")


def check_merra_protocol(archive: Path, *, source: Path | None = None) -> None:
    """Require the exact resource-corroboration protocol used in the software."""
    if source is None:
        source = Path(__file__).parent / "config/merra.yaml"
    actual = Path(archive) / "methodology/merra_protocol.yaml"
    if not actual.is_file():
        raise FileNotFoundError(f"Missing independent MERRA protocol: {actual}")
    expected_data = yaml.safe_load(source.read_text(encoding="utf-8"))
    actual_data = yaml.safe_load(actual.read_text(encoding="utf-8"))
    if actual_data != expected_data:
        raise ValueError("Archived MERRA protocol differs from the current software protocol; "
                         "choose a NEW validation_archive_dir and rebuild validation")


def assemble(output: Path, *, spatial=None, market=None, merra=None, cf_report=None,
             require_complete=False, roster=None,
             source_config=None, merra_work=None, cf_grid=None, cf_sha256_file=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Validation archive exists: {output}; use a new output path")
    inputs = {"spatial": spatial, "market": market, "merra": merra}
    if require_complete and (any(v is None for v in inputs.values()) or cf_report is None):
        raise ValueError("Complete validation requires spatial, market, fresh MERRA and CF reports")
    config_file = Path(source_config) if source_config else Path(__file__).parent / "config/sources.yaml"
    records = {}
    for label, directory in inputs.items():
        if directory is None:
            records[label] = {"status": "NOT_PROVIDED"}
            continue
        src = Path(directory)
        for filename in REQUIRED[label]:
            if not (src / filename).is_file():
                raise FileNotFoundError(
                    f"Missing {label.upper()} result: {src / filename}. "
                    "Check --validation-root/--merra and rerun only the missing step."
                )
        if label == "spatial":
            records[label] = _check_spatial(src, config_file)
        elif label == "market":
            records[label] = _check_market(src)
        else:
            records[label] = _check_merra(src, Path(roster) if roster else None)
    populations = [v["candidate_count"] for v in records.values() if v.get("status") == "COMPUTED"]
    if len(set(populations)) > 1:
        raise ValueError(f"V1–V4 candidate populations differ: {populations}")
    if require_complete and any(records[k]["status"] != "COMPUTED" for k in inputs):
        raise ValueError(f"Incomplete validation reports: {records}")
    if cf_report is not None and not Path(cf_report).is_file():
        raise FileNotFoundError(f"Missing CF report: {cf_report}")
    cf_ok = cf_report is not None and _cf_passed(Path(cf_report))
    if require_complete and not cf_ok:
        raise ValueError(f"CF report does not confirm 'All tests passed!': {cf_report}")
    cf_grid_hash = None
    if require_complete and (cf_grid is None or cf_sha256_file is None):
        raise ValueError("Complete validation requires the checked grid and its CF SHA256 evidence")
    if cf_grid is not None:
        grid_file = Path(cf_grid)
        digest_file = Path(cf_sha256_file) if cf_sha256_file is not None else None
        if not grid_file.is_file() or digest_file is None or not digest_file.is_file():
            raise FileNotFoundError("CF grid or its corresponding SHA256 check is absent")
        digest = hashlib.sha256()
        with grid_file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        cf_grid_hash = digest.hexdigest()
        if digest_file.read_text(encoding="utf-8").strip() != cf_grid_hash:
            raise ValueError("CF check report belongs to another NetCDF grid; rerun CF checker")
    if merra_work is not None:
        from validation.geometry import create_blind_fingerprints
        work = Path(merra_work)
        files = ([work / "README.json", work / "supervisor/mapping.csv",
                  work / "annual/aggregation_context.json", work / "blind/roster.csv",
                  work / "blind/regions.gpkg"]
                 + sorted((work / "annual").glob("year_*.csv")))
        if any(not path.is_file() for path in files) or len(files) <= 5:
            raise FileNotFoundError(f"MERRA reproducibility checkpoint missing from {work}")
        expected_geometry = _json(work / "annual/aggregation_context.json").get("blind_geometry_sha256")
        digest = hashlib.sha256()
        with (work / "blind/regions.gpkg").open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        if expected_geometry != digest.hexdigest():
            raise ValueError("Blinded MERRA geometry differs from the annual aggregation context")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_validation_", dir=output.parent) as folder:
        stage = Path(folder)
        for label, directory in inputs.items():
            if directory is None:
                continue
            dest = stage / label
            dest.mkdir()
            for filename in REQUIRED[label]:
                shutil.copy2(Path(directory) / filename, dest / filename)
            if label == "market":
                for path in sorted(Path(directory).glob("V3_irena_gwpt_*.csv")):
                    shutil.copy2(path, dest / path.name)
        if cf_report is not None:
            shutil.copy2(cf_report, stage / "CF11_IOOS_CHECK.txt")
        if cf_grid_hash is not None:
            (stage / "CF_GRID_SHA256.txt").write_text(cf_grid_hash + "\n")
        if merra_work is not None:
            # The full frozen blind GeoPackage remains in the working V4 archive.
            # Publish WKB fingerprints instead of a second copy of every polygon.
            fingerprint_count = create_blind_fingerprints(
                work / "blind/regions.gpkg", work / "supervisor/mapping.csv",
                stage / "merra_provenance/blind/geometry_fingerprints.csv",
            )
            if fingerprint_count != _json(work / "annual/aggregation_context.json")["region_count"]:
                raise ValueError("V4 geometry fingerprint count differs from aggregation context")
            for path in files:
                if path == work / "blind/regions.gpkg":
                    continue
                dest = stage / "merra_provenance" / path.relative_to(merra_work)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if path.name == "aggregation_context.json":
                    # The private aggregation context is used for exact restart.
                    # Only its public copy has the local source root redacted.
                    content = _public_merra_context(_json(path))
                    dest.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
                else:
                    shutil.copy2(path, dest)
            readme = stage / "merra_provenance/README.json"
            info = _json(readme)
            info["geometry_file"] = "internal V4 checkpoint only: blind/regions.gpkg"
            info["public_geometry"] = "GWEH_geometries.gpkg:regions"
            info["public_identity_check"] = "blind/geometry_fingerprints.csv"
            readme.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        docs = stage / "methodology"
        docs.mkdir()
        shutil.copy2(Path(__file__).parents[1] / "publication/dataset_docs/VALIDATION_METHODS.md",
                     docs / "VALIDATION_METHODS.md")
        shutil.copy2(config_file, docs / "external_sources.yaml")
        if (Path(__file__).parent / "config/merra.yaml").is_file():
            shutil.copy2(Path(__file__).parent / "config/merra.yaml", docs / "merra_protocol.yaml")
        records["cf11"] = {"status": "PASSED" if cf_ok else "NOT_PROVIDED" if cf_report is None else "REVIEW_REQUIRED"}
        payload = {
            "status": "COMPLETE_REPORT_SET_REQUIRES_SCIENTIFIC_REVIEW"
            if all(records[k]["status"] == "COMPUTED" for k in inputs) and cf_ok
            else "PARTIAL_EVIDENCE_ARCHIVE",
            "checks": records,
            "candidate_count": populations[0] if populations else None,
            "notes": [
                "V1/V2, V3 and V4 results are independently computed and retain their source scopes.",
                "CF-1.11 compliance pass is distinct from scientific and licensing review.",
                "MERRA sample correlations are descriptive; nominal p-values ignore spatial dependence.",
                "The frozen V4 GeoPackage is retained in the working archive, not duplicated in the public dataset; per-ID WKB checksums preserve its identity.",
            ],
        }
        (stage / "VALIDATION_SCOPE.json").write_text(json.dumps(payload, indent=2) + "\n")
        stage.rename(output)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-root", type=Path, help="Root containing spatial/, market/, merra/")
    parser.add_argument("--output", type=Path, required=True)
    for name in ("spatial", "market", "merra"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--cf-report", type=Path)
    parser.add_argument("--cf-grid", type=Path)
    parser.add_argument("--cf-sha256-file", type=Path)
    parser.add_argument("--roster", type=Path, help="Full current grid candidate roster (mandatory for V4)")
    parser.add_argument("--merra-work", type=Path, help="Original blind MERRA preparation and annual checkpoints")
    parser.add_argument("--source-config", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    dirs = {name: getattr(args, name) or (args.validation_root / name if args.validation_root else None)
            for name in ("spatial", "market", "merra")}
    payload = assemble(args.output, **dirs, roster=args.roster, merra_work=args.merra_work,
                       cf_report=args.cf_report, cf_grid=args.cf_grid,
                       cf_sha256_file=args.cf_sha256_file, source_config=args.source_config,
                       require_complete=args.require_complete)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
