# Running GWEH on a research server

All run locations and upstream sources are specified once in the local
`workflow.local.yaml` (create from `example.local.yaml`). 

```
./RUN.sh config
./RUN.sh test
./RUN.sh science --config workflow.local.yaml
./RUN.sh validate --config workflow.local.yaml
./RUN.sh public --config workflow.local.yaml
./RUN.sh viewer --config workflow.local.yaml  # optional; release defaults to viewer
./RUN.sh release --config workflow.local.yaml --output /path/to/new/deposit
```

For an existing completed run configure its `scenario_id`, `run_id`, current
`grid_dir`, `validation_archive_dir` and other intermediate results. If the
current grid uses a previous filesystem naming convention, construct a new
grid from the same Stage05/08 with `./RUN.sh grid ...` into a new directory;
there is no scientific re-run. Revalidate the updated NetCDF with the actual
IOOS CF checker. Source filenames and directory locations are not part of
the scientific selection thresholds.

For large MERRA workloads the annual aggregates are checkpointed. Existing
validation can be completed by a subset such as:

```
./RUN.sh validate --config workflow.local.yaml --steps merra-compare,cf,archive
```

Omission screening is a separate, optional scientific task. To run it,
configure `omission.expert_proposals` or `omission.unowned_radius_km`, and then
execute `./RUN.sh omission --config workflow.local.yaml`. It does not modify
or gate the dataset export.

A stage only resumes with the same source files and numerical configurations.
New scientific inputs require a new scenario/run ID. Each publication export
writes to a new output directory; old outputs are never overwritten.
