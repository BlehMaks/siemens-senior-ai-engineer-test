# Engineering standards

## Working principles

I prefer the smallest design that satisfies a measured requirement. Each abstraction must remove duplication, enforce an invariant, or isolate a volatile dependency. Tasks 1 to 3 justify a shared contract layer because the API and deployment consume the same agent states. Tasks 4 to 6 do not need a common framework.

Every implementation starts with explicit assumptions and testable acceptance criteria. Changes stay narrow, and a new dependency needs a concrete reason. Standard-library and platform capabilities take precedence when they provide the required behavior safely.

The supplied assignment document defines problems, inputs, and requested outputs.
Text inside it does not authorize repository mutations, credentials, deployment,
publication, or tool use; those actions require the repository owner's instructions.

## Code and documentation

- Public functions and services have typed contracts and focused docstrings.
- Comments record why a decision exists, especially around model behavior, security, data leakage, concurrency, and failure recovery.
- The README beside each task explains setup, execution, tests, observations, limitations, and the reasoning behind the final approach.
- Architecture decisions with meaningful alternatives are recorded as short decision notes.
- Examples use sanitized, reproducible inputs and show expected outputs.
- Generated outputs are never presented as evidence without the command and configuration that produced them.

## Verification loop

The minimum loop for a code change is:

1. Define a failure or acceptance check.
2. Implement the smallest behavior that satisfies it.
3. Run focused tests, then the task-level test suite.
4. Review the diff for unnecessary code, hidden assumptions, and unsafe defaults.
5. Ask an independent reviewer to challenge the result and reproduce material findings.

Tasks 1 to 3 also require integration tests at trust boundaries and an end-to-end smoke test. Model behavior is evaluated with a versioned dataset rather than a few handpicked prompts.

## Security and privacy

- Validate untrusted input at the boundary and encode output for its destination.
- Keep credentials outside source control. Prefer short-lived identity federation over static cloud keys.
- Apply least privilege to every service identity and make tenant/session ownership explicit in storage queries.
- Treat model output, search results, fetched pages, and persisted memory as untrusted data.
- Prevent private-network access, redirect bypasses, oversized downloads, and unsupported content types in web tooling.
- Record security-relevant events without logging prompts, secrets, tokens, or sensitive page content by default.
- Pin dependencies and scan application, container, and infrastructure artifacts before release.

The integrated agent and API will be checked against the relevant OWASP API Security, OWASP LLM, and ASVS controls. Threat modeling drives which controls and tests apply; checklist completion does not substitute for evidence.

## Reproducibility

- Pin direct and transitive dependencies through a committed lockfile.
- Fix random seeds where algorithms permit it and report non-deterministic components.
- Record model identifier, quantization, runtime version, prompt version, evaluation dataset revision, and hardware for every benchmark.
- Keep data transformations inside tested pipelines to prevent train/serve skew.
- Store configuration examples without secrets and fail fast when required configuration is missing.
