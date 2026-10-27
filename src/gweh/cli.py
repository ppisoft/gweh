from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import validate_repository
from .manifest import init_run
from .paths import DataPaths, RepositoryPaths
from .pipeline import CORE_STAGES, run_stage
from .run import RunContext

STAGE_NAMES = {number: name for number, name in CORE_STAGES}


def _repo(value):
    return RepositoryPaths.discover(Path(value).resolve() if value else None)


def _data(value):
    return DataPaths.resolve(value)


def cmd_config_validate(args: argparse.Namespace) -> int:
    report = validate_repository(_repo(args.repo))
    for issue in report.issues:
        print(f"[{issue.level}] {issue.code}: {issue.message}")
    if report.ok:
        print("[PASS] configuration contract")
        return 0
    return 2


def cmd_stage_list(_args: argparse.Namespace) -> int:
    for number, name in CORE_STAGES:
        print(f"{number}  {name}")
    return 0


def _ensure_run(args):
    repo = _repo(args.repo)
    data = _data(args.data_root)
    run_dir = data.run(args.run_id)
    manifest = run_dir / "manifest" / "run_manifest.json"
    if not manifest.exists():
        if args.stage != "00":
            raise RuntimeError(f"Run {args.run_id!r} does not exist; start with Stage 00")
        report = validate_repository(repo)
        if not report.ok:
            for issue in report.issues:
                print(f"[{issue.level}] {issue.code}: {issue.message}", file=sys.stderr)
            raise RuntimeError("Repository configuration validation failed")
        init_run(repo, data, args.run_id)
        print(f"[INIT] created production run {args.run_id} in {run_dir}")
    return RunContext(data, args.run_id)


def cmd_stage_run(args: argparse.Namespace) -> int:
    context = _ensure_run(args)
    run_stage(context, args.stage)
    print(f"[PASS] Stage {args.stage} {STAGE_NAMES[args.stage]} completed for run {args.run_id}")
    return 0


def cmd_stage_status(args: argparse.Namespace) -> int:
    context = RunContext(_data(args.data_root), args.run_id)
    manifest = context.manifest()
    print(
        f"GWEH run={args.run_id} boundary={manifest.get('boundary_profile')} created={manifest.get('created_utc')}"
    )
    state = manifest.get("stages", {})
    for number, name in CORE_STAGES:
        stage_name = f"{number}_{name}"
        path = context.stage_dir(stage_name) / "_stage.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            status = payload.get("status", "UNKNOWN")
            completed = payload.get("completed_utc", "")
        else:
            recorded = state.get(stage_name, {})
            status = recorded.get("status", "NOT_RUN")
            completed = recorded.get("completed_utc", "")
        suffix = f"  {completed}" if completed else ""
        print(f"{number}  {name:<12} {status}{suffix}")
    return 0


def _common_run_args(parser):
    parser.add_argument("--repo")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--run-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gweh", description="GWEH regionalization pipeline")
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate")
    validate.add_argument("--repo")
    validate.set_defaults(func=cmd_config_validate)

    stage = commands.add_parser("stage")
    stage_commands = stage.add_subparsers(dest="stage_command", required=True)
    stage_list = stage_commands.add_parser("list")
    stage_list.set_defaults(func=cmd_stage_list)
    stage_run = stage_commands.add_parser("run")
    stage_run.add_argument("stage", choices=[number for number, _ in CORE_STAGES])
    _common_run_args(stage_run)
    stage_run.set_defaults(func=cmd_stage_run)
    stage_status = stage_commands.add_parser("status")
    _common_run_args(stage_status)
    stage_status.set_defaults(func=cmd_stage_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
