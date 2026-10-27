# Reproducible software environment (meop40, Linux x86_64)

This is an environment snapshot supplied by the research team. The files were exported after the accepted Stage00–08 production run.

- `conda-explicit-linux64.txt`: exact conda artifacts and builds on `linux-64` (strongest recreation record).
- `environment-exact.yml`: exact conda package versions, for human review or environment creation; local self-installed `gweh==1.0.0` was removed because this repository supplies the GWEH code.
- `environment-minimal.yml`: original environment request/history.
- `geospatial-stack.txt`: GDAL/GEOS/PROJ and numerical ecosystem versions.
- `python-version.txt`, `system.txt`: Python and OS/kernel information.

To reproduce the exact conda package artifacts on compatible Linux:

```bash
conda create --name gweh-repro --file reproducibility/meop40-linux64/conda-explicit-linux64.txt
conda activate gweh-repro
./RUN.sh config
./RUN.sh test
```

Alternatively, for exact versions with the solver permitted to select builds:

```bash
conda env create -n gweh-repro -f reproducibility/meop40-linux64/environment-exact.yml
```

The original full pip inventory was not used as a requirements file because
its build-local filesystem and SSH references are not portable. Exact conda
artifacts and tested numerical/geometry library versions are included here.

A NumPy binary compatibility warning was observed in one previous test run;
fresh environment testing and geometry serialization parity on a second Linux
installation remain recommended rather than asserting universal binary identity.

Upstream GWPT/GWA4/Natural Earth/EEZ source snapshots are separate, subject to
their original providers' licensing and redistribution terms. This software
environment does not contain or grant rights to those upstream datasets.
