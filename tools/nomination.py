#!/usr/bin/env python3
"""Check compact nomination coverage and the referenced Strategic-S bibliography.

Nomination is not an additional score or a source of polygon boundaries.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def _rows(path: Path, columns: set[str]):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = columns - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
        return list(reader)


def check_provenance(repo: Path, expected_candidate_ids: set[str] | None = None) -> dict:
    repo = Path(repo)
    candidates = _rows(repo / "config/candidates.csv", {"candidate_id"})
    ids = [row["candidate_id"] for row in candidates]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("Duplicate or empty configured candidate IDs")
    universe = set(ids)
    if expected_candidate_ids is not None and universe != set(expected_candidate_ids):
        raise ValueError("Nomination and science catalogues have different candidate universes")

    nominations = _rows(repo / "provenance/candidate_nomination_register.csv",
                        {"candidate_id", "nomination_basis"})
    if any(set(row) != {"candidate_id", "nomination_basis"} for row in nominations):
        raise ValueError("Nomination register has unexpected fields")
    registered = [row["candidate_id"] for row in nominations]
    if len(registered) != len(set(registered)) or set(registered) != universe:
        raise ValueError("Nomination register must have one row per configured candidate")
    if any(not row["nomination_basis"].strip() for row in nominations):
        raise ValueError("A candidate has no documented nomination basis")

    sources = _rows(repo / "evidence/strategic_s/source_database.csv",
                    {"source_id", "title", "publication_date", "url_exact"})
    source_ids = [row["source_id"] for row in sources]
    if len(source_ids) != len(set(source_ids)) or not all(source_ids):
        raise ValueError("Strategic-S source identifiers must be unique and non-empty")
    if any(not row["title"].strip() or not row["url_exact"].startswith(("https://", "http://"))
           for row in sources):
        raise ValueError("Strategic-S sources need an identified title and exact URL")

    links = _rows(repo / "evidence/strategic_s/candidate_source_evidence.csv",
                  {"candidate_id", "source_id", "parameter", "parameter_value"})
    if (set(row["candidate_id"] for row in links) != universe
            or any(row["source_id"] not in source_ids or
                   not row["parameter"].strip() or not row["parameter_value"].strip()
                   for row in links)):
        raise ValueError("Strategic-S references must resolve for all current candidates")
    if len(links) != len({(r["candidate_id"], r["source_id"], r["parameter"], r["parameter_value"])
                          for r in links}):
        raise ValueError("Duplicate candidate/source/parameter association")
    return {"candidates": len(universe), "sources": len(sources), "associations": len(links)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print("[PASS] nomination and Strategic-S bibliography:", check_provenance(args.repo))


if __name__ == "__main__":
    main()
