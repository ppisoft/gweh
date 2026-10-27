# Independent GWEH validation (V1–V4)

The validation uses the input catalogue, polygons and climate grid
roster. No total number of candidates, selected regions or grid-eligible
regions is assumed. The production science calculation is independent of V1–V4.

## Recommended use: one configuration file

Copy `workflow/example.local.yaml` to `workflow.local.yaml`, set the source
paths and output directories, then run:

```bash
./RUN.sh science --config workflow.local.yaml    # GWPT scenario, 00–08, CESM grid
./RUN.sh validate --config workflow.local.yaml   # V1, V2, V3, fresh V4, CF, archive
./RUN.sh public --config workflow.local.yaml     # CSV/GPKG/NC + validation results
```

Each step reuses verified existing output and refuses inconsistent/incomplete
outputs. MERRA-2 aggregation saves a checkpoint for each calendar year. 
Existing Stage00–08 and MERRA yearly results are not recomputed when 
completing just the report:

```bash
./RUN.sh validate --config workflow.local.yaml --steps merra-compare,archive
```

Use new paths for `merra_result_dir` and `validation_archive_dir` when replacing
an old report. See `workflow/README.md` for the current workflow and server setup.

## V1 and V2 — independent location corroboration

`validation/spatial.py` compares GOWIRES and Global Renewables Watch (onshore)
and DeepOWT plus the configured ground-truth subsets (offshore) with mutually
exclusive selected region and denominator polygons. The ground-truth
polygon centroids are computed in a projected coordinate system. Its
`conditional_containment` measures same-candidate containment within selected
denominators. Source rows and per-candidate rows are cross-checked during archiving.
The reports also include `scoped_region_area_fraction_point_weighted`: for each
candidate, compute the WGS84 geodesic area of `region ∩ denominator` divided by
its denominator area, then average these fractions weighted by the number of
source points within each denominator. `excess_containment_over_area_fraction`
is the difference from the observed conditional containment. This is a simple
geometric reference, and is not used in selection or scoring.

## V3 — country × technology capacity corroboration

`validation/market.py` compares GWPT operating phases with IRENASTAT country
onshore/offshore totals. Positive-pair Spearman correlations are conditional on
both inventories reporting positive capacity, not global agreement of their
absolute capacities. Source snapshots with duplicate country/technology totals are rejected.

## V4 — genuinely new MERRA-2 aggregation

The current V4 aggregates hourly MERRA-2 (`U50M`, `V50M`, `PS`, `T2M`, `QV2M`,
optional `U10M`, `V10M`) for the configured interval, currently 2008–2017.
For each time and cell centre inside a blinded region polygon:

- `WS50 = sqrt(U50M² + V50M²)`
- `rho = PS / (287.05*T2M*(1 + 0.61*QV2M))`
- `WPD50 = 0.5*rho*WS50³`

Means use valid hourly samples weighted by `cos(latitude)`. Regional rankings
are compared with independent GWA4 `WPD100_mean_aw`. 

V4 keeps anonymized polygons and their supervisor mapping separate until the
hourly aggregation finishes. `merra.py compare`  requires the current
`GWEH_grid_candidate_roster.csv`: without its explicit
`climate_analysis_eligible` and `primary_climate_panel` fields, the code
refuses to report climate subsets. It does not infer them from evidence
selection; explicit roster categories are mandatory.

For direct expert operation, outside the convenience runner:

```bash
PYTHONPATH=src python validation/merra.py prepare \
  --run-dir "$RUN_DIR" --output "$VALID_DIR/merra_work"
PYTHONPATH=src python validation/merra.py aggregate \
  --blind-dir "$VALID_DIR/merra_work/blind" \
  --merra-root "$MERRA_ROOT" --start 2008-01-01 --end 2017-12-31 \
  --output "$VALID_DIR/merra_work/annual"
PYTHONPATH=src python validation/merra.py compare \
  --annual-dir "$VALID_DIR/merra_work/annual" \
  --mapping "$VALID_DIR/merra_work/supervisor/mapping.csv" \
  --catalogue "$RUN_DIR/08_catalogue/catalogue.csv" \
  --metrics "$RUN_DIR/06_metrics/region_metrics.csv" \
  --roster "$GRID_DIR/GWEH_grid_candidate_roster.csv" \
  --output "$VALID_DIR/merra_corrected"
```

Do not overwrite original MERRA-2 source files or an accepted reference run.

## Validation archive

`validation/prepare.py` verifies the independent source population, the
source-level vs region-level V1/V2 counts, the V3 market tables, and the
correctly stratified V4 population against the actual grid roster. It requires
a passing CF report for the complete archive.

```bash
PYTHONPATH=src python validation/prepare.py \
  --validation-root "$VALID_DIR" --merra "$VALID_DIR/merra_corrected" \
  --merra-work "$VALID_DIR/merra_work" \
  --roster "$GRID_DIR/GWEH_grid_candidate_roster.csv" \
  --cf-report "$GRID_DIR/CF11_IOOS_CHECK.txt" \
  --cf-grid "$GRID_DIR/GWEH_grid_CESM_192x288.nc" \
  --cf-sha256-file "$GRID_DIR/CF_GRID_SHA256.txt" \
  --require-complete --output "$VALID_DIR/archive_corrected"
```

The archive contains the V1–V4 results, the complete CF report, methodology,
source settings and small MERRA yearly checkpoint/provenance files. Raw
third-party MERRA, GOWIRES, GRW, DeepOWT or IRENA data are not redistributed.

## Public V4 geometry identity (compact release)

The V4 `prepare` and `aggregate` steps still use the full, frozen
`merra_work/blind/regions.gpkg`. The external validation assembler verifies
its SHA-256 against the original aggregation context, then exports only
`merra_provenance/blind/geometry_fingerprints.csv` (per-ID WKB SHA-256) in
the public validation archive. `tools/products.py` and
`publication/package.py` verify 1:1 equality with the published
`GWEH_geometries.gpkg` layer through `supervisor/mapping.csv`. Preserve the
working V4 checkpoint separately if the original blind-file bytes or the
chronology of the validation must be inspected; a matching WKB hash alone
does not demonstrate when the freeze occurred.
