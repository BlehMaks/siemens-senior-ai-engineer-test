# Task 6 observations, results, and reasoning

The function itself is small. This note answers the three explanations the task asks
for and records the one judgement call in the implementation. Usage and the full
option surface are in the [package README](../README.md).

## One judgement call in the specification

The task says categories occurring "less frequent than the threshold percentage"
should be replaced. That wording is strict, so a category sitting exactly on the
boundary is kept. At a 20% threshold in 10 rows, a category appearing twice stays and
one appearing once is replaced. The choice matters at small sample sizes, which is
where this function tends to get used, so it is stated here rather than left to be
discovered from behaviour.

## Why this helps a logistic regression predicting a binary outcome

One-hot encoding gives every distinct category its own column and therefore its own
coefficient. That coefficient is estimated from the rows in which the category
appears, so a level occurring three times in the training data is fitted on three
observations.

Three problems follow, and consolidation addresses all of them.

A level with a handful of rows produces a coefficient with a large standard error. It
swings between refits and between cross-validation folds, and the model reports
confidence it has not earned.

Separation is the sharper version of the same problem. If every row of a rare
category happens to share the same outcome, which is easy with three rows, the
maximum-likelihood coefficient diverges. Unregularised logistic regression fails to
converge and regularised regression merely hides it.

Unseen categories are the third case. A country absent from training has no column at
all. Consolidation gives the model a defined destination for it: the same fallback
level the other rare categories were pooled into, fitted on enough rows to mean
something.

Pooling rare levels into one fallback trades a little resolution for coefficients the
model can actually estimate. In a `DELAYED (Y/N)` model over a country attribute, the
countries with two or three tickets each carry no reliable signal alone, but together
they form a group with enough support to fit.

## An alternative for a high-cardinality attribute

Target encoding replaces each category with a statistic of the outcome for that
category, typically its mean, smoothed toward the global mean in proportion to how
little support the level has. It produces one numeric column instead of hundreds and
retains ordering information that consolidation discards.

Its cost is leakage. The encoding is computed from the target, so a naive
implementation lets each row see its own outcome, and the model looks excellent until
it meets new data. It has to be cross-fitted, with the encoding for each fold
computed only from the other folds. That is a real constraint rather than a
footnote, and it is why consolidation remains the safer default when the extra
resolution is not needed.

## An algorithm that needs no pre-treatment

Gradient boosting with native categorical support handles this internally. CatBoost
is built around it: its ordered target statistics compute category encodings using
only rows that precede the current one in a random permutation, which enforces the
leakage protection above inside the algorithm instead of leaving it to the person
using it. LightGBM takes a different route and partitions category values directly at
each split without encoding them at all.

Either removes the need to consolidate or encode by hand. Neither removes the need
for a leakage-safe validation split, since the model handles the encoding and not the
experimental design.

## Recorded behaviour

From the committed fixture of 12 training and 6 inference rows
([`baseline-vs-extension.md`](baseline-vs-extension.md)):

| | `channel` | `region` |
|---|---:|---:|
| Categories, percentage rule only | 5 to 4 | 6 to 4 |
| Categories, adding a minimum-count rule | 5 to 3 | 6 to 3 |

Both columns route their unseen inference value to the fallback rather than failing.
The percentage-only path is the assignment baseline and matches the standalone helper
exactly. The minimum-count rule is opt-in, because a percentage alone still admits
under-supported levels once the dataset grows.

The fixture is sanitised engineering evidence rather than production data. Real
threshold and minimum-count values are a policy decision for whoever owns the model.
