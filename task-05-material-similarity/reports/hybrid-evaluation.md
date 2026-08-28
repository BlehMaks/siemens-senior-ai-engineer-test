# Hybrid structured-subset evaluation

## Decision

The structured prototype is complete but **not promoted**. `material-similarity`
continues to use the reviewed word/character TF-IDF baseline unless a caller selects
`--mode hybrid` explicitly.

The comparison used the exact 998-row `Fuse.csv` bound to
`evals/relevance.yaml` by catalog SHA-256. It ran the existing eight reviewed queries
with the same top-five and status validation used by the lexical benchmark. No
unreviewed or invented labels were added.

## Measured comparison

| Measure | Text default | Hybrid prototype | Decision |
|---|---:|---:|---|
| Precision@5 | 0.542857 | 0.0 | regressed because no query retained five candidates |
| nDCG@5 | 0.846792 | 0.0 | did not improve |
| Coverage | 0.875 | 0.0 | regressed |
| Expected-status agreement | 1.0 | 0.125 | regressed |
| Returned hard-negative rate | 0.142857 | 0.0 | improved, but with lost coverage |
| Input-order stability | — | 1.0 | passed |

The hard-negative rate counts reviewed grade-zero candidates among returned
candidates. It must be read together with coverage: the prototype removed hard
conflicts but does not backfill beyond the lexical top five, so every otherwise
ranked query returned fewer than five candidates. Reporting zero hard negatives
alone would therefore be misleading.

The deterministic promotion gate requires strictly better nDCG@5, a lower
hard-negative rate, no coverage or expected-status regression, and complete
input-order stability. The measured result failed the relevance, coverage, and
status conditions.

## Implemented boundary

The parser covers only the high-value fields selected in the reviewed design:

- current in A/mA/uA;
- AC/DC voltage in V/mV/kV;
- two- or three-axis dimensions in mm/cm/m;
- exact, typical, minimum, and maximum qualifiers;
- reviewed aliases for acting characteristic, material, mounting, and mounting
  feature/boolean values.

Unknown units and aliases remain unsupported. Conflicting source columns are not
resolved by precedence. Comparable fields expose normalized values, weight,
similarity, coverage, penalties, and hard/soft conflicts. Blank text still abstains;
there is no structured-only replacement claim.

## Reproduction

From the repository root:

```bash
uv run python -m material_similarity.evaluation \
  "input/IT DA AI Tasks/Fuse.csv" \
  task-05-material-similarity/evals/relevance.yaml \
  --mode hybrid
```

The synthetic hard-negative regression in `tests/test_hybrid.py` exercises the same
non-promotion rule without the private catalog. Full-catalog and reviewed-label tests
run when `SIEMENS_FUSE_CSV` points at the exact source file.

## Limitations and next evidence

The reviewed set is small and was originally pooled for the lexical weight grid. A
future promotion attempt needs independently reviewed structured hard negatives and
a held-out set. Candidate generation should then be evaluated with a larger lexical
pool before filtering so safe backfilling can be measured rather than assumed. No
universal unit engine, fuzzy ontology, breaking-capacity rules, or electrical
interchangeability claim is included.
