# Source data, attribution and redistribution

This dataset contains derived GWEH regional geometries, scores, tables, a CF NetCDF reference-grid classification and independent validation results. The source inventory and attribution metadata are in `metadata/source_register.csv` and `metadata/strategic_s/`. The scientific workflow and the original numerical input configuration are archived separately as GWEH software 1.0.0, https://doi.org/10.5281/zenodo.23555661, with the source code at https://github.com/ppisoft/gweh.

The GWEH author holds copyright only to the extent of the derived scientific work. The CC BY 4.0 licence applies to the original geometries, metadata and analysis products created within GWEH. Source-specific restrictions and required attributions remain in force; the deposit licence does not re-license upstream datasets and does not supersede their copyright or database rights.

Construction used Global Wind Power Tracker, Global Wind Atlas 4, Natural Earth and Marine Regions EEZ data. Independent validation uses GOWIRES, Global Renewables Watch, DeepOWT, IRENASTAT and MERRA-2. Source roles, snapshots, primary landing pages, declared licences and the scope of the derived product are listed in `metadata/source_register.csv`.

Raw third-party turbine and project inventories, source rasters, provider workbooks and hourly MERRA-2 files are not distributed in this dataset. The published products have the following scope:

- V1/V2 contain per-candidate point counts without coordinates.
- V3 contains GWPT capacities and the ordinal ranks of both sources; absolute IRENASTAT capacities are not published.
- V4 contains regional aggregates without reanalysis fields.
- Project statistics are aggregated to the region and component level.

Reuse requires appropriate attribution of both GWEH and the original providers. The providers remain authoritative for the reuse conditions of their original content.
