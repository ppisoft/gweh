# Strategic-S evidence

Strategic-S is derived from four evidence components and policy modifiers.

- `S1` - strategic commitment (0-2)
- `S2` - implementation strength (0-2)
- `S3` - geographic coverage (0-2)
- `S4` - recent implementation evidence (0-1)

`S_reference` is the production mapping and `S_strict` is the mapping sensitivity.
Negative-policy caps and viable-alternative rules are defined in `strategic_score_rules.yaml`.

Files:
- `strategic_assessment.csv` - candidate-level S1-S4 values and policy flags.
- `strategic_score_rules.yaml` - deterministic mapping to S.
- `source_database.csv` - source ID, verified title, publication date if known, and exact URL.
- `candidate_source_evidence.csv` - candidate ID, source ID, assessed parameter and its value. Region names, narrative notes and review dates are not duplicated in this table.

The exact URL strings are part of the evidence record. Source-review workflow metadata are not part of the production database.
