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
