# Task 1 — Internet-search agent (compact baseline)

A small LLM agent that answers questions beyond the model's embedded knowledge by
searching the live web, reading the pages it finds, and writing an answer in its
own words with the sources it actually opened.

It runs entirely locally against Ollama. One file, under 400 lines, and it reuses the
two dependencies this workspace already has.

It sits beside the bounded research agent in [`../README.md`](../README.md),
which answers the same assignment with verified grounding instead of prose.
The two mark opposite ends of the readability-versus-provability trade-off.

## Run it

From the repository root, with Ollama running:

```bash
ollama pull qwen3:8b
make web-agent Q="what does wikipedia say about germany?"
```

`httpx` and `ddgs` are already workspace dependencies, so `uv` supplies them.
`MODEL_NAME` selects a different Ollama model and `MODEL_URL` points at any other
OpenAI-compatible endpoint:

```bash
make web-agent MODEL_NAME=llama3.1:8b Q="what changed in python 3.14?"
```

`MAX_STEPS` (default 6) bounds the tool loop.

## How it works

The agent runs a bounded tool loop. The model replies with exactly one line when
it wants a tool, and we feed the result back:

```
TOOL web_search {"query": "...", "max_results": 5}
TOOL open_url   {"url": "https://..."}
TOOL time_in    {"tz": "Europe/Berlin"}
```

When it has enough, it answers normally and cites the URLs it opened.

Three design decisions are worth calling out:

**A plain-text tool protocol, not OpenAI function calling.** Small local models
parse structured tool schemas unreliably. One line of text works on any
OpenAI-compatible endpoint regardless of how well the engine implements tool
calls. The parser takes the first `TOOL` line and lets a JSON decoder find where
the object ends, because models routinely emit several despite being told not to.

**Search finds pages; `open_url` reads them.** The prompt asks the model to open a
source rather than answer from snippets, and the loop enforces it: if the model
searched but tries to answer without opening anything, the answer is refused once
and it is told to read a result first. Asked what changed in Python 3.13, the
model previously asserted from memory that the version was unreleased, which was
wrong; it now opens the changelog and answers from it.

**A clock is a tool, not mental arithmetic.** The model got timezone offsets
backwards and live-clock pages are JavaScript-rendered, so `time_in` answers
"what time is it in X" directly.

## How it meets the assignment

| Requirement | Where |
|---|---|
| Use an LLM (Ollama / Llama 3.1 given as examples) | Any OpenAI-compatible endpoint; developed against Ollama |
| Integrate a search engine such as DuckDuckGo | `web_search` via DDGS |
| Decide when a web search is appropriate | The prompt routes: no tool call for code, explanation or translation; tools for anything time-sensitive or uncertain |
| Avoid unnecessary searches for greetings and simple queries | A greeting is answered directly, with no tool call |
| Interpret results and return a human-readable answer | The model writes prose in its own words and cites the URLs it opened |

## Limitations

These are real and observed, not hypothetical.

- **Citations are model-asserted, not machine-verified.** The prompt forbids
  inventing URLs, but nothing enforces it. In one run against `qwen3:8b` the agent
  cited `gpe.wikipedia.org`, which does not exist. Treat sources as leads to check,
  not as a grounding guarantee. The bounded agent in this same task takes the
  opposite trade-off: it machine-verifies every claim against retrieved evidence
  and abstains when none supports an answer.
- **The read-before-answering rule pushes back once, then yields.** A second
  refusal is let through so the run always terminates, and a question that needed
  no search is never pushed back at all. It removes the common failure, not every
  one of them.
- **Pages are fetched without JavaScript.** Live tickers and clocks come back as
  placeholders; the prompt tells the model to say so rather than fill the gap.
- **No test suite.** This is a compact baseline, not a hardened service.
- **Answer quality tracks the local model.** A larger model gives noticeably
  better source selection than `qwen3:8b`.
