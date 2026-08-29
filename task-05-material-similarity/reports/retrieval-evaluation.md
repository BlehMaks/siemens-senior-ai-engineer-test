# Reviewed lexical-retrieval evaluation

## Scope and provenance

The benchmark uses the supplied 998-row `Fuse.csv` with SHA-256
`40e91ed9d802011cbac078ac18045f17870ada35b776201ce57494caff2de62a`.
`evals/relevance.yaml` is JSON-compatible YAML so the evaluator needs no additional
parser dependency. It contains eight deliberately diverse queries and 60 candidate
judgments pooled from the top five results of every tested channel weight.

The grades mean:

- 3: exact described match or the same reviewed key attributes;
- 2: close family/form variant that still requires engineering approval;
- 1: broad lexical match with a known conflict or missing evidence;
- 0: incompatible or unsupported.

No grade certifies a drop-in replacement. Precision treats grades 2 and 3 as relevant;
nDCG uses all four grades and therefore distinguishes a close variant from a hard
negative. The evaluator rejects self-labels, duplicate labels, unknown IDs, catalog
hash drift, non-deterministic result IDs, and any top-five prediction that was not
reviewed.

## Weight comparison

The fixed grid changes only the word/character mixture. It does not tune TF-IDF
vocabulary, query membership, or a score threshold.

| Word | Character | Macro precision@5 | Macro nDCG@5 | Benchmark coverage |
|---:|---:|---:|---:|---:|
| 0.00 | 1.00 | 0.542857 | 0.802445 | 0.875 |
| **0.25** | **0.75** | **0.542857** | **0.846792** | **0.875** |
| 0.50 | 0.50 | 0.542857 | 0.833304 | 0.875 |
| 0.75 | 0.25 | 0.542857 | 0.761708 | 0.875 |
| 1.00 | 0.00 | 0.485714 | 0.672381 | 0.875 |

The deterministic selection rule
maximizes expected-status agreement, then coverage, then nDCG@5, then precision@5,
and finally proximity to the neutral 0.5 prior. It selects 0.25 word / 0.75
character. Character n-grams help distinguish compact ratings and dimensions while
the word channel still keeps family phrases. Reversing all 998 input rows leaves
every selected benchmark result identical (`stability = 1.0`). All eight expected
statuses match.

Benchmark coverage is 7/8 because the blank-description query correctly abstains.
It is not catalog coverage: the complete catalog has text-backed results for
663/998 rows (66.43%) and explicit abstentions for the other 335.

## Failure slices

At the selected weight, exact/duplicate queries reach nDCG@5 1.0 and precision@5
0.9; near-match queries reach 0.801204 and 0.75. The five channel-disagreement and
hard-negative queries remain harder (nDCG@5 0.785508; precision@5 0.4 and 0.56,
respectively).

- `A42` and `A823` show the intended success: identical descriptions rank
  deterministically and same-family variants remain inspectable.
- `A244` shows why both channels are needed: exact `4A` overlap alone promotes
  slow-blow surface-mount hard negatives, while the selected mixture recovers the
  fast-blow Melf family.
- `A688` remains a representative failure: character overlap promotes 125V/2410
  records that share `0.375A` over some 32V/0402 current variants. Text scoring
  cannot enforce voltage or dimensional compatibility.
- `A441` has only a generic surface-mount fuse description. Its nDCG is 1.0 because
  the weak grade-1 candidates are ordered as reviewed, but precision@5 is 0.0 because
  none has enough evidence to be a close alternative.
- `A8` correctly returns `insufficient_description`; existing structured fields are
  not silently used by the text-only model.

## Reproduction and limitations

```bash
SIEMENS_FUSE_CSV="input/IT DA AI Tasks/Fuse.csv" \
  uv run pytest -q task-05-material-similarity/tests/test_evaluation.py

uv run python -m material_similarity.evaluation \
  "input/IT DA AI Tasks/Fuse.csv" \
  task-05-material-similarity/evals/relevance.yaml \
  --output task-05-material-similarity/reports/relevance-metrics.json
```

The machine-readable report records every aggregate and slice metric. The benchmark
is a targeted, single-reviewer proxy selected to stress known catalog problems; it
is neither representative sampling nor official Siemens ground truth. The same
small set selects and reports the weight, so its score is not a held-out
generalization estimate. Before production use, a fuse-domain engineer should
independently review the labels and a separate holdout, and structured voltage,
current, action, and dimension constraints must precede replacement approval.
