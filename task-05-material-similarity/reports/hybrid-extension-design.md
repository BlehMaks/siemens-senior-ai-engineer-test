# Missing-aware structured similarity extension

## Decision and boundary

The submitted model remains the reviewed `PART_DESCRIPTION` word-and-character
TF-IDF baseline. A structured extension must be an explicitly selected `hybrid`
mode with its own component scores and coverage. It must not silently change an
`insufficient_description` text result into a confident replacement claim.

The first useful subset is intentionally narrow:

- current: `Current Rating` and `Rated Current (A)`;
- voltage, preserving AC/DC: `Maximum AC Voltage Rating`, `Maximum DC Voltage
  Rating`, `Rated Voltage(AC) (V)`, and `Rated Voltage(DC) (V)`;
- package dimensions: `Fuse Size`, `Physical Dimension`, body length/diameter,
  breadth, height, product diameter, and product length;
- categorical form: `Acting`, `Blow Characteristic`, `Fuse Material`, `Mounting`,
  and `Mounting Feature`.

These fields address the observed hard negatives without pretending that all 30
non-identity attributes have reliable semantics. Breaking capacity, temperature,
power, time, and joule-integral fields stay out until their units and engineering
comparison rules receive the same review.

## Parsing contract

Each quantitative parser returns either a typed value or an explicit reason it
could not parse the source. A typed value contains the original text, canonical
unit, numeric scalar or interval, optional qualifier such as `typical` or `maximum`,
and AC/DC mode where applicable. Missing, unsupported, and contradictory values are
different states; none is coerced to zero.

The parser must preserve and test:

- decimals and ranges;
- SI prefixes and canonical units;
- `x`, `X`, `×`, and multi-axis dimensions without losing axis order;
- qualifiers such as `(Typ)`, maximum, and minimum;
- AC, DC, and combined labels without cross-mode comparison;
- source forms such as `6.3@(CSA/UL)A`, `250VAC`, `5.2mm x 20mm`, and
  `6.9(Typ)W`;
- multiple source fields that disagree, reported as a conflict rather than resolved
  by column order.

Categorical values use reviewed aliases only. Unknown spellings remain unsupported;
fuzzy string matching would turn data cleaning uncertainty into false compatibility.

## Scoring and evidence

For each field observed on both parts, calculate a bounded component similarity.
Categorical fields use exact canonical equality. Positive scalar quantities use a
reviewed log-ratio distance, and intervals use overlap plus endpoint distance. A
mode conflict, such as AC versus DC, is reported separately and receives no positive
component evidence.

For configured component weights `w_i`, the structured score uses only comparable
fields:

```text
structured_score = sum(w_i * similarity_i) / sum(w_i)
structured_coverage = sum(comparable w_i) / sum(all configured w_i)
```

The output includes every contributing field, parsed values, component score,
conflicts, unsupported fields, and `structured_coverage`. A minimum coverage policy
must be calibrated on reviewed data; below it the structured result abstains.

When both channels are supported, a separately calibrated hybrid score may combine
the lexical and structured scores. When text is blank, an optional
`structured_only` result remains visibly distinct. No fixed production weights or
thresholds are justified by the current eight-query lexical benchmark.

## Evaluation and release gate

Implementation should remain a small module beside the lexical retriever and must
pass table-driven cases for units, ranges, qualifiers, AC/DC separation, dimension
parsing, conflicts, missingness, symmetry, bounds, deterministic ties, and JSON
serialization. The benchmark must then add independently reviewed structured hard
negatives and a holdout split.

Adopt hybrid ranking only if it improves held-out nDCG@5 and reduces the reviewed
hard-negative rate without lowering expected-status agreement or making results
order-dependent. Report catalog coverage by mode and representative parse failures.
Until those conditions are met, the structured design is a roadmap, not a claim of
improved replacement safety.
