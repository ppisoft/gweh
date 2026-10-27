# Independent validation of the GWEH reference regions

The deposited `validation/` directory contains actual independent spatial,
market and resource corroboration for the full reference candidate universe.
Validation does not modify regional boundaries, nominations or R/D/P/S scores.
The reporting populations are defined in the current catalogue and, where
relevant, the reference-grid roster; the counts are not hard-coded.

## V1/V2: independent wind-energy locations

`validation/spatial/V1_V2_summary.json` and
`validation/spatial/V1_V2_candidate_containment.csv` summarize location
checks for GOWIRES and Global Renewables Watch (onshore) and DeepOWT
(offshore, with three separate ground-truth subsets). Conditional
containment is the fraction of source locations within a selected candidate
denominator that fall inside the corresponding region. Geodesic area ratios provide
a geometric reference within the same denominator scopes. The input layout and scopes are recorded in
`validation/methodology/external_sources.yaml`. The published `regions` and `denominators` GeoPackage layers are independently partitioned; a final region can extend outside its same-ID denominator. The area baseline therefore uses only their geodesic intersection divided by the denominator area, and point containment tests same-ID assignments within the denominator scope.

## V3: independent market comparison

`validation/market/V3_summary.json` and the
`validation/market/V3_irena_gwpt_YYYY.csv` tables compare Global Wind Power
Tracker active capacity with IRENASTAT country-by-technology capacity in the
specified reporting years. Correlation statistics are calculated over pairs
positive in both datasets. Country, surface and vintage must be considered
when interpreting mismatches. The deposited country-by-technology table
contains only positive matched pairs and their average-tie ordinal ranks.
Absolute IRENASTAT capacity values are not redistributed. The published
ranks reproduce the Spearman coefficient exactly; the published summary
retains aggregate population counts and the median capacity ratio.

## V4: independently aggregated MERRA-2 resource ranks

Hourly U50M, V50M, PS, T2M and QV2M from MERRA-2 are aggregated over the
strict-interior region-centre mask. The 50-m wind-speed and density proxy are
computed for individual valid hourly samples, followed by area/time
aggregation using cos(latitude) weights:

- `WS50 = sqrt(U50M**2 + V50M**2)`;
- `rho = PS / (287.05 * T2M * (1 + 0.61 * QV2M))`;
- `WPD50 = 0.5 * rho * WS50**3`.

The primary comparison is a Spearman rank correlation with the independent
GWA4 regional WPD100 product. Nominal p-values ignore spatial dependence.

`validation/merra/V4_summary.json`, `V4_merra_candidate_values.csv` and
`V4_merra_yearly.csv` contain the comparison and candidate-level outcomes.
`validation/merra_provenance/blind/roster.csv` preserves the anonymous
validation identifiers, with their sealed candidate mapping in
`validation/merra_provenance/supervisor/mapping.csv`. Annual aggregation
checkpoints retain anonymous IDs in
`validation/merra_provenance/annual/year_YYYY.csv`.

The full `blind/regions.gpkg` was used as the actual V4 aggregation input and
is retained in the original working validation archive, not redistributed
as a second copy of the public polygons. Its original file SHA-256 remains
in `validation/merra_provenance/annual/aggregation_context.json` and was
checked against the working file when assembling the validation evidence.
`validation/merra_provenance/blind/geometry_fingerprints.csv` instead records
SHA-256 hashes of the 65 individual blind GeoPackage geometries as WKB. After
joining through `supervisor/mapping.csv`, every WKB hash must equal that of
the matching `GWEH_geometries.gpkg` `regions` feature; the release software
checks this correspondence before packaging. This establishes geometry
identity. 

The exact calculation parameters (including the blinded mapping seed,
time range, required variables and weighting) are archived in
`validation/methodology/merra_protocol.yaml`. The annual aggregation context
includes the source geometry hash, time period and grid shape; the local
filesystem root for the original hourly MERRA-2 files is deliberately
excluded from the public copy, while the private full aggregation context
remains available for rerunning and resuming the calculation.

## CF grid and validation status

`validation/CF11_IOOS_CHECK.txt` records the CF-1.11 IOOS compliance test.
`validation/CF_GRID_SHA256.txt` identifies the exact NetCDF bytes that were
checked. `validation/VALIDATION_SCOPE.json` summarizes the independently
recomputed validation populations and their numerical cross-checks.

See `metadata/data_dictionary.csv` for the annual checkpoints, blinded
roster, regional V4 fields and validation columns. Input data source versions
and access terms are listed in `metadata/source_register.csv`.
