# ADR-0004: Select the local model through a frozen M5 benchmark

- Status: accepted
- Date: 2026-08-26

## Context

The target machine is a MacBook Pro M5 with 48 GB memory. The current development
machine cannot provide valid performance evidence, and model popularity is not a
selection result.

## Decision

Keep inference behind an async provider protocol. CI uses a deterministic fake;
Ollama is the first local runtime adapter. Before measuring candidates, freeze the
evaluation cases, quantization and context settings, warm-up and repeat rules,
hardware metadata, failure handling, and rubric. Compare Qwen3 8B/14B, Llama 3.1
8B, and Mistral Small 3.1 24B on schema adherence, citation grounding, injection
resistance, planning quality, latency, throughput, memory, and failure rate.

## Alternatives

- Selecting a model from documentation or this machine was rejected as unverifiable.
- Coupling orchestration to an Ollama SDK was rejected because the HTTP contract is
  small and provider portability is required.
- Making a cloud model mandatory was rejected because local execution is an
  assignment constraint and paid inference must not gate tests.

## Consequences

The repository may publish a runnable benchmark with selection marked pending if the
M5 is unavailable. Semantic or procedural model proposals remain disabled unless
the measured hard gates pass.

