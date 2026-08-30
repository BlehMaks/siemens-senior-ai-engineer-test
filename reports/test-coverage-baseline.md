# Deterministic coverage baseline

Run `make coverage-report` from the repository root. The command erases prior data,
runs the deterministic pytest suite with branch measurement, and prints every
missing executable line or branch across `task-*/src` and `scripts`.

This inventory is intentionally non-blocking while Tasks 1–3 are changing in a
separate concurrent workstream. The Tasks 4–6 Tier 1 scope is frozen and was
measured together on 2026-08-31. Use the command's `Missing` column as the
line-and-branch worklist for any scope below 100%.

| Scope | Line coverage | Branch coverage | Remaining work |
|---|---:|---:|---|
| Task 1 | Concurrent workstream | Concurrent workstream | Re-measure after its staged changes settle; this plan does not alter it |
| Task 2 | Concurrent workstream | Concurrent workstream | Re-measure after its staged changes settle; this plan does not alter it |
| Task 3 | Concurrent workstream | Concurrent workstream | Re-measure after its staged changes settle; this plan does not alter it |
| Task 4 | 100% (763/763 statements) | 100% (156/156 branches) | None |
| Task 5 | 100% (1,265/1,265 statements) | 100% (380/380 branches) | None |
| Task 6 | 100% (529/529 statements) | 100% (148/148 branches) | None |
| New language/link audits | 100% (106/106 statements) | 100% (46/46 branches) | None |
| Existing scripts | Fresh full-repository result deferred | Fresh full-repository result deferred | Re-measure with Tasks 1–3 after the concurrent workstream settles |

The combined Tasks 4–6 command passed with 2,557/2,557 statements and 684/684
branches. The strict repository-wide gate is not enabled by this baseline commit:
doing so while the independent Tasks 1–3 index is mid-change would make this plan
claim ownership of another workstream. It becomes blocking only after every listed
scope reaches 100% with meaningful assertions and no unjustified exclusions.
