# GWEH Data Explorer

The offline viewer displays GWEH regions, evidence, market maturity, selection
pathways and model-grid representation. The GeoPackage, NetCDF and CSV products
remain the reference data.

## Build

After the public dataset has been created:

```bash
./RUN.sh viewer --config workflow.local.yaml
```

Alternatively, build from an extracted dataset or its ZIP archive:

```bash
python viewer/build_viewer.py --dataset /path/to/GWEH_dataset \
  --out GWEH_explorer.html --record VIEWER_BUILD.json
```

Open `GWEH_explorer.html` in a browser. It does not require a web server or
network access. The `release` command includes the viewer by default; use
`--no-viewer` to omit it.

Browser interaction tests (Playwright and Chromium required):

```bash
python viewer/test_viewer.py --html GWEH_explorer.html
```

The bundled low-resolution background is from Natural Earth (public domain):
https://www.naturalearthdata.com/ . Its source and checksum are documented in
`assets/PROVENANCE.md`.
