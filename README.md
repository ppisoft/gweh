# GWEH – Global Wind Energy Hotspots

Reproducible global wind-energy regionalization for climate and Earth-system research.

| | |
|---|---|
| **Version** | 1.0.0 |
| **Repository** | https://github.com/ppisoft/gweh |
| **Software DOI** | [10.5281/zenodo.23555661](https://doi.org/10.5281/zenodo.23555661) |
| **Dataset DOI** | [10.5281/zenodo.23555696](https://doi.org/10.5281/zenodo.23555696) |
| **Language** | Python 3, conda / geospatial stack |
| **Licence** | MIT (code), CC BY 4.0 (GWEH data products) |

GWEH combines wind-resource characteristics, existing deployment, committed project pipelines and documented strategic evidence, and on this basis delineates onshore, offshore and mixed hotspot regions. It is used for reproducible spatial aggregation and comparison of meteorological and climate-model outputs.

The reference dataset contains 65 nominated candidates, of which 60 satisfy the evidence-selection criteria. On the CESM 192 × 288 reference grid, 58 selected regions satisfy the minimum grid-representation criterion and 49 of them form the primary climate-analysis panel.

GWEH is not an exhaustive inventory of worldwide wind resources, a turbine-siting model or a map of economically developable capacity.

## Quick start

```bash
git clone https://github.com/ppisoft/gweh.git
cd gweh

conda env create -f environment.yml
conda activate gweh

./RUN.sh config          # verification of the numerical configuration
./RUN.sh test            # repository regression tests
```

A production run requires the upstream datasets (Section 3) and a local configuration (Section 4.1):

```bash
cp workflow/example.local.yaml workflow.local.yaml
# edit the paths

./RUN.sh science  --config workflow.local.yaml
./RUN.sh validate --config workflow.local.yaml
./RUN.sh public   --config workflow.local.yaml
./RUN.sh viewer   --config workflow.local.yaml  # offline companion HTML
```

## Related documents

| File | Contents |
|---|---|
| `CONTRIBUTING.md` | Rules for changes in a frozen scientific release |
| `SIGNOFF.md` | Verification record of release 1.0.0 |
| `DATASET_README.md` | Guide to the data product (released as the dataset `README.md`) |
| `DATA_LICENSE.md` | Licence and rights scope of the data products |
| `DATA_RIGHTS.md` | Source data and software redistribution |
| `CITATION.cff` | Machine-readable software citation |
| `validation/README.md` | Low-level validation commands |
| `viewer/README.md` | Offline data viewer, regeneration and optional browser QA |

## 1. Repository structure

| Directory | Contents |
|---|---|
| `src/gweh/` | Scientific regionalization algorithms, Stage00–08 |
| `config/` | Numerical parameters, geometry rules, candidate definitions and selection configuration |
| `evidence/` | Strategic-S evidence, source links, assessments and scoring rules |
| `tools/` | Source preparation, model-grid generation, public product export |
| `workflow/` | Unified configuration and execution of production workflows |
| `grid/` | Coordinate-only reference climate-grid template (`lat`, `lon`; no model data) |
| `validation/` | Independent spatial, market, resource and CF validation |
| `omission/` | Optional independent candidate-omission screening |
| `publication/` | Public data and software packaging, release verification |
| `viewer/` | Optional interactive data viewer, template, public-domain background and tests |
| `metadata/` | Publication metadata and data dictionary |
| `provenance/` | Source register and concise expert-nomination basis |
| `tests/` | Regression and scientific-contract tests |
| `verification/` | Comparison of a run against a verified reference run |
| `reproducibility/` | Specifications of the original production environment |

The scientific pipeline, independent validation and publication workflows are separate. Validation results do not feed back into the scientific selection or the regional geometries.

## 2. Environment and dependencies

The reference production environment was Linux x86_64 with a documented Conda and geospatial stack.

Closer reproduction of the accepted production environment:

```bash
conda create \
  --name gweh-repro \
  --file reproducibility/meop40-linux64/conda-explicit-linux64.txt

conda activate gweh-repro
```

The exact environment record contains the software versions and binary dependencies of the original production run. Creating a similar environment on another system does not by itself guarantee byte-identical geospatial results.

Creating a new CF validation report requires the external IOOS compliance checker. It is installed separately and configured through the `cf_checker` key in the local workflow YAML.

## 3. Input data

Principal source datasets:

- Global Wind Power Tracker (GWPT), February 2026 reference snapshot
- Global Wind Atlas 4 (GWA4), 100-m wind-power density
- Natural Earth geographical layers
- Marine Regions maritime boundaries
- Institutional and strategic evidence sources

Independent validation additionally uses GOWIRES, Global Renewables Watch, DeepOWT, IRENASTAT and MERRA-2.

Source versions, attribution, identifiers and applicable terms are documented in `provenance/source_register.csv` and in the data-product metadata. Original upstream datasets are not part of the repository and are obtained from their providers.

The reference GWPT workflow expects the specified February 2026 workbook and applies the three source-cell corrections listed in `config/gwpt_source_overrides.csv`. The corrections are bound to the SHA-256 of the original workbook; the source-preparation tool verifies the workbook identity and rejects unapproved modifications.

## 4. Configuration and production run

### 4.1 Workflow configuration

```bash
cp workflow/example.local.yaml workflow.local.yaml
```

```yaml
data_parent: /path/to/regions-data

scenario_id: gweh-study-001
run_id: gweh-run-001

external_root: /path/to/external-inputs

external:
  gowires: raw/GOWIRES/GOWIRES_V1.csv
  grw: raw/GRW/wind_all_2024q2_v1.gpkg
  deepowt: raw/DeepOWT/DeepOWT.geojson
  deepowt_gt:
    - raw/DeepOWT/gt_2021Q2_nsb.geojson
    - raw/DeepOWT/gt_2021Q2_ecs.geojson
    - raw/DeepOWT/gt_2021Q2_vtn.geojson
  irena:
    - raw/IRENASTAT/C-ELECCAP_SOURCE.csv
  merra_root: raw/MERRA2

merra:
  start: '2008-01-01'
  end: '2017-12-31'
```

Optional keys:

| Key | Purpose |
|---|---|
| `grid_template` | Alternative climate grid |
| `grid_dir` | Existing or new grid output directory |
| `validation_dir` | Validation result directory |
| `merra_work_dir` | MERRA-2 aggregation workspace |
| `merra_result_dir` | MERRA-2 comparison output |
| `validation_archive_dir` | Complete validation archive |
| `public_dir` | Public dataset directory |
| `viewer_dir` | Optional HTML viewer and build-record output directory |
| `omission_dir` | Output of the optional omission analysis |
| `cf_checker` | Executable IOOS CF compliance checker |
| `cf_report` | Existing CF validation report |
| `omission.expert_proposals` | Expert-supplied candidates |
| `omission.unowned_radius_km` | Radius for unassigned-project screening |

Relative paths under `external` are resolved against `external_root`; other relative paths against the directory of the YAML configuration.

A local `workflow.local.yaml`, credentials and raw upstream data do not belong in the public repository.

### 4.2 MERRA-2 input data (V4 validation)

The V4 validation uses hourly MERRA-2 `M2T1NXSLV` data for 2008–2017. The `external.merra_root` key points to the directory with the downloaded NetCDF files. The directory is searched recursively for `.nc` and `.nc4` files; a fixed year/month directory structure is not required. For example, the following is supported:

```text
raw/MERRA2/
└── M2T1NXSLV_5.12.4/
    ├── 2008/
    │   ├── 01/
    │   │   ├── MERRA2_300.tavg1_2d_slv_Nx.20080101.SUB.nc
    │   │   └── ...
    │   └── 12/
    ├── ...
    └── 2017/
        └── 12/
```

**Input requirements:** exactly one daily file for each date in the selected period, with a `YYYYMMDD` date in the filename and 24 hourly records. Each file contains `U50M`, `V50M`, `PS`, `T2M`, `QV2M` and one-dimensional latitude and longitude coordinates on a consistent grid. `U10M` and `V10M` are optional.

Missing or duplicate daily files, an inconsistent grid or missing variables terminate the validation with an error.

To run V4 separately:

```bash
./RUN.sh validate --config workflow.local.yaml \
  --steps merra-prepare,merra-aggregate,merra-compare
```

The aggregation produces verified yearly checkpoints, so completed years can be reused after an interruption. `merra_root` is resolved against `external_root`, not against the directory from which `RUN.sh` is executed.

### 4.3 Complete production run

```bash
./RUN.sh config
./RUN.sh test

./RUN.sh science  --config workflow.local.yaml
./RUN.sh validate --config workflow.local.yaml
./RUN.sh public   --config workflow.local.yaml
./RUN.sh viewer   --config workflow.local.yaml

./RUN.sh release \
  --config workflow.local.yaml \
  --output /path/to/new/deposit
```

The `release` command creates local files only; it neither uploads nor publishes any data.

## 5. `RUN.sh` commands

The unified workflow commands take `--config`; the other ten commands provide direct expert entry points.

| Command | Operation |
|---|---|
| `science` | Source preparation, Stage00–08 and grid |
| `validate` | Independent V1–V4 and CF validation |
| `omission` | Optional read-only candidate-omission screening |
| `public` | Assembly of the public data product |
| `viewer` | Build or verify the optional offline viewer from the public dataset |
| `release` | Assembly of software/data ZIPs and optional viewer companion files |
| `config` | Verification of the scientific configuration |
| `test` | Execution of the repository tests |
| `nomination-check` | Verification of candidate nomination metadata |
| `prepare` | Preparation of a corrected and verified GWPT scenario |
| `verify-source` | Verification of an existing prepared scenario |
| `run` | Direct execution of Stage00–08 |
| `status` | Listing of scientific stage status |
| `grid` | Build or verify the climate-grid representation |
| `cf-metadata` | Refresh metadata in an existing grid product |
| `products` | Export of public products from a completed run |

## 6. Scientific construction

### 6.1 Standard run

```bash
./RUN.sh science --config workflow.local.yaml
```

This command prepares the reference source scenario if it does not exist, verifies the source files and their identity, executes scientific stages 00–08 and builds or verifies the corresponding grid product.

Processing includes source registration, candidate-domain construction, resource and project evidence, geometry delineation, exclusive spatial partitioning, regional metrics, scoring, selection and assembly of the final catalogue. Completed scientific stages are not overwritten automatically.

### 6.2 Direct Stage00–08 execution

```bash
./RUN.sh prepare --data-root /path/to/regions-data --scenario-id gweh-study-001
./RUN.sh verify-source --data-root /path/to/regions-data --scenario-id gweh-study-001

./RUN.sh run    /path/to/regions-data/science_scenarios/gweh-study-001/data gweh-run-001
./RUN.sh status /path/to/regions-data/science_scenarios/gweh-study-001/data gweh-run-001
```

Optional `prepare` arguments: `--scenario-id ID` selects the source scenario identifier, `--dry-run` verifies readiness without creating the scenario, `--source-only` runs a check of the workbook alone.

The direct runner processes stages in numerical order and skips stages marked `PASS`, so an interrupted run can be completed. Before resuming, the source-code fingerprint and the checksums of the scientific inputs are compared with the recorded run manifest. Changing the scientific implementation or the numerical inputs requires a new run identifier.

## 7. Climate-grid representation

### 7.1 Standard workflow

```bash
./RUN.sh grid --config workflow.local.yaml
```

Builds or verifies the grid representation using the template configured in the workflow YAML.

### 7.2 Direct generation

```bash
./RUN.sh grid /path/to/completed/run /path/to/new/grid_output
```

This form uses the repository default template `grid/CESM_192x288.nc`. For a different grid use the procedure in Section 7.3.

The resulting product contains a CF NetCDF grid, a region-ID lookup and a candidate grid-representation roster. The vector geometries remain authoritative; cells are classified by the cell-centre inclusion rule and coarse-grid eligibility does not change the corresponding vector region.

### 7.3 Export to a different model grid

GWEH is not tied to the CESM 192 × 288 reference grid. Any model grid can be used whose template satisfies the requirements below.

**Template requirements.** The template is read through `xarray`, with an HDF5 fallback for simple files. It contains one-dimensional coordinate variables named `lat` and `lon`, at least two values on each axis, finite values and strictly ascending values on both axes. A template that fails any requirement is rejected with an explicit error; coordinates are neither completed nor re-sorted.

Longitude may use the −180…180 or the 0…360 convention; values are normalised internally before classification. The latitude and longitude attributes are rewritten to CF `units`, `standard_name` and `axis` values. Other template variables are ignored.

**Configured procedure.** Set `grid_template` in the local workflow YAML:

```yaml
grid_template: /path/to/MPI_ESM_192x96.nc
grid_dir: /path/to/new/grid_output
```

```bash
./RUN.sh grid     --config workflow.local.yaml
./RUN.sh validate --config workflow.local.yaml --steps cf,archive
./RUN.sh public   --config workflow.local.yaml
```

The output filename is derived from the template: a template `MPI_ESM_192x96.nc` gives `GWEH_grid_MPI_ESM_192x96.nc`. The CF validation and the public export derive the same name automatically.

**Direct expert invocation.** The two-argument form `./RUN.sh grid` always uses the repository default template. For a different template without the workflow YAML:

```bash
python tools/grid.py \
  --run-dir /path/to/completed/run \
  --template /path/to/MPI_ESM_192x96.nc \
  --output /path/to/new/grid_output \
  --metadata metadata/publication.yaml
```

The output directory must be new.

**Publication metadata.** `metadata/publication.yaml` describes the reference product and contains grid-bound values:

```yaml
title: 'GWEH: Global Wind Energy Hotspots on the CESM 192 x 288 grid'
id: gweh_grid_cesm_192x288_v1.0
```

When exporting to a different grid, supply an edited metadata file with a corresponding `title` and `id`. Using the reference metadata produces a NetCDF whose identifier and title describe the CESM grid regardless of the template actually used.

**Interpreting the result.** The grid-representation tiers are defined by the absolute number of cells, not by a fraction of the region area:

| Centres in region | Tier |
|---|---|
| ≥ 12 | `PRIMARY_CLIMATE_N_GE12` |
| 8–11 | `LOW_N_EXTENSION_8_11` |
| 2–7 | `NARROW_CLIMATE_2_7` |
| 1 | `EXCLUDED_CLIMATE_SINGLE_CELL` |
| 0 | `EXCLUDED_CLIMATE_UNREPRESENTED` |

The thresholds are therefore resolution-dependent. On a finer grid more regions reach the primary panel, on a coarser grid fewer do, and the `EXCLUDED_CLIMATE_UNREPRESENTED` tier can appear, which is empty on the CESM reference grid. Region counts obtained on different grids are not directly comparable, and the published figures 58 and 49 apply only to the CESM 192 × 288 reference grid.

Evidence selection remains unchanged: all 60 selected regions are selected on any grid, and all 65 candidates remain in the catalogue and in the vector layers.

**Validation on a new grid.** A new NetCDF requires a new CF check and a new checksum:

```bash
./RUN.sh validate --config workflow.local.yaml --steps cf,archive
```

Validations V1–V4 are computed from the native vector geometries and do not depend on the grid. Only the CF component and the grid-representation roster are grid-specific.

### 7.4 Refreshing CF metadata

```bash
./RUN.sh cf-metadata \
  /path/to/completed/run \
  /path/to/existing/grid.nc \
  /path/to/new/grid.nc
```

Writes a new NetCDF product according to `metadata/publication.yaml`. A new or changed NetCDF requires a corresponding current CF check and checksum.

## 8. Independent validation

```bash
./RUN.sh validate --config workflow.local.yaml
```

| Component | Purpose |
|---|---|
| V1/V2 spatial | Containment of independent location data: GOWIRES, GRW, DeepOWT |
| V3 market | Comparison of GWPT and IRENASTAT by country × technology |
| V4 resource | Ranking of the regional MERRA-2 WPD50 proxy against GWA4 WPD100 |
| CF | NetCDF compliance with CF-1.11 |
| Archive | Verified assembly of the independent validation results |

### 8.1 Selecting steps

```bash
./RUN.sh validate --config workflow.local.yaml --steps spatial,market
```

| Step | Operation |
|---|---|
| `spatial` | V1/V2 spatial validation |
| `market` | V3 comparison by country × technology |
| `merra-prepare` | Outcome-blind preparation of regions for MERRA-2 |
| `merra-aggregate` | Hourly MERRA-2 aggregation |
| `merra-compare` | Comparison of regional WPD ranking (V4) |
| `cf` | Execution or verification of the CF checker |
| `archive` | Assembly and verification of the V1–V4 and CF evidence |

The default workflow performs all seven steps in the order listed. Steps are executed in the fixed order independently of their order in the list. The `archive` step on its own requires all validation results and a passing CF report to exist already.

### 8.2 MERRA-2 checkpoints

The MERRA-2 aggregation stores yearly results and provenance information. An interrupted aggregation can be completed from verified yearly checkpoints without repeating the whole hourly processing. A completed aggregation can be compared and archived without reopening the original hourly data.

For the compact V4 public release, **keep** the existing `merra_work_dir` with its full frozen `blind/regions.gpkg` and yearly checkpoints, but point `validation_archive_dir` to a **new directory**. Reassemble the validation archive without recomputing hourly MERRA-2 or Stage00–08:

```bash
./RUN.sh validate --config workflow.local.yaml --steps archive
```

The new archive contains `blind/geometry_fingerprints.csv` instead of a duplicate GeoPackage; public export checks WKB equality against the main region layer. Set new `public_dir` and `viewer_dir` before regenerating the downstream products. Old-style validation archives are not silently reused. The original blind file checksum remains in the published aggregation context, while the original file stays in the working checkpoint directory. The fingerprint itself proves identity, not chronological outcome blindness.

Low-level expert commands are described in `validation/README.md`.

### 8.3 V3 and source-data rights

V3 uses positive GWPT–IRENASTAT pairs by country × technology.

The public validation product distributes GWPT capacities and the ordinal ranks of both sources. Absolute IRENASTAT capacities are not published. The Spearman rank correlations are fully reproducible from the published rank columns; the aggregate ratio statistics remain in the validation summary.

Third-party input data retain their original licences and conditions; details are in Section 14 and in `provenance/source_register.csv`.

## 9. Optional omission screening

Omission screening is a separate read-only research operation.

```yaml
omission:
  expert_proposals: /path/to/proposals.csv
  unowned_radius_km: 100
```

```bash
./RUN.sh omission --config workflow.local.yaml
```

The workflow compares expert-nominated regions with the existing selected regions and reference denominators. Optionally it also identifies spatial groups of active projects that remain unassigned. The outputs are `expert_omission_review.csv`, `unowned_active_proximity_groups.csv` and `OMISSION_REVIEW.json`; only the output corresponding to the configured modes has substantive content.

Omission screening does not change Stage00–08, the candidate catalogue, the regional geometry, the evidence scores or the selection decisions. It is not required for the release of the reference dataset.

## 10. Public data products

### 10.1 Unified export

```bash
./RUN.sh public --config workflow.local.yaml
```

The export requires completed scientific products, a corresponding climate grid and a complete independent validation archive. It assembles the public dataset:

- EPSG:4326 GeoPackage of regions and reference denominators
- candidate catalogue and evidence-selection status
- regional resource and market metrics
- R/D/P/S evidence scores
- market-component information
- NetCDF climate-grid mask and lookup
- Strategic-S provenance and assessment
- independent validation V1–V4
- data dictionary, source attribution and release metadata
- SHA-256 content manifest

### 10.2 Direct export

```bash
./RUN.sh products \
  /path/to/completed/run \
  /path/to/new/public_output \
  --climate-roster /path/to/roster.csv \
  --cf-grid /path/to/grid.nc \
  --cf-lookup /path/to/lookup.csv \
  --validation-dir /path/to/validation_archive
```

For a product equivalent to the release, the explicit grid and validation options are required. The output directory must be new.

## 11. Optional offline data viewer and publication archives

### 11.1 Build or reuse the interactive viewer

```bash
./RUN.sh viewer --config workflow.local.yaml
```

This step is available **only after `public`**. It produces `GWEH_explorer.html`
and a checksum-based `VIEWER_BUILD.json` in `viewer_<run_id>/` (or the configured
`viewer_dir`). The HTML can be opened offline and shows the candidate regions,
evidence statistics, market maturity and selection pathways, and model-grid
cell boundaries. It uses Natural Earth public-domain background geography.

The generator reads only manifest-verified public dataset inputs, detects the
actual `GWEH_grid_*.nc` and its dimensions, and has no dependency on the raw
GWPT/GWA/IRENASTAT source assets. For a standalone build from a dataset ZIP:

```bash
python viewer/build_viewer.py \
  --dataset /path/to/GWEH_v1.0.0_dataset.zip \
  --out GWEH_explorer.html \
  --record VIEWER_BUILD.json
```

No viewer code is called by Stage00–08 or by V1–V4. The authoritative scientific
outputs remain the GeoPackage, NetCDF and CSV tables. See `viewer/README.md`.

### 11.2 Create deposit archives

```bash
./RUN.sh release --config workflow.local.yaml --output /path/to/new/deposit
```

The release workflow verifies the scientific and publication inputs, creates
separate software and dataset ZIP files, and **by default** builds or reuses a
verified companion HTML viewer. `GWEH_explorer.html`, `VIEWER_BUILD.json` and
`VIEWER_README.md` are separate files alongside the dataset ZIP in the
release output directory. The dataset ZIP and its scientific manifest remain
unchanged. The viewer generator and bundled map asset are part of the software
ZIP under `viewer/`.

To omit the optional viewer from a release:

```bash
./RUN.sh release --config workflow.local.yaml \
  --output /path/to/new/deposit --no-viewer
```

The release command produces local archives and checksums.

| Product | Archive | Licence |
|---|---|---|
| Software | `GWEH_v1.0.0_software.zip` | MIT |
| Dataset | `GWEH_v1.0.0_dataset.zip` | CC BY 4.0 for GWEH-created content |

The dataset licence does not replace the original rights of upstream data providers.

## 12. Output directories and restart

```text
<data_parent>/
└── science_scenarios/
    └── <scenario_id>/
        ├── data/runs/<run_id>/{manifest,00_...,...,08_catalogue}/
        ├── grid_<run_id>/
        ├── validation_<run_id>/{spatial,market,merra_work,merra_current,archive_current}/
        ├── public_<run_id>/
        └── omission_<run_id>/
```

Locations can be overridden in `workflow.local.yaml`.

The workflow distinguishes four kinds of restart. A **scientific restart** continues from completed Stage00–08 stages and computes the missing ones. A **validation restart** reuses verified completed validation products where supported. A **MERRA restart** continues from the yearly aggregation checkpoints. A **metadata or grid update** requires a new grid output and a new CF verification. The public export and the release archive always require a new output directory.

A change in the scientific inputs requires a new scenario or run identifier. Metadata-only changes do not require a new scientific computation, but they must pass the provenance and validation checks of the public output.

## 13. Reproducibility and limitations

GWEH is reproducible from its fixed candidate definitions, numerical configurations, scientific code and the stated upstream data snapshots.

Candidate nomination uses expert-informed geographic and strategic criteria described in the manuscript and the concise nomination register. It is therefore not a fully automatic global discovery of all possible wind-energy systems.

The possible sources of uncertainty include completeness of the candidate universe, approximate project coordinates and surface classification, wind-atlas and project-data coverage, interpretation of strategic evidence, delineation of spatial boundaries, model-grid resolution and cell-centre representation, and differences between independent validation datasets.

## 14. Citation, attribution and licensing

**Creator:** Petr Pisoft
**ORCID:** https://orcid.org/0000-0002-5034-9169
**Institution:** Department of Atmospheric Physics, Faculty of Mathematics and Physics, Charles University, Prague, Czech Republic
**Contact:** petr.pisoft@matfyz.cuni.cz

**Funding:** Johannes Amos Comenius Programme (P JAC), project CZ.02.01.01/00/22_008/0004605, *Natural and anthropogenic georisks*.

The software is distributed under the MIT licence. Data products created within GWEH are distributed under CC BY 4.0. Original upstream datasets remain under the terms of their providers and are not redistributed within GWEH.

Details are in `LICENSE`, `DATA_LICENSE.md`, `DATA_RIGHTS.md`, `CITATION.cff` and in the dataset source register.

In scientific publications, cite the version DOIs of both the dataset and the software together with the original data providers.
