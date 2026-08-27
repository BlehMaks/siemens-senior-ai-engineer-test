# Fuse catalog data analysis

## Reproducible source facts

The supplied semicolon-delimited `Fuse.csv` has SHA-256
`40e91ed9d802011cbac078ac18045f17870ada35b776201ce57494caff2de62a`.
`load_materials` keeps blanks as empty strings, validates the exact 32-column schema,
and rejects blank or duplicate identifiers. `profile_materials` observes:

- 998 rows, 998 non-blank unique `PART_ID` values, and 32 columns;
- 335 blank `PART_DESCRIPTION` values (33.57%);
- 663 non-empty descriptions but only 582 distinct descriptions: 81 repetitions
  beyond the first item, spread across 45 duplicate-text groups (126 rows total);
- low-cardinality categorical evidence, including 5 non-empty `Acting` values,
  8 `Fuse Material` values, 5 `Mounting` values, and 3 `LC Risk` values;
- unit-bearing source values such as `6.3A`, `250VAC`, `5.2mm x 20mm`, `3ms`,
  `6.9(Typ)W`, and temperature values suffixed by `Cel`.

`PART_ID` is identity only. It is validated and retained for joins and deterministic
tie-breaking, but it must never enter a similarity feature matrix.

## Material data difficulties and remedies

Eight fields are at least 50% blank: `Additional Feature` (67.54%),
`Body Breadth (mm)` (58.22%), `Maximum DC Voltage Rating` (55.11%),
`Maximum Power Dissipation` (74.65%), `Pre-arcing time-Min (ms)` (89.18%),
`Product Diameter` (72.75%), `Rated Voltage (V)` (56.01%), and
`Rated Voltage(DC) (V)` (54.21%). Missingness-aware scoring must compare only
fields observed for both materials and report coverage; blank text cannot be
treated as evidence or silently imputed into confident recommendations.

Duplicate descriptions make text evidence indistinguishable for different IDs.
Retrieval must keep distinct IDs, exclude the query ID, use deterministic tie
breaking, and expose identical-text matches rather than pretending that the model
resolved them semantically.

Technical attributes are strings, not clean numbers: they include units, ranges,
qualifiers such as `(Typ)`, dimensions, and AC/DC modes. A later structured model
must parse magnitude, canonical unit, qualifier, and mode before comparison. Raw
lexicographic comparison or stripping units would create unsafe equivalences.

## D51 normalization boundary

`normalize_description` case-folds and collapses whitespace, canonicalizes safe
spellings of common units, and standardizes multiplication signs in numeric
dimensions. It deliberately retains model punctuation, decimal ratings, AC/DC,
dimensions, and qualifiers. It performs no stemming, stop-word removal, semantic
rewriting, or structured-value inference. The next delivery slice can therefore
build lexical features from normalized descriptions without losing catalog evidence.
