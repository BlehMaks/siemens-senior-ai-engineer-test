# Deterministic coverage baseline

On 2026-08-31, the repository was re-measured after the Tasks 1-6 submission
cleanup with the deterministic full-suite command flow rooted at
`make coverage-report` and a clean comparison snapshot of `origin/master`
(`b2256a1`). The current `HEAD` evidence comes from
the committed [`test-coverage-evidence.json`](test-coverage-evidence.json)
summary. Its raw values are reproducible with `make coverage-report` followed
by `coverage json`; the baseline was measured with the same flow in a clean
snapshot of the named revision.

Definitions used below:

- Statement coverage: executable source lines covered by tests.
- Branch coverage: measured decision branches covered by tests.
- Combined coverage: coverage.py's repository-wide percentage, which blends
  statements and branches into one score.

The final full-repository test corpus now passes 1,848 tests with 3 explicit
owner-private-data skips. The `origin/master` baseline passed 1,366 tests with
5 owner-private/public-data skips. That is 482 more passing tests at `HEAD`,
plus substantially more covered production code, not just a small percentage
change. A clean clone without the employer `Fuse.csv` converts two Task 5
checks from passes to explicit skips; it does not change the collected corpus
or public implementation coverage.

## Current `HEAD` whole-repository coverage

| Scope | Statements | Branches | Combined coverage context | Notes |
|---|---:|---:|---:|---|
| Repository audits (`scripts/`) | 88.03% (544/618) | 84.23% (283/336) | 86.69% weighted by coverage.py internals | Audit and submission helpers are measured together |
| Task 1 | 87.54% (4,428/5,058) | 70.92% (1,078/1,520) | 83.70% weighted by coverage.py internals | Large trace, memory, and retrieval surface dominates remaining misses |
| Task 2 | 89.05% (4,302/4,831) | 72.75% (1,113/1,530) | 85.13% weighted by coverage.py internals | Storage, cloud-state, and schema branches drive the remainder |
| Task 3 | 94.71% (376/397) | 80.26% (61/76) | 92.39% weighted by coverage.py internals | Remaining misses are in deployment helper edge paths |
| Task 4 | 100.00% (946/946) | 100.00% (214/214) | 100.00% | Three owner-private-data checks stay skipped unless `SIEMENS_TASK4_INPUT_DIR` is supplied |
| Task 5 | 100.00% (1,345/1,345) | 100.00% (396/396) | 100.00% | Fully covered in public deterministic scope |
| Task 6 | 100.00% (660/660) | 100.00% (202/202) | 100.00% | Fully covered in public deterministic scope |
| Repository total | 90.95% (12,601/13,855) | 78.31% (3,347/4,274) | 87.97% | `coverage.py` total across `task-*/src` and `scripts` |

## Test corpus comparison

| Revision | Result | Skip count | Notes |
|---|---:|---:|---|
| Current `HEAD` | 1,848 passed | 3 skipped | All skips are explicit Task 4 owner-private-data checks; a clean clone has two additional Task 5 private-catalog skips |
| `origin/master` (`b2256a1`) | 1,366 passed | 5 skipped | Three Task 4 private-data skips plus two Task 5 owner-only Fuse dataset skips |
| Delta | +482 passed | -2 skipped | More deterministic public coverage; the skip delta reflects the populated Task 5 catalog in this checkout |

## Coverage comparison against `origin/master`

`origin/master` measured 86.97% combined repository coverage. Current `HEAD`
measured 87.97% combined repository coverage, 90.95% statement coverage, and
78.31% branch coverage. The repository-wide combined increase is +1.00 points,
while the covered code surface grew materially because Tasks 4-6 added and
closed new production modules instead of merely re-running the old suite.

For the business-extension scopes, the old-vs-current improvement is much
larger than the repository-wide headline:

| Scope | `origin/master` statements | Current statements | Delta | `origin/master` branches | Current branches | Delta |
|---|---:|---:|---:|---:|---:|---:|
| Task 4 | 95.15% | 100.00% | +4.85 | 86.17% | 100.00% | +13.83 |
| Task 5 | 86.23% | 100.00% | +13.77 | 72.41% | 100.00% | +27.59 |
| Task 6 | 95.61% | 100.00% | +4.39 | 90.91% | 100.00% | +9.09 |

These task-level gains are the clearest evidence that final delivery quality is
stronger than the original deliverables: the repository now ships a much larger
deterministic regression corpus, complete public coverage for Tasks 4-6, and a
clean submission path that keeps owner-private checks explicit instead of
hiding them behind unverifiable claims.
