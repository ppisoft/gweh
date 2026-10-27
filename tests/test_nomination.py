"""One-source-catalogue integrity, independent of the candidate population size."""
import csv
import shutil
from pathlib import Path

import pytest

from tools.nomination import check_provenance

ROOT = Path(__file__).resolve().parents[1]


def _working_copy(tmp_path):
    for name in (
        "config/candidates.csv",
        "provenance/candidate_nomination_register.csv",
        "evidence/strategic_s/source_database.csv",
        "evidence/strategic_s/candidate_source_evidence.csv",
    ):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    return tmp_path


def test_nomination_matches_dynamic_candidate_universe():
    actual = {r["candidate_id"] for r in csv.DictReader((ROOT / "config/candidates.csv").open())}
    result = check_provenance(ROOT, actual)
    assert result["candidates"] == len(actual)
    assert result["associations"] >= len(actual)


def test_nomination_detects_missing_candidate(tmp_path):
    repo = _working_copy(tmp_path)
    path = repo / "provenance/candidate_nomination_register.csv"
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows[1:])
    with pytest.raises(ValueError, match="one row per configured candidate"):
        check_provenance(repo)


def test_nomination_detects_unknown_strategic_source(tmp_path):
    repo = _working_copy(tmp_path)
    path = repo / "evidence/strategic_s/candidate_source_evidence.csv"
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["source_id"] = "nonexistent_reference"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="Strategic-S references"):
        check_provenance(repo)
