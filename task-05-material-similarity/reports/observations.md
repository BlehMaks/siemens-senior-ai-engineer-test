# Task 5 observations, results, and reasoning

The catalog decides most of this design, so the data problems come first and the
model follows from them. Full evidence is in
[`data-analysis.md`](data-analysis.md),
[`retrieval-evaluation.md`](retrieval-evaluation.md), and
[`hybrid-extension-design.md`](hybrid-extension-design.md).

## Difficulties in the data

The task asks for at least two. Three matter enough to change the design.

A third of the catalog has no description at all. `PART_DESCRIPTION` is blank for 335
of 998 rows, or 33.57%, and a description-based model has nothing to work with there.
The remedy is to abstain and say so: those rows return a labelled low-confidence
fallback instead of five recommendations dressed up as evidence.

Descriptions also repeat. Among 663 non-empty descriptions only 582 are distinct, so
81 repetitions spread across 45 duplicate-text groups cover 126 rows. Identical text
makes two different parts indistinguishable to any text model. Retrieval therefore
excludes the query part, returns distinct IDs, breaks ties deterministically, and
surfaces identical-text matches as such rather than implying the model resolved them.

The technical attributes are not numbers. They are strings carrying units, ranges,
qualifiers, and modes: `250VAC`, `5.2mm x 20mm`, `6.9(Typ)W`, `6.3@(CSA/UL)A`, and
temperatures suffixed `Cel`. Eight fields are also at least half blank, from
`Rated Voltage(DC) (V)` at 54.21% to `Pre-arcing time-Min (ms)` at 89.18%. Comparing
these as raw strings would equate values that are not equivalent, and stripping the
units would be worse. Any structured comparison has to parse magnitude, canonical
unit, qualifier, and mode first, then score only fields present in both materials
while reporting coverage.

## The model, and why this one

A word-and-character TF-IDF representation with cosine similarity.

The character channel is the point. Fuse descriptions carry their meaning in compact
tokens such as `250V`, `6.3A`, and `5x20mm`, which word tokenisation splits or drops
and which general-purpose sentence embeddings tend to blur, since two fuses differing
only in current rating read as near-identical prose. Character n-grams keep those
distinctions while the word channel still captures family phrases.

With 998 rows, brute-force cosine ranking is exact and instant, so an approximate
nearest-neighbour index would add moving parts for no benefit. The model is also
inspectable: a recommendation traces back to the shared substrings that produced it,
which matters more than a small metric gain when a person has to accept or reject the
alternative.

## Results

Only the word/character mixture was varied, on a fixed grid:

| Word | Character | Macro precision@5 | Macro nDCG@5 |
|---:|---:|---:|---:|
| 0.00 | 1.00 | 0.5429 | 0.8024 |
| **0.25** | **0.75** | **0.5429** | **0.8468** |
| 0.50 | 0.50 | 0.5429 | 0.8333 |
| 1.00 | 0.00 | 0.4857 | 0.6724 |

The selected 0.25/0.75 mixture confirms the reasoning above. Character evidence
carries most of the signal, and the pure-word setting is the only one that loses
precision. Reversing all 998 input rows leaves every benchmark result identical, so
the ranking does not depend on input order.

Across the catalog, 663 of 998 rows (66.43%) receive text-backed alternatives and the
remaining 335 receive an explicit abstention.

## Caveats

There is no supplied ground truth. The benchmark is a small reviewed relevance set,
so `precision@5 = 0.5429` measures agreement with that set rather than catalog-wide
accuracy, and it should not be read as a production quality figure.

Lexical similarity is also not interchangeability. Two fuses can share nearly all
their description text and still be unsuitable substitutes for one another. Turning
similarity into a compatibility claim needs the structured attributes, which is what
the extension in [`hybrid-extension-design.md`](hybrid-extension-design.md) is for.
