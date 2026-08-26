# ADR-0001: Use an explicit agent state machine

- Status: accepted
- Date: 2026-08-26

## Context

The research agent must enforce budgets, cancellation, evidence validation, and
public progress events. Those guarantees need visible transitions that can be tested
without a model or network.

## Decision

Implement a small typed state machine in Task 1. Deterministic code owns legal
transitions and the plan-search-fetch-extract-answer-validate sequence. Model output
may propose structured content but cannot choose capabilities or bypass policy.

## Alternatives

- A general agent framework was rejected because no required feature currently
  offsets the extra control surface and dependency.
- A free-form model loop was rejected because budgets, replay, and terminal-state
  guarantees would be implicit.

## Consequences

The runner contains some explicit orchestration code, but its behavior is inspectable
and fixture-testable. A framework can replace it later only if a measured need such
as checkpoint interoperability exceeds the migration cost.

