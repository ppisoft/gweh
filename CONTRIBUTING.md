# Contributing to GWEH

Repository: https://github.com/ppisoft/gweh

GWEH 1.0.0 is a frozen scientific release bound to a published dataset. The fingerprint of `src/` is recorded in the run manifest of the accepted run and in the global attributes of the published NetCDF. This determines what can be changed without a new scientific computation and what cannot.

## What may change without a new run

Documentation, comments outside `src/`, translations, link fixes and data-dictionary additions do not change the scientific fingerprint. The same applies to `publication/`, `workflow/`, `tests/` and `validation/` to the extent that the numerical output is unchanged.

## What requires a new run and a new version

Any change in `src/gweh/`, `config/` or `evidence/strategic_s/` changes `source_code_sha256` or the input checksums. The pipeline detects this when a run is resumed and requires a new run identifier. Such a change means:

1. a new `run_id` and a complete Stage00–08 run,
2. a new independent V1–V4 and CF validation,
3. a new public export and new archives,
4. new version DOIs for both the software and the dataset.

This applies to a single docstring in `src/` as well. If you propose a purely stylistic change to a production module, state this in the issue; it will be merged into the next version together with a new verification run.

## Reporting problems

For a bug in the code or data, open an issue and state the version (`1.0.0`), the file concerned, the `RUN.sh` command and its output. For a scientific finding, include the `candidate_id` and the affected table or layer.

Document an error in published values against the public dataset rather than a local run. The public dataset contains `MANIFEST_SHA256.csv`, which confirms that the same content is being compared.

## Proposing a new candidate region

The candidate universe is not extended by editing `config/candidates.csv` directly. Use the optional omission screening, which compares the proposal with the existing regions and denominators:

```yaml
omission:
  expert_proposals: /path/to/proposals.csv
```

```bash
./RUN.sh omission --config workflow.local.yaml
```

Attach `expert_omission_review.csv` to the issue together with a justification supported by citable sources. The screening is descriptive; by itself it neither nominates a candidate nor changes the selection. Accepting a proposal is a scientific decision and takes effect in the next dataset version.

## Development environment and tests

```bash
conda env create -f environment.yml
conda activate gweh

./RUN.sh config
./RUN.sh test
```

The tests cover the input contract, surface ontology, project ownership, geometry and topology, scoring and selection, grid classification and publication packaging. A pull request is expected to leave `./RUN.sh config` and `./RUN.sh test` passing with an unchanged number of failures.

Write new tests against publicly available components. Tests bound to a specific number of candidates or regions do not belong in the repository, because they would break with any future extension of the universe.

## Style

Python 3, PEP 8, no external formatting tools in CI. Code in `src/` uses explicit exceptions instead of silent data repair; write new validation the same way.

A local `workflow.local.yaml`, credentials and raw upstream data do not belong in the repository; `.gitignore` covers them.

## Contribution licence

By submitting a pull request you agree to release code changes under the MIT licence and documentation and data-product changes under CC BY 4.0, to the extent of the rights you hold in them.

## Contact

Petr Pisoft, petr.pisoft@matfyz.cuni.cz
Department of Atmospheric Physics, Faculty of Mathematics and Physics, Charles University, Prague, Czech Republic
