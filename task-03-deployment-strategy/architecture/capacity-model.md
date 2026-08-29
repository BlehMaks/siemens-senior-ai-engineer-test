# C01B: Capacity model and local fake-provider load proof

Status: executable local proof; enterprise envelopes are design probes, not
production capacity claims.

## Capacity worksheet

The production design does not infer capacity from the Siemens name. It starts with
measurable workload variables:

```text
peak_runs_per_second
  = daily_active_users * mean_runs_per_user_day * peak_concentration
    / active_seconds_per_day

in_flight_runs
  = peak_runs_per_second * mean_run_duration_seconds

peak_fetches_per_second
  = peak_runs_per_second * mean_searches_per_run * mean_pages_per_search

required_model_tokens_per_second
  = peak_runs_per_second * mean_model_calls_per_run * mean_tokens_per_call

worker_slots
  = ceil(in_flight_runs / safe_concurrency_per_worker)

cost_per_successful_run
  = (worker_compute + queue_and_storage + retrieval + model + egress)
    / successful_runs

quality_adjusted_cost
  = cost_per_successful_run / accepted_answer_quality_rate
```

The probe intentionally leaves currency cost unset: a useful value requires the
approved model, region, negotiated pricing, cache policy, and measured success and
quality rates. Production telemetry must attribute those inputs by tenant, business
unit, model, and tool, then calculate the two unit-cost formulas above. This avoids
turning public list prices or the fake executor into a Siemens cost claim.

The local probe uses the real Task 2 HTTP app, SQLite repositories, API-key auth,
quota/admission limiter, durable work queue, SSE route, cancellation route, and
local worker. It replaces only the expensive model/search backend with a
deterministic fake executor. That keeps the proof cheap while still exercising the
submission and recovery contracts that matter for deployment.

## Design envelopes

| Envelope | Submission rate | In-flight range | Worksheet point | Fetch rate | Model throughput | Worker slots | Evidence status |
|---|---:|---:|---:|---:|---:|---:|---|
| Pilot | 1 run/s | 20 to 50 | 35 in-flight | 12 fetches/s | 6,000 tokens/s | 4 | Small local fake-provider proof only |
| Business unit | 20 runs/s | 500 to 1,500 | 1,000 in-flight | 400 fetches/s | 200,000 tokens/s | 100 | Unmeasured design probe |
| Enterprise stress | 100 runs/s plus burst | 5,000 to 15,000 | 10,000 in-flight | 3,000 fetches/s | 1,500,000 tokens/s | 1,000 | Unmeasured design probe; not a production claim |

The 100 rps enterprise stress case is deliberately not presented as a locally proven
capacity result. It is a sizing pressure test for provider quotas, inference
capacity, egress controls, regional failover, and queue age. A real enterprise gate
must replay the same contract against representative model latency, public-web and
internal-knowledge rate limits, regional data stores, and business-unit quotas.

## Local proof scenario

Run:

```bash
uv run python -m deployment_strategy.capacity_probe
```

Optional JSON artifact:

```bash
uv run python -m deployment_strategy.capacity_probe --output /tmp/capacity-proof.json
```

The checked-in sample is
[capacity-load-proof.sample.json](capacity-load-proof.sample.json).

Fixed inputs:

| Setting | Value |
|---|---:|
| Initial submissions | 12 |
| Tenant queue admission limit | 8 queued runs |
| Deterministic cancellation | Accepted run index 3 |
| Deterministic model-quota failure | Accepted run index 6 |
| Execution backend | In-process fake executor; no paid cloud, no network model call |
| State and queue | Local SQLite through Task 2 repositories |
| Thresholds | p95 submit ≤ 250 ms, first SSE event availability ≤ 350 ms, recovery ≤ 1,000 ms |

The scenario proves these behaviors:

- submit accepts work until the tenant queue limit is reached;
- excess submissions fail with HTTP 429 instead of creating unbounded work;
- status returns the accepted run as queued before execution;
- SSE resumes after `Last-Event-ID` and measures from submission start until the
  first post-created event is available;
- cancellation terminalizes a queued run and removes its work item;
- idempotent duplicate submission returns the original accepted run;
- same idempotency key with a different payload is rejected with HTTP 409;
- one accepted run deterministically fails with a bounded fake model-quota failure;
- after draining the overload, a recovery submission is accepted and completed.

Checked-in sample measurement on this development machine:

| Metric | Result |
|---|---:|
| Accepted / rejected initial work | 8 / 4 |
| Duplicate behavior | HTTP 202, same run ID |
| Conflicting duplicate | HTTP 409 |
| Terminal results after drain | 6 completed, 1 cancelled, 1 budget_exhausted |
| p95 submit latency | 116.339 ms |
| p95 first-event availability across eight runs | 264.403 ms |
| Oldest real queue age before drain | 155.131 ms |
| Recovery time through terminal completion | 18.194 ms |
| Recovery terminal state | completed |
| Whole scenario elapsed | 338.232 ms |
| Resource usage | +4,669,440 platform `ru_maxrss`, +0.136668 user CPU s, +0.107824 system CPU s |

These numbers are local proof-of-contract measurements, not production performance
claims. They are useful because the assertions fail closed if admission,
idempotency, SSE delivery, cancellation, model-quota degradation, or recovery
regresses.
