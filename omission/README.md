# Independent omission screening

`screen.py` compares new expert-nominated points or polygons with the current
GWEH region and denominator geometries. Optionally, it groups unassigned
active project phases by a configurable proximity threshold.

Configure `omission.expert_proposals` and/or `omission.unowned_radius_km` in
`workflow.local.yaml`, then run:

```bash
./RUN.sh omission --config workflow.local.yaml
```

Outputs (`expert_omission_review.csv`, `unowned_active_proximity_groups.csv`,
`OMISSION_REVIEW.json`) are descriptive research diagnostics. They never
change Stage00–08 geometry, nomination, scores or selection. Proposals require
independent scientific review before any new candidate configuration is made.
This optional branch is not a prerequisite for data or software publication.
