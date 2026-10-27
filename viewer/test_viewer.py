"""Browser regression checks for the standalone GWEH data viewer."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


def exercise_html(html_file: Path, screenshot: Path | None = None,
                  record_file: Path | None = None) -> list[str]:
    """Exercise the released viewer using counts from its build record."""
    record_path = record_file or html_file.with_name("VIEWER_BUILD.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("status") != "PASS":
        raise ValueError(f"Viewer build record is not PASS: {record_path}")
    candidate_count = int(record["candidate_count"])
    grid_cell_count = int(record["active_grid_cell_count"])
    primary_count = int(record["primary_climate_panel"])
    if candidate_count <= 0 or grid_cell_count < 0 or not (0 <= primary_count <= candidate_count):
        raise ValueError(f"Invalid population counts in {record_path}")
    outcomes = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1620, "height": 980}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Some automated browser environments restrict file:// navigation.
        # Inserting the fully self-contained HTML tests the same client logic.
        page.set_content(html_file.read_text(encoding="utf-8"), wait_until="load", timeout=60000)
        assert page.locator("html").get_attribute("lang") == "en"
        assert page.get_by_text("Colour regions by").count() == 1
        assert page.locator(".region").count() == candidate_count
        assert page.locator(".grid-cell").count() == grid_cell_count
        assert int(page.locator("#cellStat").inner_text().replace(",", "")) == grid_cell_count
        assert "GWEH" in page.title()
        outcomes.append(
            f"Initial English UI: {candidate_count} candidates and "
            f"{grid_cell_count:,} active model-grid cells"
        )

        region = page.locator('.region[data-id="north_sea_offshore"]')
        region.click(timeout=20000)
        assert page.locator("#regionSelect").input_value() == "north_sea_offshore"
        assert "North Sea offshore" in page.locator("#regionDetail h3").inner_text()
        assert page.locator(".region-label:not(.hidden)").count() == 1
        outcomes.append("Pointer interaction: polygon selection, detailed statistics and click-only label")

        help_button = page.locator('.info-help[data-help="S_score"]')
        help_button.hover()
        assert page.locator("#hintPopup").is_visible()
        assert "Strategic evidence score" in page.locator("#hintPopup").inner_text()
        outcomes.append("Methodological glossary: English tooltips on hover")

        page.locator("#regionSelect").select_option("patagonia_onshore")
        assert "Patagonia onshore" in page.locator("#regionDetail h3").inner_text()
        assert page.locator('.region-label:not(.hidden)').get_attribute("data-id") == "patagonia_onshore"
        label_box = page.locator(".region-label:not(.hidden)").bounding_box()
        assert label_box is not None and 5 < label_box["height"] < 25
        assert "scale(" in page.locator("#viewport").get_attribute("transform")
        outcomes.append("Dropdown selection: zoom, regional detail and stable on-screen label")

        page.locator("#attribute").select_option("selection_pathway")
        legend = page.locator("#legendItems").inner_text()
        assert "Primary resource" in legend and "Not selected" in legend
        outcomes.append("Selection-pathway map and dynamic legend")

        page.locator("#gridToggle").check()
        page.locator("#selectionFilter").select_option("primary")
        assert int(page.locator("#candidateStat").inner_text().replace(",", "")) == primary_count
        assert 0 < page.locator(".grid-cell:not(.hidden)").count() < grid_cell_count
        outcomes.append("Primary-panel filter also filters CESM grid-cell outlines")

        page.locator("#regionSelect").select_option("north_sea_offshore")
        assert page.locator("#regionSelect").input_value() == "north_sea_offshore"
        assert "North Sea offshore" in page.locator("#regionDetail h3").inner_text()
        outcomes.append("Dropdown selection remains functional when a filter is applied")

        page.locator("#reset").click()
        chosen_before = page.locator("#regionSelect").input_value()
        region = page.locator('.region[data-id="north_sea_offshore"]')
        bbox = region.bounding_box()
        assert bbox is not None
        x, y = bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 55, y + 35, steps=8)
        page.mouse.up()
        assert page.locator("#regionSelect").input_value() == chosen_before
        assert page.locator("#viewport").get_attribute("transform") != "translate(0 0) scale(1)"
        outcomes.append("Pan map by dragging without changing the selection")

        page.locator(".viewer-provenance summary").click()
        assert "Natural Earth" in page.locator(".viewer-provenance").inner_text()
        assert len(page.locator("#sourceHash").inner_text()) > 10
        outcomes.append("Provenance panel explains source files, roles and background rights")

        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)

        with page.expect_download() as result:
            page.locator("#saveSvg").click()
        download = result.value
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "map.svg"
            download.save_as(str(p))
            text = p.read_text(encoding="utf-8")
            assert "<style" in text
            assert "viewBox=\"0 0 1200 610\"" in text
            assert "Natural Earth (public domain)" in text
            assert "GSHHG" not in text
        outcomes.append("SVG export includes styles and correct Natural Earth attribution")

        assert not errors, errors
        outcomes.append("Chromium: no uncaught JavaScript errors")
        browser.close()
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--record", type=Path,
                        help="Viewer build record (default: VIEWER_BUILD.json beside HTML)")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    for outcome in exercise_html(args.html, args.screenshot, args.record):
        print("PASS", outcome)


if __name__ == "__main__":
    main()
