# Production web-research gap analysis and remediation plan

## Purpose

This document is a self-contained handoff for a future implementation session. It
compares the current Siemens research-agent solution with production web-research
principles supplied by the user and proposes a prioritized remediation plan.

No production code was changed during the analysis that produced this document.
Before implementing anything, re-check the referenced code because the repository
may have evolved.

## Scope

The source principles apply differently across the six assignment tasks:

- **Task 1:** directly applicable. It owns query planning, web search, content
  retrieval, evidence, answer generation, and citation validation.
- **Tasks 2 and 3:** applicable to cross-cutting production concerns: durable state,
  observability, quotas, resilience, security, cost, and deployment.
- **Task 5:** applicable to ranking, hybrid retrieval, explainability, abstention,
  and relevance evaluation. Much of this is already implemented.
- **Tasks 4 and 6:** web/RAG-specific requirements are mostly out of scope; only
  general evaluation and reproducibility principles apply.

## Executive assessment

The current solution has a strong safety-oriented foundation:

- explicit and bounded agent state transitions;
- hard limits for time, queries, pages, bytes, model calls, attempts, and tokens;
- SSRF-safe URL handling and redirect revalidation;
- isolation of web content as untrusted data;
- immutable evidence provenance;
- strict citation validation and safe abstention;
- durable asynchronous API execution, quotas, cancellation, and worker leases;
- infrastructure budget alerts and deployment controls.

However, the retrieval plane remains closer to an assessment prototype than a
production research system. The agent can reject unsafe or unsupported output, but
it cannot yet reliably discover, extract, rank, and trace facts from real long-form
Siemens documents.

The most serious current limitation is that a successfully extracted document may
contain up to 100,000 normalized characters, while answer synthesis receives only
the first 400 characters and normally no additional quotes. Facts located later in
a report are therefore unavailable to the model.

## Current target boundaries

Tasks 1 to 3 already have useful ownership boundaries:

1. Task 1 owns research orchestration and answer evidence.
2. Task 2 owns the API, durable runs, authentication, quotas, and worker lifecycle.
3. Task 3 owns cloud composition, identity, operational infrastructure, and cost
   controls.

The remediation should preserve these boundaries. Extend the Task 1 pipeline behind
typed ports instead of rewriting the Task 2 control plane or duplicating logic in
Task 3.

## Critical production gaps

### 1. Context is limited to the beginning of each page

`build_evidence` stores normalized source text, but the public summary is a maximum
of 400 characters. `ResearchRunner._synthesize` passes that summary to the model as
the document excerpt. Quotes are empty unless explicitly supplied, and the normal
runner does not create them.

Impact:

- facts in the middle or end of long reports cannot be used;
- search success can still result in an uninformative context;
- citation validation may be safe while answer recall remains poor.

Relevant code:

- `task-01-search-agent/src/search_agent/evidence.py`
- `task-01-search-agent/src/search_agent/runner.py`

### 2. No document chunking or retrieval layer

There is no semantic or structural chunking, chunk metadata, chunk retrieval,
parent-child expansion, or context selection. Search hits are fetched in order and
converted directly to evidence records.

Impact:

- large documents cannot be searched internally;
- context selection is based on document order rather than question relevance;
- there is no inspectable reason why a particular passage was selected.

### 3. PDF, tables, and JavaScript-heavy pages are not handled

The fetcher accepts HTML, XHTML, and plain text. The HTML extractor deliberately
excludes tables. The production runtime does not compose a controlled browser
fallback.

Impact:

- sustainability, financial, compliance, and engineering reports distributed as
  PDF cannot be researched directly;
- values stored in tables may be lost;
- JavaScript-only report pages become fetch/extraction failures.

### 4. No source authority model

`SitePolicy` is a security control, not an authority scorer. The current pipeline
does not distinguish official documentation, audited reports, regulatory filings,
major media, secondary articles, forums, or content farms.

Impact:

- a relevant but weak secondary source can outrank a primary source;
- critical claims may be supported by only one unofficial page;
- query-specific source policies cannot be enforced.

### 5. No real publication freshness model

Evidence stores `retrieved_at`, but does not capture publication date, event date,
page update date, document version, or effective date. Evidence collected during a
run is always newly retrieved, even when the underlying information is obsolete.

Impact:

- “latest” and “current” questions cannot be answered reliably;
- old documentation may appear fresh because it was fetched recently;
- conflicts cannot be resolved using temporal precedence.

### 6. No hybrid document/chunk ranking

The system preserves search-provider order and deduplicates exact canonical URLs.
It does not combine lexical relevance, semantic relevance, authority, freshness,
metadata filters, or a reranker.

Impact:

- exact identifiers, model numbers, dates, and report names are not treated
  differently from broad semantic questions;
- the best source or passage is not guaranteed to enter the model context;
- retrieval failures are difficult to distinguish from model failures.

### 7. Insufficient deduplication

Exact URLs are deduplicated, but mirrored or near-identical content under different
URLs is not removed before context construction. Content hashes are used mainly for
citation-source diversity checks.

Impact:

- copied press releases may appear as independent confirmation;
- context capacity can be wasted on duplicate material;
- source diversity can be overstated unless explicitly configured and checked.

### 8. Incomplete failure taxonomy

The public contract distinguishes `search_failed`, `no_evidence`,
`budget_exhausted`, and `validation_failed`. Fetch and extraction failures are
mostly aggregated as `failed_pages`. Provider, generation, answer-scope, and
citation-verification failures commonly become `validation_failed`.

Impact:

- operators cannot quickly identify the failing stage;
- prompt changes may be attempted for retrieval failures;
- provider and extraction reliability cannot be measured independently.

### 9. No full production research trace

The in-memory Task 1 snapshot contains the request, plan, hits, evidence, and answer.
Task 2 persists the final public answer and a reduced reflection containing action
types, aggregate failures, evidence IDs/URLs, and aggregate usage. It does not retain
the full privacy-safe research decision trail.

Missing trace data includes:

- request classification and reasoning code;
- generated search queries and selected providers;
- rejected search hits and rejection reasons;
- document metadata and fetch/extraction status;
- generated chunks and ranking scores;
- selected context and context hash;
- verified claims;
- stage-level latency and estimated cost.

### 10. Evaluation does not run the real end-to-end pipeline

The checked 34-case Task 1 evaluation is useful as a deterministic contract gate,
but it constructs `RunResult` observations from frozen fixtures. It does not execute
the real planner, search adapter, fetcher, extractor, retriever, model, and verifier.

Impact:

- a perfect fixed evaluation score does not prove source or chunk recall;
- extraction regressions may not affect the evaluation;
- production changes to query rewriting and ranking cannot be compared objectively.

### 11. Single search provider without search-plane fallback

The runtime composes one DDGS/DuckDuckGo backend. The port is replaceable, but there
is no orchestrator for official-domain search, documentation search, news search,
or provider fallback.

Impact:

- one provider outage can stop research;
- specialized sources may never appear;
- quota, regional, and coverage failures cannot be mitigated.

### 12. Sequential and non-adaptive research

Search queries and document fetches are performed sequentially. The plan is created
once; the runner does not evaluate evidence sufficiency and perform a bounded second
search pass.

Impact:

- latency grows roughly with the number of queries and pages;
- one slow page consumes the overall deadline;
- weak first-pass evidence cannot trigger a targeted follow-up search.

### 13. No web-research cache

There is no cache for search results, fetched documents, extracted content, chunks,
embeddings, or ranking output.

Impact:

- repeated research consumes unnecessary latency, quotas, and cost;
- external provider instability is experienced repeatedly;
- there is no query-class-specific freshness policy.

### 14. No per-run research cost attribution

Task 3 provides infrastructure budget alerts and hard capacity limits, but the
application does not calculate the cost of an individual research run by tenant,
query class, search provider, model, or retry waste.

Impact:

- cost per successful, quality-accepted answer is unknown;
- expensive failure patterns are hard to detect;
- business-unit chargeback and budget decisions lack evidence.

### 15. Enterprise Siemens controls are designed but not proven

The repository documents a production direction, but corporate identity, data
residency, DLP, SIEM integration, retention, disaster recovery, regional isolation,
and production-scale load evidence remain follow-on work.

## Proposed target pipeline

```text
Request validation
  -> Query analysis
  -> Search planning
  -> Multi-source search
  -> Result normalization and URL deduplication
  -> Guarded fetch / PDF loader / controlled browser loader
  -> Content extraction
  -> Structural chunking
  -> Content deduplication
  -> Source quality and freshness scoring
  -> Hybrid retrieval and reranking
  -> Context building
  -> Claim extraction
  -> Claim verification
  -> Answer generation
  -> Citation verification
  -> Response or typed abstention
```

Cross-cutting concerns:

```text
Tracing | Logging | Metrics | Caching | Rate limits | Cost | Evaluation | Security
```

## P0 remediation: retrieval correctness

### A. Introduce document and chunk contracts

Add internal immutable models similar to:

```json
{
  "document_id": "doc-...",
  "chunk_id": "chunk-...",
  "url": "https://...",
  "title": "...",
  "domain": "siemens.com",
  "published_at": "2026-01-10T00:00:00Z",
  "updated_at": null,
  "retrieved_at": "2026-08-30T12:00:00Z",
  "section": "Scope 3 emissions",
  "page": 42,
  "language": "en",
  "source_type": "official_report",
  "content_hash": "...",
  "text": "..."
}
```

Requirements:

- stable deterministic IDs derived from canonical provenance;
- bounded text and metadata fields;
- exact connection back to the source document;
- page/section/table provenance where available;
- clear distinction between publication, update, event, and retrieval times.

### B. Add format-specific extraction

Implement separate bounded extractors behind the existing extraction boundary:

- HTML main-content extractor;
- HTML table extractor;
- PDF text and table extractor;
- controlled browser extractor only when static extraction is insufficient.

All loaders must preserve current URL guardrails, byte/time limits, cancellation,
content-type validation, and untrusted-data isolation.

Acceptance examples:

- a sustainability PDF can be researched end to end;
- a value from a table retains its header, page, and section;
- JavaScript fallback cannot access private addresses or secrets;
- one failed format parser does not disclose document bytes or exception internals.

### C. Replace first-400-character context with top-k chunks

Do not send full documents to the LLM. Chunk by structural boundaries:

- headings and sections;
- paragraphs;
- table blocks with repeated headers;
- FAQ items;
- issue/discussion messages where relevant.

Use parent-child retrieval:

1. rank small chunks for precision;
2. expand selected chunks to bounded parent sections for answer context.

Acceptance criteria:

- facts near the middle and end of long documents are retrievable;
- selected context remains within the run token budget;
- every context fragment has exact source provenance;
- selected context is deterministic for a frozen corpus and configuration.

### D. Add source authority and freshness policies

Classify sources into a bounded taxonomy:

- `official_report`;
- `official_documentation`;
- `regulatory_filing`;
- `government`;
- `academic`;
- `major_media`;
- `technical_article`;
- `forum`;
- `unknown`.

Define query-specific policies. For example, a Siemens report query should prefer:

```text
official Siemens report
  -> regulatory or audited filing
  -> official announcement
  -> reputable secondary analysis
```

An unofficial source should not independently establish a critical claim when a
primary source is expected.

### E. Introduce hybrid ranking

Begin with a simple, measurable score:

```text
final_score =
    lexical_relevance
  + semantic_relevance
  + source_authority
  + freshness
  + query_specific_rules
```

Recommended staged implementation:

1. lexical ranking over titles, snippets, and chunks;
2. metadata/source/freshness rules;
3. rerank only a bounded candidate set;
4. add embeddings only if the evaluation demonstrates improved recall or nDCG.

Exact identifiers, versions, dates, product numbers, and report names must retain a
strong lexical channel. Do not replace lexical retrieval with embeddings.

### F. Add content-level deduplication

Use normalized content hashes for exact duplicates and optional embedding or
shingling similarity for near duplicates. Deduplicate before context construction,
while retaining a list of mirror URLs for provenance.

Independent-source requirements must operate on independent content and ownership,
not merely different URLs.

### G. Use claim-first answer generation

Adopt a two-stage answer path:

```text
selected chunks -> verified claims -> final answer
```

Example intermediate claim:

```json
{
  "claim": "Siemens published the report in December 2025.",
  "evidence_id": "ev-...",
  "chunk_id": "chunk-...",
  "source_url": "https://...",
  "section": "Report overview",
  "page": 4
}
```

Before rendering an answer:

- verify that each claim is supported by the referenced chunk;
- verify provenance and temporal metadata;
- detect a more recent contradictory source;
- require source diversity where the query policy demands it;
- abstain when the minimum evidence policy is not met.

Keep the existing deterministic evidence-ID and URL validation as a final guard.

## P0 remediation: end-to-end evaluation

Keep the current fixed fixture suite as a fast contract test. Add a separate frozen
web evaluation corpus containing:

- captured search results;
- HTML pages and long reports;
- representative PDF reports;
- tables containing target facts;
- duplicates and press-release mirrors;
- outdated and current sources;
- conflicting sources;
- missing publication dates;
- JavaScript-only and unavailable pages;
- prompt-injection content;
- private and metadata URLs.

The end-to-end suite must execute the real production pipeline through deterministic
provider adapters.

Required metrics:

- source recall@k;
- chunk recall@k;
- extraction completeness;
- primary-source rate;
- freshness/temporal correctness;
- answer correctness;
- faithfulness;
- citation correctness;
- citation completeness;
- conflict handling;
- abstention quality;
- latency and cost per successful answer.

Add every confirmed user-reported failure to the regression corpus.

## P1 remediation: observability and failure isolation

### A. Persist a privacy-safe research trace

For each run, retain bounded metadata for:

- original request and query classification;
- generated queries and chosen providers;
- normalized hits and rejection reasons;
- fetch/extraction outcomes;
- document and chunk IDs;
- ranking component scores;
- selected context IDs and context hash;
- verified claims;
- final answer and citations;
- stage latency, usage, retry count, and estimated cost.

Do not persist hidden reasoning, credentials, full prompts, or raw pages in normal
operational storage. If raw debugging artifacts are required, isolate, encrypt, and
expire them under a separate authorized retention policy.

### B. Add stage-level telemetry

Recommended spans or stage records:

```text
planning
search.<provider>
fetch.<domain>
extract.<format>
chunk
rank
context_build
claim_extract
generate
verify
```

Keep metric dimensions bounded. Domains and tenant IDs should not become unbounded
metric labels; use logs/traces with pseudonymized IDs for high-cardinality details.

### C. Expand typed failures

At minimum distinguish:

- `planning_failed`;
- `search_failed`;
- `fetch_failed`;
- `extraction_failed`;
- `retrieval_failed`;
- `generation_failed`;
- `citation_failed`;
- `verification_failed`;
- `budget_exhausted`;
- `cancelled`.

Public responses may remain coarse and safe, while the internal trace records the
precise bounded reason code.

## P1 remediation: search-plane resilience

### A. Normalize multiple search providers

Introduce a common result contract:

```json
{
  "title": "...",
  "url": "...",
  "snippet": "...",
  "published_at": "...",
  "provider": "...",
  "source_type": "...",
  "rank": 1
}
```

Candidate provider order for the primary scenario:

1. official Siemens domain/report search;
2. documentation or filing search;
3. general web search;
4. news search for current events;
5. secondary sources when primary evidence is insufficient.

Each provider adapter should implement:

- timeout;
- bounded retry with exponential backoff and jitter;
- 429/rate-limit handling;
- circuit breaker;
- quota monitoring;
- pagination bounds;
- normalized errors;
- explicit fallback behavior.

### B. Add a bounded adaptive research loop

After the initial search, calculate evidence sufficiency:

- was an authoritative source found;
- was the requested date/version found;
- did a relevant chunk enter top-k;
- are sources independent;
- are there unresolved contradictions.

When sufficiency is low, permit a bounded follow-up action:

- rewrite the query with exact terms, dates, or entities;
- restrict to an official domain;
- use a specialized provider;
- expand top-k;
- search for a contradicting or confirming primary source.

Stop conditions should include:

- maximum queries, pages, and iterations;
- wall-clock, token, and monetary budgets;
- sufficient confidence/evidence;
- no new unique sources;
- cancellation.

### C. Parallelize independent work

Run independent provider searches and page fetches concurrently with:

- a global semaphore;
- per-provider and per-domain concurrency limits;
- per-operation timeouts;
- one overall run deadline;
- cancellation of remaining work once evidence is sufficient.

One slow or failing page must not block the entire answer when enough valid evidence
has already been collected.

## P2 remediation: caching and cost

### A. Add cache layers only after trace and evaluation exist

Potential cache entries:

- normalized search results;
- fetched document bytes or safe content-addressed artifacts;
- extracted documents;
- chunks;
- embeddings;
- ranking results.

TTL must depend on the information type:

- historical report: long;
- versioned documentation: until version change;
- news: short;
- price or operational status: very short.

Cache keys must include relevant policy, parser, model, ranking, and corpus versions.
Do not allow stale cached content to bypass freshness requirements.

### B. Attribute cost per run

Record:

- provider calls and retries;
- fetched bytes;
- extraction work;
- model input/output tokens;
- cache hits and misses;
- failed-fetch and retry waste;
- cost by tenant, query class, provider, model, and artifact version.

Primary FinOps measures:

```text
cost_per_successful_run
quality_adjusted_cost = cost_per_successful_run / accepted_answer_quality_rate
```

## P2 remediation: enterprise readiness

Before a Siemens-wide rollout, prove rather than merely document:

- corporate OIDC and workload identities;
- business-unit and tenant quotas;
- regional data residency enforcement;
- isolation of public-web and internal connectors;
- DLP before external model submission;
- immutable SIEM/audit integration;
- retention and deletion enforcement;
- backup, restore, and disaster-recovery exercises;
- regional cell isolation and controlled failover;
- production-shaped load, saturation, queue-age, and recovery tests;
- SLOs for latency, availability, answer quality, and citation correctness.

## Recommended implementation sequence

### Phase 1: make real documents retrievable

1. Define document/chunk/metadata contracts.
2. Add PDF and table extraction.
3. Add structural chunking.
4. Replace the 400-character context with top-k chunks.
5. Add lexical ranking, source authority, freshness, and exact content deduplication.
6. Add a frozen end-to-end corpus and retrieval metrics.

### Phase 2: make failures diagnosable

1. Introduce stage-level internal failure codes.
2. Persist a privacy-safe trace.
3. Add stage latency and usage telemetry.
4. Add claim-first generation and contradiction handling.

### Phase 3: make the search plane resilient and efficient

1. Add provider normalization and official-domain search.
2. Add bounded retry, fallback, and circuit breakers.
3. Parallelize independent search/fetch operations.
4. Add a bounded adaptive research iteration.
5. Add cache and cost attribution after metrics establish the need.

### Phase 4: prove enterprise readiness

1. Run production-shaped quality and load tests.
2. Exercise recovery, deletion, failover, and incident procedures.
3. Complete corporate identity, residency, DLP, and SIEM integration.
4. Define and enforce quality, latency, availability, and cost SLOs.

## Acceptance gates before calling the system production-ready

- A fact from the middle of a long Siemens PDF can be retrieved and cited with a
  stable page/section reference.
- Table values retain headers, units, and provenance.
- Official and more recent sources outrank weaker or obsolete sources.
- Mirrored content cannot satisfy independent-source requirements.
- Search, extraction, retrieval, generation, and verification failures are visible
  as distinct internal stages.
- Operators can reconstruct why a source and chunk were selected without accessing
  hidden reasoning or secrets.
- A frozen end-to-end evaluation exercises the actual production entry points.
- Confirmed user failures become regression cases.
- One provider outage or slow page does not stop a run with sufficient remaining
  evidence.
- Every run stays within time, query, page, byte, token, concurrency, and cost
  budgets.
- Production-shaped load and recovery tests meet approved SLOs.

## Existing components to preserve

The following should be extended, not discarded:

- `QueryPlanner` and strict planning contracts;
- `ResearchRunner` hard budgets and explicit orchestration;
- `GuardedFetcher`, `UrlGuard`, and `SitePolicy` safety boundaries;
- immutable `EvidenceRecord` provenance;
- `AnswerValidator` citation-ID and URL checks;
- `RunStateGraph` legal state transitions;
- Task 2 durable run, quota, cancellation, and lease contracts;
- Task 3 identity, deployment, and budget controls;
- Task 5 lexical/hybrid retrieval evaluation patterns.

## Verification performed during the analysis

- Task 1 tests: **519 passed**.
- Fixed Task 1 evaluation: **34 cases passed**, including all hard gates.
- The documented direct evaluation command did not import `search_agent` in the
  current root environment. Running with
  `PYTHONPATH=task-01-search-agent/src` succeeded. Treat this as a small
  reproducibility/documentation gap.
- The working tree was clean before this handoff document was added.

## Guidance for the next session

Do not implement every advanced technique at once. Start with the smallest pipeline
that can disprove or confirm the main quality hypothesis:

```text
PDF/HTML extraction
  -> structural chunks
  -> lexical + authority + freshness ranking
  -> top-k context
  -> existing citation validation
  -> frozen end-to-end evaluation
```

Add embeddings, multiple model sizes, semantic cache, and complex provider routing
only when measured recall, latency, or cost shows that the simpler pipeline is
insufficient.
