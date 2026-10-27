#!/usr/bin/env python3
"""Simple entry point for GWEH science, independent validation and omission.

All file paths live in one local YAML file. This wrapper never changes the
scientific stage algorithms and never fixes the expected number of regions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation"
VALIDATION_SOURCES = VALIDATION / "config/sources.yaml"


def _abs(base: Path, value, label: str, *, required=True) -> Path | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"Missing '{label}' in workflow config")
        return None
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


class Settings:
    def __init__(self, filename: Path):
        self.file = filename.resolve()
        if not self.file.is_file():
            raise FileNotFoundError(f"Workflow configuration file not found: {self.file}")
        data = yaml.safe_load(self.file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Workflow YAML must contain a mapping: {self.file}")
        self.data = data
        self.base = self.file.parent
        self.parent = _abs(self.base, data.get("data_parent"), "data_parent")
        self.scenario_id = str(data.get("scenario_id", "")).strip()
        self.run_id = str(data.get("run_id", "")).strip()
        if not self.scenario_id or not self.run_id:
            raise ValueError("Both scenario_id and run_id must be configured")
        import re
        for label, value in (("scenario_id", self.scenario_id), ("run_id", self.run_id)):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or ".." in value:
                raise ValueError(f"Invalid {label}: {value}")
        self.scenario = self.parent / "science_scenarios" / self.scenario_id
        self.data_root = self.scenario / "data"
        self.run = self.data_root / "runs" / self.run_id
        self.grid = _abs(self.base, data.get("grid_dir"), "grid_dir", required=False) or (self.scenario / f"grid_{self.run_id}")
        self.valid = _abs(self.base, data.get("validation_dir"), "validation_dir", required=False) or (self.scenario / f"validation_{self.run_id}")
        self.public = _abs(self.base, data.get("public_dir"), "public_dir", required=False) or (self.scenario / f"public_{self.run_id}")
        self.omi = _abs(self.base, data.get("omission_dir"), "omission_dir", required=False) or (self.scenario / f"omission_{self.run_id}")
        self.viewer = _abs(self.base, data.get("viewer_dir"), "viewer_dir", required=False) or (self.scenario / f"viewer_{self.run_id}")
        self.external_root = _abs(self.base, data.get("external_root"), "external_root", required=False) or self.parent
        self.inputs = data.get("external", {}) or {}
        standard_merra = yaml.safe_load((VALIDATION / "config/merra.yaml").read_text(encoding="utf-8"))
        self.merra_cfg = {"blind_seed": standard_merra["blind_seed"],
                          "start": standard_merra["period"]["start"],
                          "end": standard_merra["period"]["end"]}
        self.merra_cfg.update(data.get("merra", {}) or {})
        self.merra_work = _abs(self.base, data.get("merra_work_dir"), "merra_work_dir", required=False) or (self.valid / "merra_work")
        self.merra_result = _abs(self.base, data.get("merra_result_dir"), "merra_result_dir", required=False) or (self.valid / "merra_current")
        self.archive = _abs(self.base, data.get("validation_archive_dir"), "validation_archive_dir", required=False) or (self.valid / "archive_current")

    @property
    def grid_template(self):
        return _abs(self.base, self.data.get("grid_template"), "grid_template", required=False) or ROOT / "grid/CESM_192x288.nc"

    @property
    def roster(self):
        return self.grid / "GWEH_grid_candidate_roster.csv"

    @property
    def cf_grid(self):
        return self.grid / f"GWEH_grid_{self.grid_template.stem}.nc"

    @property
    def cf_lookup(self):
        return self.grid / "GWEH_grid_region_lookup.csv"

    @property
    def cf_report(self):
        return _abs(self.base, self.data.get("cf_report"), "cf_report", required=False) or (self.grid / "CF11_IOOS_CHECK.txt")

    def source(self, key: str, default: str | None = None) -> Path:
        value = self.inputs.get(key, default)
        return _abs(self.external_root, value, f"external.{key}")

    def list_sources(self, key: str, defaults: list[str] | None = None) -> list[Path]:
        values = self.inputs.get(key)
        if values is None:
            values = defaults or []
        if not isinstance(values, list) or not values:
            raise ValueError(f"external.{key} must be a non-empty YAML list")
        return [self.source_single(value) for value in values]

    def source_single(self, value: str) -> Path:
        return _abs(self.external_root, value, "external source")


def _ensure(files: list[Path], label: str):
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"{label}: missing {len(missing)} source files:\n  " + "\n  ".join(missing))


def _cmd(*args):
    print("[run]", " ".join(map(str, args)), flush=True)
    subprocess.run([str(x) for x in args], check=True)


def science(conf: Settings):
    if not conf.scenario.exists():
        _cmd(sys.executable, ROOT / "tools/source.py", "--repo", ROOT,
             "--data-root", conf.parent, "--scenario-id", conf.scenario_id)
    _cmd(sys.executable, ROOT / "tools/source.py", "--repo", ROOT,
         "--verify", "--data-root", conf.parent, "--scenario-id", conf.scenario_id)
    _cmd("bash", ROOT / "RUN.sh", "run", conf.data_root, conf.run_id)
    make_grid(conf)


def make_grid(conf: Settings):
    from validation.merra import _unique
    if conf.grid.exists():
        _ensure([conf.roster, conf.cf_grid, conf.cf_lookup], "Existing grid")
        c = _unique(conf.run / "08_catalogue/catalogue.csv", {"candidate_id"})
        r = _unique(conf.roster, {"candidate_id", "climate_analysis_eligible", "primary_climate_panel"})
        if set(c.candidate_id) != set(r.candidate_id):
            raise ValueError("The existing grid roster is from another candidate universe")
        from gweh.manifest import sha256_file
        import xarray as xr
        geo = conf.run / "05_exclusive/exclusive_regions.parquet"
        manifest = conf.run / "manifest/run_manifest.json"
        _ensure([geo, manifest], "Run provenance for existing grid")
        science = json.loads(manifest.read_text(encoding="utf-8"))
        with xr.open_dataset(conf.cf_grid, decode_cf=False, mask_and_scale=False) as data:
            attrs = dict(data.attrs)
        if (attrs.get("gweh_source_run_id") != conf.run_id
                or attrs.get("source_code_sha256") != science.get("source_code_sha256")
                or attrs.get("geometry_regions_sha256") != sha256_file(geo)):
            raise ValueError("Existing grid belongs to another science run or geometry: "
                             "choose a new grid_dir and rebuild the grid")
        # Reusing an old grid is safe only when its published CF metadata
        # correspond to the current publication file as well as to the run.
        expected_metadata = yaml.safe_load(
            (ROOT / "metadata/publication.yaml").read_text(encoding="utf-8")
        ) or {}
        if any(
            (str(attrs[field]).strip() if attrs.get(field) not in (None, "") else None)
            != (value.strip() if isinstance(value, str) and value.strip() else None)
            for field, value in expected_metadata.items()
        ):
            raise ValueError(
                "Existing grid CF metadata differ from metadata/publication.yaml; "
                "configure a NEW grid_dir and regenerate the CF grid, then rerun IOOS"
            )
        if not r.primary_climate_panel.isin([0, 1]).all() or not r.climate_analysis_eligible.isin([0, 1]).all():
            raise ValueError("Grid roster lacks explicit 0/1 climate flags")
        if (r.primary_climate_panel > r.climate_analysis_eligible).any():
            raise ValueError("Primary grid panel is not a subset of climate-eligible candidates")
        print(f"[reuse] scientifically matched candidate grid: {conf.grid}")
        return
    if not (conf.run / "08_catalogue/catalogue.csv").is_file():
        raise FileNotFoundError(f"First complete Stage00–08: {conf.run}")
    _cmd(sys.executable, ROOT / "tools/grid.py", "--run-dir", conf.run,
         "--template", conf.grid_template, "--output", conf.grid,
         "--metadata", ROOT / "metadata/publication.yaml")


def _spatial_inputs(conf: Settings):
    spec = yaml.safe_load(VALIDATION_SOURCES.read_text(encoding="utf-8"))["source_roles"]
    inputs = {
        "gowires": conf.source("gowires", spec["GOWIRES"]["path"]),
        "grw": conf.source("grw", spec["GRW"]["path"]),
        "deepowt": conf.source("deepowt", spec["DeepOWT"]["path"]),
        "deepowt_gt": conf.list_sources("deepowt_gt", spec["DeepOWT"]["ground_truth"]),
    }
    _ensure([inputs["gowires"], inputs["grw"], inputs["deepowt"], *inputs["deepowt_gt"]],
            "V1/V2 external evidence")
    return inputs


def _market_inputs(conf: Settings):
    chosen = conf.inputs.get("irena")
    if chosen is None:
        pattern = yaml.safe_load(VALIDATION_SOURCES.read_text(encoding="utf-8"))["source_roles"]["IRENASTAT"]["file_glob"]
        paths = sorted(conf.external_root.glob(pattern))
    else:
        if isinstance(chosen, str):
            chosen = [chosen]
        if not isinstance(chosen, list):
            raise ValueError("external.irena must be a file path or list of file paths")
        paths = [conf.source_single(x) for x in chosen]
    if not paths:
        raise FileNotFoundError(f"No IRENASTAT files found under {conf.external_root} (external.irena)")
    _ensure(paths, "V3 IRENASTAT")
    return paths


def validate(conf: Settings, steps: list[str]):
    allowed = ("spatial", "market", "merra-prepare", "merra-aggregate", "merra-compare", "cf", "archive")
    invalid = set(steps) - set(allowed)
    if invalid:
        raise ValueError(f"Invalid validation steps {sorted(invalid)}; choose from {allowed}")
    make_grid(conf)
    conf.valid.mkdir(parents=True, exist_ok=True)
    from validation.prepare import assemble, _check_spatial, _check_market, _check_merra, _cf_passed
    spatial = conf.valid / "spatial"
    market = conf.valid / "market"
    if "spatial" in steps:
        if spatial.exists():
            print("[reuse]", _check_spatial(spatial, VALIDATION_SOURCES))
        else:
            args = _spatial_inputs(conf)
            _cmd(sys.executable, VALIDATION / "spatial.py", "--run-dir", conf.run,
                 "--gowires", args["gowires"], "--grw", args["grw"],
                 "--deepowt", args["deepowt"], "--deepowt-gt", *args["deepowt_gt"],
                 "--output", spatial)
            _check_spatial(spatial, VALIDATION_SOURCES)
    if "market" in steps:
        if market.exists():
            print("[reuse]", _check_market(market))
        else:
            _cmd(sys.executable, VALIDATION / "market.py", "--run-dir", conf.run,
                 "--irena", *_market_inputs(conf), "--output", market)
            _check_market(market)
    if "merra-prepare" in steps:
        if conf.merra_work.exists():
            _ensure([conf.merra_work / "blind/regions.gpkg",
                     conf.merra_work / "blind/roster.csv",
                     conf.merra_work / "supervisor/mapping.csv",
                     conf.merra_work / "README.json"], "Existing outcome-blind MERRA preparation")
            info = json.loads((conf.merra_work / "README.json").read_text())
            if info.get("source_run") != conf.run_id:
                raise ValueError("Blind MERRA panel originates from another science run")
            print(f"[reuse] blind MERRA geometry: {conf.merra_work}")
        else:
            _cmd(sys.executable, VALIDATION / "merra.py", "prepare", "--run-dir", conf.run,
                 "--output", conf.merra_work, "--seed", str(conf.merra_cfg["blind_seed"]))
    if "merra-aggregate" in steps:
        if not (conf.merra_work / "blind/regions.gpkg").is_file():
            raise FileNotFoundError("Run validation step merra-prepare first")
        merra_root = conf.source("merra_root", "raw/MERRA2")
        if not merra_root.is_dir():
            raise FileNotFoundError(f"MERRA input directory does not exist: {merra_root}")
        _cmd(sys.executable, VALIDATION / "merra.py", "aggregate",
             "--blind-dir", conf.merra_work / "blind", "--merra-root", merra_root,
             "--start", str(conf.merra_cfg.get("start", "2008-01-01")),
             "--end", str(conf.merra_cfg.get("end", "2017-12-31")),
             "--output", conf.merra_work / "annual")
    if "merra-compare" in steps:
        if not (conf.merra_work / "annual/aggregation_context.json").is_file():
            raise FileNotFoundError("Run validation step merra-aggregate first or configure merra_work_dir")
        if conf.merra_result.exists():
            print("[reuse]", _check_merra(conf.merra_result, conf.roster))
        else:
            _cmd(sys.executable, VALIDATION / "merra.py", "compare",
                 "--annual-dir", conf.merra_work / "annual",
                 "--mapping", conf.merra_work / "supervisor/mapping.csv",
                 "--catalogue", conf.run / "08_catalogue/catalogue.csv",
                 "--metrics", conf.run / "06_metrics/region_metrics.csv",
                 "--roster", conf.roster, "--output", conf.merra_result)
            _check_merra(conf.merra_result, conf.roster)
    if "cf" in steps:
        report = conf.cf_report
        checker = conf.data.get("cf_checker")
        if checker:
            checker = _abs(conf.base, checker, "cf_checker")
            _ensure([checker, conf.cf_grid], "CF checker")
            report.parent.mkdir(parents=True, exist_ok=True)
            # Recheck the *current* grid every time. A stale text report cannot
            # stand in for compliance of a newly generated NetCDF file.
            with tempfile.TemporaryDirectory(prefix=".cf11_", dir=report.parent) as temp:
                fresh = Path(temp) / "CF11.txt"
                _cmd(checker, "--test=cf:1.11", "--criteria=strict", "--format=text",
                     f"--output={fresh}", conf.cf_grid)
                if not _cf_passed(fresh):
                    raise ValueError(f"CF metadata checker did not pass: {fresh.read_text()}")
                fresh.replace(report)
            (report.parent / "CF_GRID_SHA256.txt").write_text(
                hashlib.sha256(conf.cf_grid.read_bytes()).hexdigest() + "\n",
                encoding="utf-8",
            )
            print(f"[done] CF-1.11 PASS on current grid: {conf.cf_grid}")
        elif report.is_file():
            if report.stat().st_mtime_ns < conf.cf_grid.stat().st_mtime_ns:
                raise ValueError("Existing CF report predates the NetCDF grid; "
                                 "configure cf_checker and rerun the CF step")
            if not _cf_passed(report):
                raise ValueError(f"Existing CF report does not pass: {report}")
            sidecar = report.parent / "CF_GRID_SHA256.txt"
            if not sidecar.is_file() or sidecar.read_text().strip() != hashlib.sha256(conf.cf_grid.read_bytes()).hexdigest():
                raise ValueError("CF report lacks current grid SHA256 evidence; configure cf_checker and rerun")
            print(f"[reuse] CF report PASSED: {report}")
        else:
            raise ValueError("Set cf_checker in workflow YAML or supply a current, passing cf_report")
    if "archive" in steps:
        if conf.archive.exists():
            if not (conf.archive / "merra_provenance/blind/geometry_fingerprints.csv").is_file():
                raise ValueError(
                    "Existing validation archive lacks the compact V4 geometry fingerprints. "
                    "Keep the original MERRA working checkpoints and configure a NEW "
                    "validation_archive_dir; then rerun --steps archive only."
                )
            from validation.prepare import _check_spatial, _check_market
            _check_spatial(conf.archive / "spatial", VALIDATION_SOURCES)
            _check_market(conf.archive / "market")
            _check_merra(conf.archive / "merra", conf.roster)
            if not _cf_passed(conf.archive / "CF11_IOOS_CHECK.txt"):
                raise ValueError("Archived CF checker report does not confirm PASS")
            cf_signature = conf.archive / "CF_GRID_SHA256.txt"
            if (not cf_signature.is_file() or
                    cf_signature.read_text().strip() != hashlib.sha256(conf.cf_grid.read_bytes()).hexdigest()):
                raise ValueError("Archived CF report does not match the current NetCDF")
            payload = json.loads((conf.archive / "VALIDATION_SCOPE.json").read_text())
            if payload.get("status") != "COMPLETE_REPORT_SET_REQUIRES_SCIENTIFIC_REVIEW":
                raise ValueError("Existing validation archive is not complete")
            from validation.prepare import check_merra_protocol, check_public_merra_context
            check_merra_protocol(conf.archive)
            check_public_merra_context(conf.archive)
            print(f"[reuse] verified V1–V4 archive: {conf.archive}")
        else:
            report = assemble(conf.archive, spatial=spatial, market=market,
                              merra=conf.merra_result, roster=conf.roster,
                              merra_work=conf.merra_work, cf_report=conf.cf_report,
                              cf_grid=conf.cf_grid, cf_sha256_file=conf.cf_report.parent / "CF_GRID_SHA256.txt",
                              source_config=VALIDATION_SOURCES, require_complete=True)
            print("[done] V1–V4 archive:", conf.archive)
            print(json.dumps(report["checks"], indent=2))



def omission(conf: Settings):
    """Run optional, read-only screening of external omission proposals."""
    params = conf.data.get("omission") or {}
    expert = params.get("expert_proposals")
    radius = params.get("unowned_radius_km")
    if expert in (None, "") and radius is None:
        raise ValueError(
            "Set omission.expert_proposals and/or omission.unowned_radius_km in "
            "workflow.local.yaml to run exploratory omission screening"
        )
    if not (conf.run / "05_exclusive/exclusive_regions.parquet").is_file():
        raise FileNotFoundError("Complete Stage00–08 before omission screening")
    if conf.omi.exists():
        raise FileExistsError(f"Omission output exists; choose a new omission_dir: {conf.omi}")
    args = [sys.executable, ROOT / "omission/screen.py", "--run-dir", conf.run]
    if expert not in (None, ""):
        path = _abs(conf.base, expert, "omission.expert_proposals")
        _ensure([path], "Omission proposal input")
        args.extend(["--expert-proposals", path])
    if radius is not None:
        args.extend(["--unowned-radius-km", str(radius)])
    conf.omi.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_omission_", dir=conf.omi.parent) as tmp:
        staging = Path(tmp) / "screen"
        _cmd(*args, "--output", staging)
        staging.rename(conf.omi)
    print(f"[done] read-only omission screen: {conf.omi}")


def public(conf: Settings):
    _ensure([conf.roster, conf.cf_grid, conf.cf_lookup,
             conf.archive / "VALIDATION_SCOPE.json"], "Public delivery inputs")
    from validation.prepare import _check_merra, _cf_passed
    _check_merra(conf.archive / "merra", conf.roster)
    if not _cf_passed(conf.archive / "CF11_IOOS_CHECK.txt"):
        raise ValueError("Published grid CF report does not show PASS")
    if ((conf.archive / "CF_GRID_SHA256.txt").read_text().strip()
            != hashlib.sha256(conf.cf_grid.read_bytes()).hexdigest()):
        raise ValueError("Archived CF validation refers to a different NetCDF file")
    if conf.public.exists():
        raise FileExistsError(f"Public output already exists: {conf.public}")
    _cmd(sys.executable, ROOT / "tools/products.py", "--run-dir", conf.run,
         "--output", conf.public, "--repo", ROOT,
         "--climate-roster", conf.roster, "--cf-grid", conf.cf_grid,
         "--cf-lookup", conf.cf_lookup, "--validation-dir", conf.archive)


def viewer(conf: Settings):
    """Build or verify the optional, read-only browser from the finished public data."""
    from publication.package import check_public, verify_viewer

    if not conf.public.is_dir():
        raise FileNotFoundError("Create the public dataset with './RUN.sh public' first")
    check_public(conf.public)
    if conf.viewer.exists():
        verify_viewer(conf.viewer, conf.public, ROOT)
        print(f"[reuse] verified interactive viewer: {conf.viewer}")
        return
    conf.viewer.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gweh_viewer_", dir=conf.viewer.parent) as tmp:
        staged = Path(tmp) / "viewer"
        staged.mkdir()
        _cmd(sys.executable, ROOT / "viewer/build_viewer.py", "--dataset", conf.public,
             "--out", staged / "GWEH_explorer.html", "--record", staged / "VIEWER_BUILD.json")
        verify_viewer(staged, conf.public, ROOT)
        staged.rename(conf.viewer)
    print(f"[done] GWEH data viewer: {conf.viewer / 'GWEH_explorer.html'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["science", "grid", "validate", "public", "viewer", "omission", "release"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="For release: new directory containing DOI-draft ZIPs")
    parser.add_argument("--steps", help="For validate: spatial,market,merra-prepare,merra-aggregate,merra-compare,cf,archive")
    parser.add_argument("--no-viewer", action="store_true", help="Skip optional HTML companion in release")
    args = parser.parse_args()
    config = Settings(args.config)
    if args.command == "science":
        science(config)
    elif args.command == "grid":
        make_grid(config)
    elif args.command == "validate":
        steps = args.steps.split(",") if args.steps else [
            "spatial", "market", "merra-prepare", "merra-aggregate", "merra-compare", "cf", "archive"
        ]
        validate(config, steps)
    elif args.command == "public":
        public(config)
    elif args.command == "viewer":
        viewer(config)
    elif args.command == "omission":
        omission(config)
    elif args.command == "release":
        if args.output is None:
            parser.error("release needs --output NEW_DIRECTORY")
        if not config.public.is_dir():
            public(config)
        args_package = [sys.executable, ROOT / "publication/package.py", "--repo", ROOT,
                        "--public-dir", config.public, "--run-dir", config.run,
                        "--output", _abs(config.base, args.output, "release.output")]
        if not args.no_viewer:
            viewer(config)
            args_package.extend(["--viewer-dir", config.viewer])
        _cmd(*args_package)


if __name__ == "__main__":
    try:
        main()
    except (FileNotFoundError, FileExistsError, ValueError) as error:
        raise SystemExit(f"[error] {error}") from None
