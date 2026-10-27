# GWEH - Global Wind Energy Hotspots – data and validation guide

GWEH is a snapshot of evidence-based reference regions for wind-energy research. The currently released candidate universe consists of 65 nominated areas; their evidential and climatic eligibility is documented in `GWEH_catalogue.csv`.  

| | |
|---|---|
| Version | 1.0.0 |
| Dataset DOI | [10.5281/zenodo.23555696](https://doi.org/10.5281/zenodo.23555696) |
| Software DOI | [10.5281/zenodo.23555661](https://doi.org/10.5281/zenodo.23555661) |
| Code repository | https://github.com/ppisoft/gweh |
| Licence | CC BY 4.0 for GWEH-created content |

## Data products

| File | Use |
|---|---|
| `GWEH_geometries.gpkg` | EPSG:4326, layers `regions` (mutually exclusive polygons) and `denominators` (reference evidence domains) |
| `GWEH_catalogue.csv` | Every candidate, evidence selection, selection pathway, market maturity and climate flags |
| `GWEH_scores.csv` | R/D/P/S evidence scores and the combined H |
| `GWEH_region_metrics.csv` | WPD100, surface fractions, projects, capacities and coverage |
| `GWEH_market_components.csv` | Onshore and offshore market components, including mixed regions |
| `GWEH_grid_*.nc` | CF-1.11 numerical mask for a named model-grid template |
| `GWEH_grid_region_lookup.csv` | Mapping of grid IDs to stable `candidate_id` and cell counts |
| `metadata/` | Source register, Strategic-S evidence, nomination basis, GWPT source overrides and data dictionary |
| `validation/` | Independent V1–V4 results, compact V4 blind-geometry fingerprints and the CF checker report |
| `MANIFEST_SHA256.csv` | Content integrity for all files in the product |
| `CITATION.cff` | Machine-readable citation metadata for the dataset DOI |

The vector geometry is the primary reference. The public GeoPackage uses the `MULTIPOLYGON` schema for both layers, including single-part regions. The native Stage05 GeoParquet may contain `Polygon` as well as `MultiPolygon`; publication changes only the geometry type wrapper, not the coordinates, rings or region boundaries. The exporter verifies exact vertex and attribute preservation after serialization. Regions and their same-ID evidence denominators are made mutually exclusive separately; a region therefore need not be wholly contained in its own final denominator. The V1/V2 area reference uses the geodesic intersection of each region with its corresponding denominator, divided by denominator area. 

The CF mask applies strict cell-centre inclusion. Selected candidates represented by fewer than two centres remain valid native-vector candidates but are absent from the grid classification. The numeric grid IDs are local to its lookup; use `candidate_id` to join results.

## Evidence and uncertainty

Resource evidence R reflects wind-resource metrics and resource-area support; D is operating deployment, P is committed pipeline and S is externally documented strategic evidence. H is their sum, but the final selection uses explicit resource and market pathways, not H alone. Relative percentiles are conditional on the candidate universe and are recomputed after any change to it.

Expert nomination provides the candidate universe but does not determine polygon boundaries. The concise nomination register and the Strategic-S sources and assessments are available in `metadata/`. Three source-cell corrections to the frozen GWPT workbook are listed in `metadata/gwpt_source_overrides.csv`. Their effect is incorporated in the published results and the scientific manuscript addresses threshold sensitivity and nomination uncertainty.

Sources of uncertainty include project location and surface assignment, the source snapshot, spatial partition boundaries, strategic documentation, wind-atlas assumptions and model or reanalysis grid resolution.

V1/V2 test point containment against independent wind-energy location datasets. V3 compares GWPT and IRENASTAT capacity by country × technology at the level of ranks. V4 compares the hourly-derived regional MERRA-2 WPD50 proxy with GWA4 WPD100 at the level of ranks.

V4 used a separate, anonymized copy of all region geometries as its aggregation input. The full blind GeoPackage remains in the working validation archive, rather than being duplicated in this dataset. 

Spatially concentrated wind corridors may be poorly represented by coarse reanalysis. Different spatial scales, measurement heights and surface representations complicate the interpretation of individual regions.

The history of candidate nomination and later omission reviews is described in the accompanying scientific manuscript. The software includes an optional independent omission screening; this is not a required or precomputed dataset layer.

## Provenance and rights

The software snapshot contains the exact numerical configurations of this reference construction; the data product contains its scientific identity and the complete actual V1–V4 validation evidence. Raw upstream datasets are not redistributed.

Source versions and their terms are in `metadata/source_register.csv`, redistribution caveats in `DATA_RIGHTS.md` and the licence of GWEH-created content in `DATA_LICENSE.md`.

The public V3 tables contain GWPT capacities and the ordinal ranks of both sources; absolute IRENASTAT capacities are not published. The Spearman rank correlations are fully reproducible from the published rank columns.

**Creator:** Petr Pisoft, ORCID https://orcid.org/0000-0002-5034-9169
Department of Atmospheric Physics, Faculty of Mathematics and Physics, Charles University, Prague, Czech Republic
Contact: petr.pisoft@matfyz.cuni.cz
Funded by the P JAC programme, project CZ.02.01.01/00/22_008/0004605.

The release-level metadata of the dataset are in `metadata/publication.yaml`; the blinded yearly checkpoint columns of V4 are covered by `metadata/data_dictionary.csv` and the exact protocol is in `validation/methodology/merra_protocol.yaml`.

In scientific publications, cite the version DOIs of both the dataset and the software together with the original data providers.
