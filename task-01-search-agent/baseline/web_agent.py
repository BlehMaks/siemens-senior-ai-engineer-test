#!/usr/bin/env python3
"""Internal web tool: let the local model search the web and read pages.

Direct call, local only — this talks to the model on 127.0.0.1:8080 and is NOT
exposed through the public tunnel. It runs a small tool loop:

    you ── question ──> model
    model asks for a tool  ──>  we run it (DuckDuckGo search / fetch a URL)
    result fed back to model  ──>  ... repeat ...  ──>  final answer

The protocol is plain text (not OpenAI function-calling), so it works with any
OpenAI-compatible endpoint regardless of how well the engine parses tool calls.
The model emits exactly one line:

    TOOL web_search {"query": "...", "max_results": 5}
    TOOL open_url   {"url": "https://..."}

and we reply with the result. When it has enough, it answers normally.

Usage:  scripts/web.sh "your question"     (wrapper sets the venv)
Env:    MODEL_URL (default http://127.0.0.1:8080/v1), MODEL_NAME, API_KEY,
        MAX_STEPS (default 6), TEMP (default 0.3)
"""

import contextlib
import difflib
import json
import os
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from typing import ClassVar

import httpx

MODEL_URL = os.environ.get("MODEL_URL", "http://127.0.0.1:8080/v1").rstrip("/")
MODEL_NAME = os.environ.get("MODEL_NAME", "")
API_KEY = os.environ.get("API_KEY", "")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "6"))
TEMP = float(os.environ.get("TEMP", "0.3"))

SYSTEM = """You are a helpful assistant with live internet access via two tools.

Right now it is {now} ({tz}). Use this for any "today"/"tomorrow"/"current time"
question instead of guessing — you have no other clock.

To use a tool, reply with EXACTLY ONE line and nothing else:
TOOL web_search {{"query": "<what to search>", "max_results": 5}}
TOOL open_url {{"url": "<full https url>"}}
TOOL time_in {{"tz": "<IANA zone, e.g. Europe/Moscow>"}}

Rules:
- If the question does NOT need live data (writing code, explaining, translating),
  just answer directly — no tool call.
- Anything time-sensitive (weather, news, prices, "latest", "who is now") DOES
  need the tools: search, open a source, answer from what you actually read.
- "What time is it in <city>?" — use TOOL time_in with that city's IANA zone.
  Do NOT search for it and do NOT do the offset arithmetic yourself.
- Pages are fetched WITHOUT JavaScript. Live clocks/tickers come back as
  placeholders like 00:00:00 — if a page shows no real value, say so or try
  another source. Never fill the gap with a guessed number.
- Use web_search to find pages, then open_url to read the promising ones.
- Search in English when that gives better results, then answer in the user's language.
- For any specific fact (a date, number, version, name) open_url at least one
  source and confirm it there — do NOT answer from search snippets alone.
- After each tool result I send back, decide the next step.
- When you can answer, reply normally (no TOOL line) and cite the URLs you used.
- Today's facts may be newer than your training data; trust fresh tool results,
  not your memory. The current year is later than you might assume.
- NEVER invent a URL, source, name, date, or number. Only cite a URL you actually
  opened, and only state facts you actually saw in a tool result. If the searches
  and pages you opened do NOT contain the answer, say plainly that you could not
  verify it — do not guess."""

_client = httpx.Client(
    timeout=30.0,
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (local-web-agent)"},
)


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---- tools ---------------------------------------------------------------
def web_search(query, max_results=5):
    from ddgs import DDGS

    try:
        hits = DDGS().text(query, max_results=int(max_results))
    except Exception as e:
        return f"search error: {e}"
    if not hits:
        return "no results"
    out = []
    for i, h in enumerate(hits, 1):
        out.append(
            f"[{i}] {h.get('title', '')}\n{h.get('href', '')}\n{h.get('body', '')}"
        )
    return "\n\n".join(out)


class _TextExtractor(HTMLParser):
    _skip: ClassVar[set[str]] = {"script", "style", "head", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._skip:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._skip and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth == 0:
            t = data.strip()
            if t:
                self.parts.append(t)


def open_url(url, max_chars=4000):
    try:
        r = _client.get(url)
    except Exception as e:
        return f"fetch error: {e}"
    ctype = r.headers.get("content-type", "")
    text = r.text
    if "html" in ctype or text.lstrip().startswith("<"):
        p = _TextExtractor()
        with contextlib.suppress(Exception):
            p.feed(text)
        text = re.sub(r"\s+\n", "\n", "\n".join(p.parts))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) > int(max_chars):
        text = text[: int(max_chars)] + "\n…[truncated]"
    return f"URL: {url}  (HTTP {r.status_code})\n\n{text}"


def _find_zone(tz):
    """Resolve a sloppy zone name. Small models mistype these ('Asia/T Tokyo')
    and then repeat the same broken call forever, so be forgiving: exact match,
    then spaces->underscores, then match on the city part alone."""
    from zoneinfo import ZoneInfo, available_timezones

    try:
        return ZoneInfo(tz)
    except Exception:
        pass
    zones = available_timezones()

    def norm(s):
        return s.lower().replace(" ", "").replace("_", "")

    want = norm(tz)
    for z in zones:  # 'asia/t tokyo' -> 'Asia/Tokyo'
        if norm(z) == want:
            return ZoneInfo(z)
    city = norm(tz.split("/")[-1])
    by_city = {}
    for z in sorted(zones):
        by_city.setdefault(norm(z.split("/")[-1]), z)
    if city in by_city:
        return ZoneInfo(by_city[city])
    # last resort: typo-tolerant ('asia/t tokyo' -> city 'ttokyo' -> 'tokyo')
    near = difflib.get_close_matches(city, list(by_city), n=1, cutoff=0.8)
    return ZoneInfo(by_city[near[0]]) if near else None


def time_in(tz="UTC"):
    """Exact current time in a timezone. A tool, not mental math: the model got
    the CEST->MSK offset backwards, and live-clock sites are JS-rendered."""
    zone = _find_zone(tz)
    if zone is None:
        return (
            f"unknown timezone {tz!r} — use an IANA name like Europe/Moscow, "
            "America/New_York, Asia/Tokyo"
        )
    return datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S (%Z, UTC%z)")


TOOLS = {"web_search": web_search, "open_url": open_url, "time_in": time_in}
# Models often emit several TOOL lines at once despite being told not to, so match
# the FIRST one and let the JSON decoder find where its object ends. A greedy
# ".*}" here would swallow the following lines and produce invalid JSON — which
# silently cost us the arguments and sent the model into a retry loop.
TOOL_RE = re.compile(r"TOOL\s+(\w+)\s*(\{)")


def parse_tool(text):
    """-> (name, args_dict), or (None, None) when the reply is a final answer.

    A reply counts as a tool call only when it STARTS with TOOL. Same rule the
    streaming path uses — if the two disagreed we'd stream an answer and then
    treat it as a tool call, showing the user the reply twice."""
    if not text.lstrip().startswith("TOOL"):
        return None, None
    m = TOOL_RE.search(text)
    if not m:
        return None, None
    try:
        args, _ = json.JSONDecoder().raw_decode(text[m.start(2) :])
    except ValueError:
        args = {}
    return m.group(1), args if isinstance(args, dict) else {}


def strip_tool_tail(text):
    """Models like to append a stray 'TOOL ...' line after a finished answer.
    Cut it off so the user doesn't see the protocol leaking through."""
    m = re.search(r"\n\s*TOOL\s+\w+\s*\{", text)
    return text[: m.start()].rstrip() if m else text


# Streaming counterpart of strip_tool_tail. That regex can only match once the
# "{" has arrived — 17+ chars after the "\n" for 'TOOL web_search {' — so a
# fixed-size holdback forwards the first half of the marker before it can fire.
# Match instead any trailing fragment that could STILL grow into one.
_PARTIAL_TOOL_TAIL = re.compile(
    r"\n[ \t]*(?:T(?:O(?:O(?:L(?:[ \t]*\w*[ \t]*)?)?)?)?)?$"
)


def safe_tail_cut(text):
    """How much of a partly-generated reply is safe to show the user: everything
    before a trailing fragment that might turn out to be a TOOL marker. A false
    alarm (a line starting with 'T') costs one token of latency, not a leak."""
    m = _PARTIAL_TOOL_TAIL.search(text)
    return m.start() if m else len(text)


# ---- shared agent loop (used by the CLI AND the server-side web endpoint) --
def system_prompt():
    """SYSTEM with the real current date/time filled in — the model has no clock
    of its own, so without this it invents 'now' (and thus 'today'/'tomorrow')."""
    now = datetime.now().astimezone()
    return SYSTEM.format(now=now.strftime("%Y-%m-%d %H:%M"), tz=now.tzname() or "local")


def build_messages(history):
    """history: list of {role, content}. Drop client system msgs, prepend ours."""
    conv = [m for m in history if m.get("role") != "system"]
    return [{"role": "system", "content": system_prompt()}, *conv]


# Sent back once when the model searches and then answers without reading anything.
# Asked what changed in Python 3.13 it did exactly that, and asserted from memory
# that the version was unreleased, which was wrong. The prompt already tells it to
# open a source; this makes the loop insist rather than hope.
_READ_BEFORE_ANSWERING = (
    "You have not opened any source yet. Your search results are titles and "
    "snippets, not evidence, and your own memory may be out of date. Open the "
    "most relevant result with TOOL open_url and answer from what you read. If "
    "the pages do not contain the answer, say plainly that you could not verify it."
)


def run_loop(messages, chat_fn, on_progress=None, max_steps=MAX_STEPS):
    """Run the tool loop. `chat_fn(messages) -> assistant text`. Mutates messages.
    `on_progress(name, args)` is called before each tool runs (optional)."""
    last_call, repeats = None, 0
    searched = opened = insisted = False
    for _ in range(max_steps):
        reply = chat_fn(messages).strip()
        name, args = parse_tool(reply)
        if name is None:
            # A question that needed a search needs a source read too. Push back
            # once; a second refusal is let through so the run still terminates.
            if searched and not opened and not insisted:
                insisted = True
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": _READ_BEFORE_ANSWERING})
                continue
            return strip_tool_tail(reply)  # final answer
        # A weak model can repeat one identical (often broken) call until the step
        # budget is gone — minutes of nothing. Cut that off early.
        call = (name, json.dumps(args, sort_keys=True))
        repeats = repeats + 1 if call == last_call else 0
        last_call = call
        if repeats >= 2:
            return (
                "No answer: the model kept repeating the same "
                f"{name} call. Try rephrasing the question."
            )
        if on_progress:
            on_progress(name, args)
        fn = TOOLS.get(name)
        if not fn:
            result = f"unknown tool: {name} — use web_search, open_url or time_in"
        elif not args:
            result = (
                f"{name} got no arguments. Send the call as ONE line, e.g. "
                'TOOL web_search {"query": "your search"}'
            )
        else:
            try:
                result = fn(**args)
            except TypeError as e:
                result = f"wrong arguments for {name}: {e}"
            except Exception as e:
                result = f"tool error: {e}"
        # Only a result the model can actually read counts as having read one.
        if name == "web_search" and not result.startswith(
            ("search error:", "no results")
        ):
            searched = True
        elif name == "open_url" and not result.startswith("fetch error:"):
            opened = True
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{result}"})
    return "(stopped: reached MAX_STEPS without a final answer)"


# ---- CLI plumbing --------------------------------------------------------
def _headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = "Bearer " + API_KEY
    return h


def resolve_model():
    if MODEL_NAME:
        return MODEL_NAME
    try:
        r = _client.get(MODEL_URL + "/models", headers=_headers())
        return r.json()["data"][0]["id"]
    except Exception:
        return "local"


def run(question):
    model = resolve_model()

    def chat_fn(messages):
        r = _client.post(
            MODEL_URL + "/chat/completions",
            headers=_headers(),
            json={
                "model": model,
                "messages": messages,
                "temperature": TEMP,
                "max_tokens": 2048,
            },
            timeout=None,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return run_loop(
        build_messages([{"role": "user", "content": question}]),
        chat_fn,
        on_progress=lambda n, a: log(
            f"  ↪ {n}({a.get('query') or a.get('url') or ''})"
        ),
    )


def main():
    question = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not question:
        sys.exit('usage: web.sh "your question"')
    log(f"model: {MODEL_URL}")
    print(run(question))


if __name__ == "__main__":
    main()
