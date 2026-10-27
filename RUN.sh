#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

usage() {
  cat <<'EOF'
GWEH – Global Wind Energy Hotspots
  ./RUN.sh config
  ./RUN.sh test
  ./RUN.sh nomination-check
  ./RUN.sh prepare --data-root PARENT [--scenario-id ID] [--dry-run]
  ./RUN.sh verify-source --data-root PARENT [--scenario-id ID]
  ./RUN.sh run SCENARIO_DATA_ROOT RUN_ID
  ./RUN.sh status SCENARIO_DATA_ROOT RUN_ID
  ./RUN.sh grid RUN_DIRECTORY GRID_OUTPUT_DIRECTORY
  ./RUN.sh cf-metadata RUN_DIRECTORY EXISTING_GRID_NC NEW_GRID_NC
  ./RUN.sh products RUN_DIRECTORY OUTPUT_DIRECTORY [--climate-roster CSV --cf-grid NC --cf-lookup CSV --validation-dir DIR]

  # Single local YAML for normal workflows (see workflow/example.local.yaml):
  ./RUN.sh science  --config workflow.local.yaml  # prepare, Stage00–08, grid
  ./RUN.sh validate --config workflow.local.yaml  # V1–V4, CF, archive
  ./RUN.sh validate --config workflow.local.yaml --steps merra-compare,archive
  ./RUN.sh omission --config workflow.local.yaml  # optional expert / unassigned-project screening
  ./RUN.sh public   --config workflow.local.yaml  # final CSV/GPKG/NC + V1–V4 validation
  ./RUN.sh viewer   --config workflow.local.yaml  # offline interactive data viewer
  ./RUN.sh release  --config workflow.local.yaml --output NEW_DIR # software/data ZIP + viewer
  ./RUN.sh release  --config workflow.local.yaml --output NEW_DIR --no-viewer
EOF
}

command="${1:-}"
if [[ $# -gt 0 ]]; then shift; fi
case "$command" in
  science|validate|public|viewer|omission|release)
    python "$ROOT/workflow/run.py" "$command" "$@"
    ;;
  config)
    python -m gweh config validate --repo "$ROOT"
    ;;
  nomination-check)
    python "$ROOT/tools/nomination.py" --repo "$ROOT"
    ;;
  test)
    python -m pytest -q "$ROOT/tests"
    ;;
  prepare)
    python "$ROOT/tools/source.py" --repo "$ROOT" "$@"
    ;;
  verify-source)
    python "$ROOT/tools/source.py" --repo "$ROOT" --verify "$@"
    ;;
  run)
    if [[ $# -ne 2 ]]; then usage >&2; exit 2; fi
    data_root="$1"; run_id="$2"
    # Never reuse a completed stage with a different source or numeric inputs.
    python - "$ROOT" "$data_root/runs/$run_id/manifest/run_manifest.json" <<'SCIENCE_CHECK'
import json
import sys
from pathlib import Path
from gweh.manifest import SNAPSHOT_INPUTS, combined_sha256, sha256_file

root = Path(sys.argv[1]); file = Path(sys.argv[2])
if file.is_file():
    previous = json.loads(file.read_text(encoding="utf-8"))
    current = combined_sha256(
        (p.relative_to(root).as_posix(), sha256_file(p))
        for p in sorted((root / "src").rglob("*.py"))
    )
    if current != previous.get("source_code_sha256"):
        raise SystemExit("[error] Scientific source differs from this run: use a fresh run ID")
    for relative in SNAPSHOT_INPUTS:
        if sha256_file(root / relative) != previous.get("snapshot_sha256", {}).get(relative):
            raise SystemExit(f"[error] {relative} differs from run inputs: use a fresh run ID")
SCIENCE_CHECK
    for stage in 00 01 02 03 04 05 06 07 08; do
      stage_dir="$data_root/runs/$run_id/${stage}_$(python -m gweh stage list | awk -v n="$stage" '$1==n {print $2}')"
      if [[ -f "$stage_dir/_stage.json" ]]; then
        # Completed stages are not overwritten, so interrupted runs can resume.
        python - "$stage_dir/_stage.json" <<'PY'
import json, sys
if json.load(open(sys.argv[1]))["status"] != "PASS":
    raise SystemExit("Cannot skip incomplete stage: " + sys.argv[1])
PY
        echo "[skip] completed stage $stage"
      else
        python -m gweh stage run "$stage" --repo "$ROOT" \
          --data-root "$data_root" --run-id "$run_id"
      fi
    done
    ;;
  status)
    if [[ $# -ne 2 ]]; then usage >&2; exit 2; fi
    python -m gweh stage status --data-root "$1" --run-id "$2"
    ;;
  grid)
    if [[ "${1:-}" == "--config" ]]; then
      python "$ROOT/workflow/run.py" grid "$@"
      exit $?
    fi
    if [[ $# -ne 2 ]]; then usage >&2; exit 2; fi
    python "$ROOT/tools/grid.py" --run-dir "$1" \
      --template "$ROOT/grid/CESM_192x288.nc" --output "$2" \
      --metadata "$ROOT/metadata/publication.yaml"
    ;;
  cf-metadata)
    if [[ $# -ne 3 ]]; then usage >&2; exit 2; fi
    python "$ROOT/tools/grid.py" --run-dir "$1" \
      --refresh-metadata "$2" --output "$3" \
      --metadata "$ROOT/metadata/publication.yaml"
    ;;
  products)
    if [[ $# -lt 2 ]]; then usage >&2; exit 2; fi
    run_directory="$1"; output_directory="$2"; shift 2
    python "$ROOT/tools/products.py" --run-dir "$run_directory" \
      --output "$output_directory" "$@"
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    usage >&2; exit 2
    ;;
esac
