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
| Complete local gate | `make check` | change author | Blocking |
| Submission boundary | `make audit-submission` | release owner | Blocking |

Task packages add focused unit, integration, security, and evaluation markers only
when those layers exist. The mandatory fixture suites require neither internet
access, a cloud account, nor a local language model. Hardware and live-cloud tests
use explicit opt-in commands and never replace deterministic coverage.

## Review and security gates

The adversarial reviewer is test-and-report only. Security review maps implemented
boundaries to the relevant OWASP API, ASVS, LLM, and Agentic risks. Release review
also runs dependency, secret, container, Terraform, workflow, provenance,
documentation, and ponytail simplicity checks.

A missing optional scanner must fail with an installation command or be recorded as
an unavailable non-mandatory gate. It must not print a false pass. Critical and high
findings block release; lower-severity findings need an explicit disposition.

## Coverage standard

Coverage is requirement based rather than percentage driven. Tests must exercise
legal behavior, each documented boundary, error paths that protect data or security,
and concurrency or model failure where applicable. A green suite is meaningful only
when it uses production entry points and asserts externally observable outcomes.
