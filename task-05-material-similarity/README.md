# Task 5: Identifying alternative materials

## Assignment baseline

Use `Fuse.csv` to return five similar alternatives for every material. The assignment asks for:

1. descriptive analysis of the attributes, at least two data difficulties, and possible remedies;
2. a similarity model based on `PART_DESCRIPTION`, with an explanation;
3. a plan for extending the model to the remaining attributes.

The deliverables are the data analysis and the code for the description-based similarity model.

The short account of the observations, the results, and the reasoning behind them
is [`reports/observations.md`](reports/observations.md).

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

- A text-only method cannot produce evidence-based recommendations for blank descriptions. The complete output therefore invokes a clearly labeled, low-confidence fallback; `--mode text` preserves the reviewed text-only abstention contract.
- Normalize units and qualifiers with explicit parsers before structured comparison. Values such as typical, maximum, ranges, AC/DC, and unit prefixes are not interchangeable.
- Do not use `PART_ID` as a similarity feature.
- Evaluate with a small reviewed relevance set, duplicate/near-duplicate checks, catalog constraints, coverage, and stability. With no supplied ground truth, explain the limits of every offline metric.
- Show representative successes and failures, including blank descriptions, identical descriptions, conflicting attributes, and sparse rows.
- Produce exactly five alternatives only where five defensible candidates exist; otherwise expose the coverage limitation explicitly.
- The lexical all-pairs baseline for 998 rows targets at most 60 seconds and 1 GB
  peak memory on the recorded reference machine. ANN infrastructure is out of scope
  unless a later catalog-size benchmark demonstrates a need.

## Run the complete output and lexical baseline

```bash
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" --part-id <PART_ID>
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" --output alternatives.json
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" \
  --mode text --part-id <PART_ID>
```

The `complete` default combines word and character TF-IDF cosine scores with reviewed
`0.25/0.75` weights. The versioned eight-query relevance benchmark selected this
mixture by graded nDCG@5 from a fixed five-weight grid; the full metrics and limits
are in `reports/retrieval-evaluation.md`. Rows with usable descriptions retain that
model unchanged. A row that cannot produce five text candidates is filled from a
deterministic non-ID fallback over the other 30 source attributes. Exact normalized
field equality carries 90% of the fallback score; agreement in the observed/missing
pattern carries 10%.

This staged output returns five unique non-self IDs for all 998 supplied rows while
keeping the method and confidence visible on every alternative. The six source rows
with no description and no populated attributes are matched only to the other
equally empty rows, labeled `missingness_only`, and must not be treated as
engineering-approved substitutes. `--mode text` continues to emit
`insufficient_description` for blank text, and `--mode hybrid` continues to expose
the separate non-promoted structured prototype.

Run the deterministic benchmark with:

```bash
uv run python -m material_similarity.evaluation \
  "input/IT DA AI Tasks/Fuse.csv" \
  task-05-material-similarity/evals/relevance.yaml
```

## Opt-in hybrid prototype

Structured reranking is available only when requested; it does not replace either
the complete deliverable output or the reviewed lexical benchmark:

```bash
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" \
  --mode hybrid --part-id <PART_ID>
uv run python -m material_similarity.evaluation \
  "input/IT DA AI Tasks/Fuse.csv" \
  task-05-material-similarity/evals/relevance.yaml --mode hybrid
```

The prototype parses only current, AC/DC voltage, two- or three-axis dimensions,
acting characteristic, material, mounting, and mounting feature. Its alternatives
label `mode` as `hybrid` or `text_only` and expose the original text evidence,
structured score and coverage, component values, penalties, unsupported fields, and
conflicts. Hard conflicts are returned under `excluded` rather than hidden. An
abbreviated synthetic result looks like:

```json
{
  "part_id": "Q",
  "status": "insufficient_candidates",
  "alternatives": [{
    "part_id": "A",
    "mode": "hybrid",
    "structured_score": 1.0,
    "structured_coverage": 0.83871,
    "final_score": 1.0,
    "components": [{"field": "current", "similarity": 1.0, "weight": 3.0}]
  }],
  "excluded": [{
    "part_id": "B",
    "conflicts": [{"field": "acting", "code": "categorical_mismatch", "hard": true}]
  }]
}
```

The exact eight-query reviewed comparison did **not** promote hybrid mode. It removed
all reviewed returned hard negatives (`0.142857` to `0.0`) but left fewer than five
candidates for every text-backed query, reducing coverage from `0.875` to `0.0` and
expected-status agreement from `1.0` to `0.125`. Text nDCG@5 remains `0.846792`;
hybrid nDCG@5 is `0.0` under the honest insufficient-candidate contract. Full
evidence and limitations are in `reports/hybrid-evaluation.md`.

## Business extension

The version-2 business mode is opt-in and leaves the assignment baseline and the
existing `complete`, `text`, and `hybrid` modes unchanged. It applies the reviewed
policy in `evals/compatibility-policy.yaml` before ranking. A hard current,
AC/DC, dimension, acting, material, mounting, or mounting-feature conflict cannot
be offset by a text score. Missing evidence and unsupported parser input remain
separate, inspectable states.

```bash
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" \
  --mode extension \
  --policy task-05-material-similarity/evals/compatibility-policy.yaml \
  --part-id <PART_ID>
```

After reviewing the strict result, callers can explicitly run the separate Tier 2
tolerance rung:

```bash
uv run material-similarity "input/IT DA AI Tasks/Fuse.csv" \
  --mode extension-relaxed \
  --policy task-05-material-similarity/evals/compatibility-policy.yaml \
  --part-id <PART_ID>
```

This mode reconsiders only strict top-five exclusions caused by
`current:numeric_hard_conflict` or `dimensions:dimension_mismatch`. It never expands
the lexical candidate pool and never relaxes AC/DC, categorical, dimension-axis,
contradictory-source, or unsupported evidence. Strict candidates remain in
`alternatives`; they always precede the separately serialized
`relaxed_alternatives`, regardless of score. Every relaxed entry contains its full
candidate evidence, ordered `relaxed_rules`, and the exact
`tolerance_only_relaxation_requires_engineering_review` reason. Any result containing
a relaxed candidate has status `review_required`, even when the two candidate lists
contain five entries in total.

Rows with text use `strict_hybrid`. Blank descriptions never enter a TF-IDF query;
they use the separately labeled `structured_only` mode only when reviewed typed
fields meet the minimum evidence coverage. Version-2 status is `ok` only for five
defensible candidates. One to four candidates produce `review_required`, and no
defensible candidate or insufficient query evidence produces
`insufficient_evidence`. Every result still supports engineering review rather than
automatic approval.

The extension emits one version-2 object for `--part-id`, or an array of those
objects when the option is omitted:

| Field | Type and invariant |
|---|---|
| `schema_version` | Always `"2.0"`; the default command remains on the unversioned assignment schema. |
| `part_id` | Non-blank source identifier. |
| `mode` | `strict_hybrid` for text-backed queries or `structured_only` for blank descriptions. |
| `status` | `ok`, `review_required`, or `insufficient_evidence`; only `ok` has exactly five alternatives. |
| `alternatives` | Zero to five scored candidates with text evidence, structured components, coverage, penalties, unsupported fields, and conflicts. |
| `excluded` | Candidates rejected by hard gates, with their exact conflict codes and evidence. |
| `evidence_coverage` | Query-side reviewed structured-weight coverage in `[0, 1]`. |
| `reason` | `null` for `ok`; otherwise `fewer_than_five_defensible_candidates`, `no_defensible_candidates`, or `query_structured_coverage_below_minimum`. |

The policy file is JSON-compatible YAML with schema `1.0`, a numeric
`minimum_structured_coverage` in `[0, 1]`, `required_candidates` fixed at five, and
the eight reviewed rules in documented order. Each rule supplies `name`, positive
`weight`, optional `hard_ratio`, boolean `hard_category` and `never_relax` flags,
and unique `supported_values`. Invalid policy shapes, unsupported parser values,
unknown part IDs, and missing comparison outputs fail closed instead of silently
falling back to the assignment baseline.

The tolerance command uses a separate `2.1` result schema so strict `2.0` consumers
remain unchanged. It adds `relaxed_alternatives` to the fields above, sets `mode` to
`relaxed_hybrid` for text-backed rows, and keeps `structured_only` rows unrelaxed.
Each relaxed entry has exactly `candidate`, `relaxed_rules`, and `review_reason`.
When no tolerance rule applies, status and reason retain the strict result; the
command cannot manufacture candidates outside the original lexical top five.

The safety benchmark contains 20 reviewed, sanitized cases split into training and
held-out groups. It covers every policy rule plus blank and duplicate descriptions,
sparse rows, parser failures, strict candidates, hard conflicts, and mandatory
abstention. Run the same-catalog comparison report with:

```bash
uv run python -m material_similarity.evaluation \
  "input/IT DA AI Tasks/Fuse.csv" \
  task-05-material-similarity/evals/relevance.yaml \
  --mode comparison \
  --safety-benchmark task-05-material-similarity/evals/safety.yaml \
  --output task-05-material-similarity/reports/baseline-vs-extension.json \
  --markdown-output task-05-material-similarity/reports/baseline-vs-extension.md
```

Both report files are rendered from one versioned object. The committed example is
a sanitized deterministic fixture; private-catalog output must be generated locally
with the owner-authorized catalog and reviewed before use. The version-1.1 report
evaluates relaxed hybrid separately from strict hybrid. Structured-only precision@5
and nDCG@5 remain unset
until blank-description rows have reviewed relevance labels; compatibility checks
must not be mislabeled as interchangeability ground truth.

## Output contract

The CLI emits one object for `--part-id`, or an array containing one object per
catalog row when `--part-id` is omitted. The schema is deliberately small:

| Field | Type and invariant |
|---|---|
| `part_id` | Non-blank source identifier. |
| `status` | `ok` when five labeled entries exist; `insufficient_candidates` remains possible for catalogs with fewer than six rows. Text-only mode can also emit `insufficient_description`. |
| `alternatives` | Exactly five entries for every row in the supplied 998-row catalog. |
| `alternatives[].part_id` | Unique candidate identifier, never the query identifier. |
| `alternatives[].method` | `description` or `structured_fallback`; the fallback never uses `PART_ID` as a feature. |
| `alternatives[].confidence` | `description_supported`, `field_supported`, or the explicit limitation `missingness_only`. |
| `alternatives[].score` | Method-local score in `[0, 1]`; descending within its method, then `part_id` ascending for ties. |
| `alternatives[].word_score` | Word-channel cosine contribution in `[0, 1]`. |
| `alternatives[].character_score` | Character-channel cosine contribution in `[0, 1]`. |
| `alternatives[].shared_tokens` | Up to five highest-IDF shared word features. |
| `alternatives[].shared_character_ngrams` | Up to five highest-IDF shared character features. |
| `alternatives[].shared_fields` | Up to five exact normalized field matches, or explicit `missing:<field>` evidence for a missingness-only fallback. |

Scores describe text similarity, not electrical compatibility or approval to use a
part as a replacement. Consumers must inspect the evidence and status rather than
applying an arbitrary score cutoff.

The transparent TF-IDF matrices are rebuilt once per CLI invocation from the input
catalog; Task 5 does not retrain per material and the assignment does not require a
persisted index. A long-lived service can build the same index at startup and reuse
it until the catalog version changes.

## Deliverables and verification

- `reports/data-analysis.md` records the reproducible profile and data-quality
  remedies.
- `reports/retrieval-evaluation.md` records reviewed metrics, successes, failures,
  and limitations; `reports/relevance-metrics.json` is its reproducible machine
  output.
- `reports/hybrid-extension-design.md` defines the minimal missing-aware structured
  extension without changing the assignment's text-only result semantics.
- `evals/compatibility-policy.yaml` and `evals/safety.yaml` contain the versioned
  reviewed Tier 1 policy and bounded safety cases.
- `reports/baseline-vs-extension.json` and `.md` are machine-readable and human
  views generated from the same sanitized fixture result object.

From the repository root, the complete Task 5 gate is:

```bash
SIEMENS_FUSE_CSV="input/IT DA AI Tasks/Fuse.csv" \
  uv run pytest -q task-05-material-similarity/tests
uv run ruff format --check task-05-material-similarity
uv run ruff check task-05-material-similarity
uv run mypy task-05-material-similarity/src
```
