"""Stage orchestration for the GWEH production workflow."""
from __future__ import annotations

import pandas as pd
from .candidates.domains import run_candidate_build
from .catalogue import run_catalogue
from .evidence.metrics import PRODUCTION_RASTER_CHUNK_SIZE, run_evidence_metrics
from .geography.build import run_geography_build
from .geometry.delineate import run_delineation
from .geometry.exclusive import run_exclusive_partition
from .ingest.gwpt import run_gwpt_ingest
from .inputs.validate import run_input_validation
from .manifest import update_run_stage, utc_now, write_stage_manifest
from .run import RunContext
from .scoring import run_scores
from .strategic_s import load_strategic_s_contract
from .yamlio import load_yaml_mapping


CORE_STAGES = (
    ("00", "inputs"),
    ("01", "ingest"),
    ("02", "geography"),
    ("03", "candidates"),
    ("04", "delineation"),
    ("05", "exclusive"),
    ("06", "metrics"),
    ("07", "scores"),
    ("08", "catalogue"),
)


_yaml = load_yaml_mapping


def _boundary(context):
    config = _yaml(context.config("boundary.yaml"))
    required = (
        "name", "physical_dataset_id", "land_suffix", "ocean_suffix", "coastline_suffix",
        "admin_dataset_id", "admin_suffix", "admin1_dataset_id", "admin1_suffix", "eez_dataset_id", "eez_suffix",
    )
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise ValueError(f"boundary.yaml is missing keys: {missing}")
    return {key: str(config[key]) for key in required}


def _record_stage(
    context,
    workspace,
    stage,
    started,
    config_keys,
    notes,
):
    return write_stage_manifest(
        workspace / "_stage.json",
        stage=stage,
        run_id=context.run_id,
        started_utc=started,
        completed_utc=utc_now(),
        config_keys_consumed=config_keys,
        notes=notes,
    )


def _run_stage00(context):
    stage = "00_inputs"
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_input_validation(
            data_root=context.data.root,
            source_registry_path=context.config("sources.yaml"),
            output_dir=workspace,
        )
        manifest = _record_stage(
            context, workspace, stage, started, ["sources.yaml"],
            qc,
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage01(context):
    stage = "01_ingest"
    context.require_stage("00_inputs")
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_gwpt_ingest(
            data_root=context.data.root,
            source_registry_path=context.config("sources.yaml"),
            surface_corrections_path=context.config("surface_source_corrections.csv"),
            output_dir=workspace,
        )
        manifest = _record_stage(
            context, workspace, stage, started, ["sources.yaml:GWPT", "surface_source_corrections.csv", "surface_policy.yaml"],
            qc,
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage02(context):
    stage = "02_geography"
    context.require_stage("00_inputs")
    boundary = _boundary(context)
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_geography_build(
            data_root=context.data.root,
            source_registry_path=context.config("sources.yaml"),
            boundary=boundary,
            output_dir=workspace,
        )
        manifest = _record_stage(
            context,
            workspace,
            stage,
            started,
            [
                f"sources.yaml:{boundary['physical_dataset_id']}",
                f"sources.yaml:{boundary['admin_dataset_id']}",
                f"sources.yaml:{boundary['admin1_dataset_id']}",
                f"sources.yaml:{boundary['eez_dataset_id']}",
                "boundary.yaml",
            ],
            qc,
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage03(context):
    stage = "03_candidates"
    context.require_stage("02_geography")
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_candidate_build(
            geography_dir=context.stage_dir("02_geography"),
            candidate_registry_path=context.config("candidates.csv"),
            domains_path=context.config("domains.yaml"),
            output_dir=workspace,
        )
        manifest = _record_stage(
            context,
            workspace,
            stage,
            started,
            ["candidates.csv", "domains.yaml"],
            qc,
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _gwa_wpd_source(context):
    manifest = pd.read_csv(context.stage_dir("00_inputs") / "source_manifest.csv", dtype=str).fillna("")
    rows = manifest[
        manifest["dataset_id"].eq("GWA4")
        & manifest["artifact_id"].eq("WPD100")
        & manifest["exists"].str.lower().eq("true")
    ]
    if len(rows) != 1:
        raise RuntimeError(f"Expected one resolved GWA4/WPD100 artifact from Stage 00, found {len(rows)}")
    path = (context.data.root / str(rows.iloc[0]["relative_path"])).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _run_stage04(context):
    stage = "04_delineation"
    context.require_stage("01_ingest")
    context.require_stage("03_candidates")
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_delineation(
            ingest_dir=context.stage_dir("01_ingest"),
            candidates_dir=context.stage_dir("03_candidates"),
            candidate_registry_path=context.config("candidates.csv"),
            geometry_anchor_exclusions_path=context.config("geometry_anchor_exclusions.csv"),
            geometry_config=_yaml(context.config("geometry.yaml")),
            wpd_path=_gwa_wpd_source(context),
            output_dir=workspace,
        )
        manifest = _record_stage(
            context, workspace, stage, started, ["geometry.yaml", "geometry_anchor_exclusions.csv", "candidates.csv"],
            qc,
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage05(context):
    stage = "05_exclusive"
    context.require_stage("01_ingest")
    context.require_stage("02_geography")
    context.require_stage("04_delineation")
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_exclusive_partition(
            ingest_dir=context.stage_dir("01_ingest"),
            geography_dir=context.stage_dir("02_geography"),
            delineation_dir=context.stage_dir("04_delineation"),
            domains_path=context.config("domains.yaml"),
            partition_config=_yaml(context.config("partition.yaml")),
            geometry_config=_yaml(context.config("geometry.yaml")),
            output_dir=workspace,
        )
        manifest = _record_stage(
            context,
            workspace,
            stage,
            started,
            ["partition.yaml", "domains.yaml", "geometry.yaml"],
            {
                "candidate_count": qc["candidate_count"],
                "owned_active_rows": qc["ownership"]["owned_active_rows"],
                "unassigned_surface_conflict_phase_count": qc["surface_ownership"]["unassigned_surface_conflict_phase_count"],
                "unassigned_surface_conflict_mw": qc["surface_ownership"]["unassigned_surface_conflict_mw"],
                "unauthorized_source_vs_owner_mismatch_count": qc["surface_ownership"]["unauthorized_source_vs_owner_mismatch_count"],
                "evidence_overlap_pairs_after": qc["evidence_overlap_pairs_after"],
                "denominator_overlap_pairs_after": qc["denominator_overlap_pairs_after"],
            },
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage06(context):
    stage = "06_metrics"
    context.require_stage("01_ingest")
    context.require_stage("05_exclusive")
    started = utc_now()
    scoring = _yaml(context.config("scoring.yaml"))
    with context.stage_workspace(stage) as workspace:
        qc = run_evidence_metrics(
            ingest_dir=context.stage_dir("01_ingest"),
            exclusive_dir=context.stage_dir("05_exclusive"),
            candidate_registry_path=context.config("candidates.csv"),
            wpd_path=_gwa_wpd_source(context),
            output_dir=workspace,
            resource_metric_config=scoring["resource_metrics"],
            market_qc_tolerance_mw=float(scoring["market_qc_tolerance_mw"]),
        )
        manifest = _record_stage(
            context,
            workspace,
            stage,
            started,
            ["scoring.yaml:market_qc_tolerance_mw", "scoring.yaml:resource_metrics", "candidates.csv"],
            {
                "candidate_count": qc["candidate_count"],
                "resource_defined_count": qc["resource_defined_count"],
                "project_wpd_defined_count": qc["project_wpd_defined_count"],
                "coverage_defined_count": qc["coverage_defined_count"],
                "market_active_mw_qc_fail_count": qc["market_active_mw_qc_fail_count"],
                "raster_chunk_size": PRODUCTION_RASTER_CHUNK_SIZE,
            },
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage07(context):
    stage = "07_scores"
    context.require_stage("06_metrics")
    started = utc_now()
    strategic = load_strategic_s_contract(context.snapshot, context.config("strategic_s.yaml"))
    strategic_scores = strategic.score_frame("REFERENCE")
    assessment_rel = strategic.assessment_path.relative_to(context.snapshot).as_posix()
    rules_rel = strategic.rules_path.relative_to(context.snapshot).as_posix()
    with context.stage_workspace(stage) as workspace:
        qc = run_scores(
            metrics_dir=context.stage_dir("06_metrics"),
            scoring_config_path=context.config("scoring.yaml"),
            strategic_scores_source=strategic_scores,
            strategic_score_column="S_score",
            output_dir=workspace,
        )
        manifest = _record_stage(
            context,
            workspace,
            stage,
            started,
            ["scoring.yaml", assessment_rel, rules_rel, "strategic_s.yaml"],
            {
                "candidate_count": qc["candidate_count"],
                "score_complete_count": qc["score_complete_count"],
            },
        )
    update_run_stage(context.manifest_path, stage, manifest)


def _run_stage08(context):
    stage = "08_catalogue"
    context.require_stage("06_metrics")
    context.require_stage("07_scores")
    started = utc_now()
    with context.stage_workspace(stage) as workspace:
        qc = run_catalogue(
            metrics_dir=context.stage_dir("06_metrics"),
            scores_dir=context.stage_dir("07_scores"),
            candidate_registry_path=context.config("candidates.csv"),
            selection_config_path=context.config("selection.yaml"),
            output_dir=workspace,
        )
        manifest = _record_stage(
            context,
            workspace,
            stage,
            started,
            ["candidates.csv", "selection.yaml"],
            {
                "candidate_count": qc["candidate_count"],
                "evidence_selected_count": qc["evidence_selected_count"],
                "evidence_not_selected_count": qc["evidence_not_selected_count"],
            },
        )
    update_run_stage(context.manifest_path, stage, manifest)


STAGE_RUNNERS = {
    "00": _run_stage00,
    "01": _run_stage01,
    "02": _run_stage02,
    "03": _run_stage03,
    "04": _run_stage04,
    "05": _run_stage05,
    "06": _run_stage06,
    "07": _run_stage07,
    "08": _run_stage08,
}


def run_stage(context: RunContext, stage: str) -> None:
    try:
        runner = STAGE_RUNNERS[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown stage: {stage}") from exc
    runner(context)
