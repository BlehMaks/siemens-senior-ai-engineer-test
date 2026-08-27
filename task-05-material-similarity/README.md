# Task 5: Identifying alternative materials

## Assignment baseline

Use `Fuse.csv` to return five similar alternatives for every material. The assignment asks for:

1. descriptive analysis of the attributes, at least two data difficulties, and possible remedies;
2. a similarity model based on `PART_DESCRIPTION`, with an explanation;
3. a plan for extending the model to the remaining attributes.

The deliverables are the data analysis and the code for the description-based similarity model.

## Observed data constraints

The semicolon-delimited dataset has 998 unique part IDs and 32 columns. `PART_DESCRIPTION` is missing for 335 rows; among non-empty descriptions, 81 are duplicates. Missingness is severe in several technical fields, including pre-arcing time, maximum power dissipation, product diameter, and additional features. Most nominally numeric attributes contain units, ranges, qualifiers, or free text and therefore load as strings.

These are not cosmetic cleaning issues. A description-only model has no evidence for one third of the catalog, exact duplicate descriptions can dominate nearest-neighbor results, and unit-bearing values cannot be compared safely as raw strings.

## Recommended stack and staged approach

- `pandas` for profiling and reproducible normalization.
- A word-and-character TF-IDF representation with cosine similarity as the transparent baseline. Character n-grams preserve model numbers, dimensions, current ratings, and spelling variants that generic semantic embeddings may blur.
- Brute-force cosine ranking for 998 rows. An approximate-nearest-neighbor service would add complexity without a scale benefit.
- A compact sentence-embedding model as a benchmark, not an automatic replacement. Choose it only if a documented evaluation shows better domain retrieval.
- A hybrid extension that parses units and categorical attributes, computes per-field similarities, and combines calibrated component scores with missingness-aware weights.

## Ranking rules

- Exclude the query part itself.
- Return distinct part IDs and handle identical descriptions deterministically.
- Preserve exact technical values during normalization; do not remove tokens such as `250V`, `6.3A`, or `5x20mm` as noise.
- Attach a score, evidence fields, and a confidence or coverage indicator to each recommendation.
- Separate text-only results from structured-attribute fallback results so the assignment's step-2 claim remains accurate.

## Constraints and acceptance checks

- A text-only method cannot produce evidence-based recommendations for blank descriptions. The output must abstain, mark low confidence, or invoke a clearly labeled fallback rather than fabricate similarity.
- Normalize units and qualifiers with explicit parsers before structured comparison. Values such as typical, maximum, ranges, AC/DC, and unit prefixes are not interchangeable.
- Do not use `PART_ID` as a similarity feature.
- Evaluate with a small reviewed relevance set, duplicate/near-duplicate checks, catalog constraints, coverage, and stability. With no supplied ground truth, explain the limits of every offline metric.
- Show representative successes and failures, including blank descriptions, identical descriptions, conflicting attributes, and sparse rows.
- Produce exactly five alternatives only where five defensible candidates exist; otherwise expose the coverage limitation explicitly.
- The lexical all-pairs baseline for 998 rows targets at most 60 seconds and 1 GB
  peak memory on the recorded reference machine. ANN infrastructure is out of scope
  unless a later catalog-size benchmark demonstrates a need.

## Run the lexical baseline

```bash
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" --part-id <PART_ID>
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" --output alternatives.json
```

The implementation combines word and character TF-IDF cosine scores with reviewed
`0.25/0.75` weights. The versioned eight-query relevance benchmark selected this
mixture by graded nDCG@5 from a fixed five-weight grid; the full metrics and limits
are in `reports/retrieval-evaluation.md`. Each JSON record has
`ok`, `insufficient_description`, or
`insufficient_candidates` status. Successful records contain five unique non-self
IDs, the combined and per-channel scores, and the strongest shared word and
character features by inverse document frequency. Blank text always produces an
explicit empty result.

Run the deterministic benchmark with:

```bash
uv run python -m material_similarity.evaluation \
  "input/IT DA AI Tasks/Fuse.csv" \
  task-05-material-similarity/evals/relevance.yaml
```

## Output contract

The CLI emits one object for `--part-id`, or an array containing one object per
catalog row when `--part-id` is omitted. The schema is deliberately small:

| Field | Type and invariant |
|---|---|
| `part_id` | Non-blank source identifier. |
| `status` | `ok`, `insufficient_description`, or `insufficient_candidates`. |
| `alternatives` | Exactly five entries for `ok`, none for blank text, otherwise zero to four evidence-backed entries. |
| `alternatives[].part_id` | Unique candidate identifier, never the query identifier. |
| `alternatives[].score` | Weighted cosine score in `[0, 1]`; descending, then `part_id` ascending for ties. |
| `alternatives[].word_score` | Word-channel cosine contribution in `[0, 1]`. |
| `alternatives[].character_score` | Character-channel cosine contribution in `[0, 1]`. |
| `alternatives[].shared_tokens` | Up to five highest-IDF shared word features. |
| `alternatives[].shared_character_ngrams` | Up to five highest-IDF shared character features. |

Scores describe text similarity, not electrical compatibility or approval to use a
part as a replacement. Consumers must inspect the evidence and status rather than
applying an arbitrary score cutoff.

## Deliverables and verification

- `reports/data-analysis.md` records the reproducible profile and data-quality
  remedies.
- `reports/retrieval-evaluation.md` records reviewed metrics, successes, failures,
  and limitations; `reports/relevance-metrics.json` is its reproducible machine
  output.
- `reports/hybrid-extension-design.md` defines the minimal missing-aware structured
  extension without changing the assignment's text-only result semantics.

From the repository root, the complete Task 5 gate is:

```bash
SIEMENS_FUSE_CSV="input/IT DA AI Tasks/Fuse.csv" \
  uv run pytest -q task-05-material-similarity/tests
uv run ruff format --check task-05-material-similarity
uv run ruff check task-05-material-similarity
uv run mypy task-05-material-similarity/src
```
