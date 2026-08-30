# Test strategy

## Feedback loop

Every implementation change follows the same evidence loop:

1. State the behavior and the smallest check that can disprove it.
2. Implement the narrowest change that satisfies the check.
3. Run focused tests, then the affected package and workspace gates.
4. Commit one coherent change and run the mandatory adversarial review.
5. Reproduce confirmed findings, fix them in a separate commit, and rerun the
   affected gates.

Reviewer reproduction tests stay uncommitted until the finding is accepted or
rejected. Accepted tests become ordinary regression coverage with the remediation.

## Local gates

| Gate | Command | Owner | Failure policy |
|---|---|---|---|
| Locked dependency graph | `make lock-check` | change author | Blocking |
| Formatting | `make format-check` | change author | Blocking |
| Lint | `make lint` | change author | Blocking |
| Strict production typing | `make type` | change author | Blocking |
| Unit and integration tests | `make test` | change author | Blocking; flaky retries are not accepted |
| Diagnostic line and branch coverage | `make coverage-report` | change author | Non-blocking until the complete inventory is closed |
| Complete local gate | `make check` | change author | Blocking |
| Clean-machine Tasks 1 to 6 check | `make local-submission` | release owner | Blocking before delivery |
| Running Tasks 1 to 3 API smoke | `make local-acceptance` | release owner | Blocking before cloud deployment |
| Submission boundary | `make audit-submission` | release owner | Blocking |
| English-only repository text | `make audit-language` | change author | Blocking |
| Local Markdown links | `make audit-links` | change author | Blocking |

Task packages add focused unit, integration, security, and evaluation markers only
when those layers exist. The mandatory fixture suites require neither internet
access, a cloud account, nor a local language model. Hardware and live-cloud tests
use explicit opt-in commands and never replace deterministic coverage. Task 4 and
Task 5 data checks use `SIEMENS_TASK4_INPUT_DIR` and `SIEMENS_FUSE_CSV`; without
those private inputs, the affected tests skip with a reason.

## Review and security gates

The adversarial reviewer is test-and-report only. Security review maps implemented
boundaries to the relevant OWASP API, ASVS, LLM, and Agentic risks. Release review
also runs dependency, secret, container, Terraform, workflow, provenance,
documentation, and ponytail simplicity checks.

A missing optional scanner must fail with an installation command or be recorded as
an unavailable non-mandatory gate. It must not print a false pass. Critical and high
findings block release; lower-severity findings need an explicit disposition.

## Coverage standard

Coverage combines a numeric backstop with requirement-based review. Tests must
exercise legal behavior, each documented boundary, error paths that protect data or
security, and concurrency or model failure where applicable. `make coverage-report`
measures lines and branches in every `task-*/src` tree and `scripts` without blocking
intermediate commits. The checked-in inventory records current misses by package.

No production path is excluded merely because it is difficult to test. Generated
code and import-only files may be excluded only through a documented, independently
reviewed rule; every `pragma: no cover` needs the same justification. The strict
100% line-and-branch gate is wired into `make check` only after the inventory is
closed. A green suite remains meaningful only when it uses production entry points
and asserts externally observable outcomes.
