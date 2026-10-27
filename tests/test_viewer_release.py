"""Regression checks for the optional viewer release contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from publication.package import verify_viewer


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _fixture(tmp_path):
    repo = tmp_path / "repo"
    pub = tmp_path / "public"
    view = tmp_path / "viewer_result"
    (repo / "viewer/assets").mkdir(parents=True)
    pub.mkdir()
    (pub / "metadata").mkdir()
    (pub / "metadata/publication.yaml").write_text("product_version: 1.0.0\n")
    view.mkdir()
    paths = {
        "template_sha256": repo / "viewer/dashboard_template.html",
        "generator_sha256": repo / "viewer/build_viewer.py",
        "background_sha256": repo / "viewer/assets/natural_earth_110m_countries.geojson",
    }
    for i, f in enumerate(paths.values()):
        f.write_text(f"frozen source {i}")
    (pub / "GWEH_catalogue.csv").write_text("candidate_id\na\n")
    (pub / "MANIFEST_SHA256.csv").write_text(
        "file,sha256\nGWEH_catalogue.csv," + _digest(pub / "GWEH_catalogue.csv") + "\n"
    )
    html = view / "GWEH_explorer.html"
    html.write_text("<html>static viewer</html>")
    record = {
        "status": "PASS",
        "dataset_version": "1.0.0",
        "viewer_size_bytes": html.stat().st_size,
        "viewer_sha256": _digest(html),
        "input_sha256": {"GWEH_catalogue.csv": _digest(pub / "GWEH_catalogue.csv")},
        **{name: _digest(file) for name, file in paths.items()},
    }
    (view / "VIEWER_BUILD.json").write_text(json.dumps(record))
    return repo, pub, view


def test_viewer_release_accepts_matching_provenance(tmp_path):
    repo, public, viewer = _fixture(tmp_path)
    assert verify_viewer(viewer, public, repo)["status"] == "PASS"


def test_viewer_release_rejects_modified_html(tmp_path):
    repo, public, viewer = _fixture(tmp_path)
    (viewer / "GWEH_explorer.html").write_text("changed")
    with pytest.raises(ValueError, match="content"):
        verify_viewer(viewer, public, repo)


def test_viewer_release_rejects_changed_dataset(tmp_path):
    repo, public, viewer = _fixture(tmp_path)
    (public / "GWEH_catalogue.csv").write_text("candidate_id\nb\n")
    (public / "MANIFEST_SHA256.csv").write_text(
        "file,sha256\nGWEH_catalogue.csv," + _digest(public / "GWEH_catalogue.csv") + "\n"
    )
    with pytest.raises(ValueError, match="outdated or different"):
        verify_viewer(viewer, public, repo)


def test_viewer_release_rejects_modified_generator(tmp_path):
    repo, public, viewer = _fixture(tmp_path)
    (repo / "viewer/build_viewer.py").write_text("changed")
    with pytest.raises(ValueError, match="source snapshot"):
        verify_viewer(viewer, public, repo)
