"""Dependency-free reviewer UI served by the existing FastAPI process."""

from __future__ import annotations

from html import escape

RESEARCH_UI_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>Research Agent</title>
  <style>
    :root {
      color-scheme: light;
      --paper: #f5f7f6;
      --surface: #ffffff;
      --ink: #16211f;
      --muted: #64716e;
      --line: #dce3e1;
      --accent: #007f78;
      --accent-dark: #005f5a;
      --danger: #a13b35;
      --shadow: 0 18px 48px rgba(20, 54, 49, .08);
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; margin: 0; }
    body {
      background: var(--paper);
      color: var(--ink);
      font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      align-items: center;
      background: rgba(255, 255, 255, .92);
      border-bottom: 1px solid var(--line);
      display: flex;
      gap: 16px;
      min-height: 72px;
      padding: 12px clamp(18px, 4vw, 48px);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .brand { display: grid; gap: 2px; min-width: 170px; }
    .brand strong { font-size: 17px; letter-spacing: -.02em; }
    .brand span { color: var(--muted); font-size: 12px; }
    .connection { align-items: center; color: var(--muted); display: flex; gap: 8px; }
    .dot { background: #d1a12b; border-radius: 50%; height: 8px; width: 8px; }
    .dot.ready { background: #27845d; }
    .spacer { flex: 1; }
    .credentials { align-items: center; display: flex; gap: 8px; }
    input, textarea, button { font: inherit; }
    input, textarea {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 10px;
      color: var(--ink);
      outline: none;
    }
    input:focus, textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(0, 127, 120, .11);
    }
    #api-key { min-width: 220px; padding: 9px 11px; }
    button {
      background: var(--accent);
      border: 0;
      border-radius: 10px;
      color: white;
      cursor: pointer;
      font-weight: 650;
      min-height: 42px;
      padding: 0 18px;
      transition: background 140ms ease, opacity 140ms ease;
    }
    button:hover { background: var(--accent-dark); }
    button:disabled { cursor: default; opacity: .5; }
    button.secondary {
      background: transparent;
      border: 1px solid var(--line);
      color: var(--ink);
    }
    main {
      display: grid;
      gap: 20px;
      margin: 0 auto;
      max-width: 980px;
      min-height: calc(100vh - 72px);
      padding: clamp(28px, 5vw, 64px) clamp(18px, 4vw, 48px) 36px;
      grid-template-rows: auto 1fr auto;
    }
    .intro { max-width: 680px; }
    h1 { font-size: clamp(30px, 5vw, 52px); letter-spacing: -.045em; line-height: 1.04; margin: 0 0 12px; }
    .intro p { color: var(--muted); font-size: 16px; margin: 0; }
    #conversation { display: grid; gap: 22px; align-content: start; padding: 8px 0; }
    .message { display: grid; gap: 7px; }
    .message .role { color: var(--muted); font-size: 12px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
    .message .content { max-width: 800px; overflow-wrap: anywhere; white-space: pre-wrap; }
    .message.user .content {
      background: #e8efed;
      border-radius: 12px;
      justify-self: start;
      padding: 11px 14px;
    }
    .citations { display: grid; gap: 6px; margin-top: 10px; }
    .citations a { color: var(--accent-dark); overflow-wrap: anywhere; }
    .memory-note {
      color: var(--accent-dark);
      font-size: 13px;
      font-weight: 650;
      margin-top: 9px;
    }
    .status { color: var(--muted); min-height: 24px; }
    .status.error { color: var(--danger); }
    .composer {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      display: grid;
      gap: 12px;
      padding: 14px;
    }
    textarea { min-height: 96px; padding: 13px 14px; resize: vertical; width: 100%; }
    .composer-row { align-items: center; display: flex; gap: 12px; }
    .composer-row small { color: var(--muted); flex: 1; }
    @media (max-width: 720px) {
      header { align-items: stretch; flex-wrap: wrap; }
      .connection { order: 3; width: 100%; }
      .credentials { width: 100%; }
      #api-key { flex: 1; min-width: 0; }
      main { min-height: calc(100vh - 128px); padding-top: 34px; }
      .composer-row { align-items: stretch; flex-direction: column; }
      .composer-row button { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand"><strong>Research Agent</strong><span>Local reviewer workspace</span></div>
    <div class="connection"><span class="dot" id="health-dot"></span><span id="health">Checking service</span></div>
    <div class="spacer"></div>
    <div class="credentials">
      <input id="api-key" type="password" autocomplete="off" spellcheck="false" placeholder="Paste local API key"__API_KEY_VALUE__>
      <button class="secondary" id="new-session" type="button">New session</button>
    </div>
  </header>
  <main>
    <section class="intro">
      <h1>Ask. Search. Verify.</h1>
      <p>The same bounded agent used by the worker searches public sources, validates evidence, and returns inspectable citations.</p>
    </section>
    <section id="conversation" aria-live="polite"></section>
    <section class="composer">
      <textarea id="query" maxlength="400" placeholder="Ask a research question…" aria-label="Research question"></textarea>
      <div class="composer-row">
        <small id="status" class="status">__API_KEY_NOTICE__</small>
        <button id="send" type="button">Run research</button>
      </div>
    </section>
  </main>
  <script>
    const byId = (id) => document.getElementById(id);
    const keyInput = byId("api-key");
    const queryInput = byId("query");
    const sendButton = byId("send");
    const statusLine = byId("status");
    const conversation = byId("conversation");
    const terminalStates = new Set(["completed", "failed", "cancelled", "expired"]);
    let sessionId = null;

    function setStatus(message, error = false) {
      statusLine.textContent = message;
      statusLine.classList.toggle("error", error);
    }

    function authHeaders(json = false) {
      const key = keyInput.value.trim();
      if (!key) throw new Error("Paste a local API key first.");
      const headers = { "Authorization": "Bearer " + key };
      if (json) headers["Content-Type"] = "application/json";
      return headers;
    }

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      let payload = null;
      try { payload = await response.json(); } catch (_) { payload = null; }
      if (!response.ok) {
        const message = payload?.detail?.message || payload?.detail?.code || "Request failed with HTTP " + response.status;
        throw new Error(message);
      }
      return payload;
    }

    function addMessage(role, text, className) {
      const wrapper = document.createElement("article");
      wrapper.className = "message " + className;
      const label = document.createElement("div");
      label.className = "role";
      label.textContent = role;
      const content = document.createElement("div");
      content.className = "content";
      content.textContent = text;
      wrapper.append(label, content);
      conversation.appendChild(wrapper);
      wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
      return wrapper;
    }

    function renderAnswer(run) {
      const answer = run.answer;
      const text = answer?.answer_text || run.failure?.message || "The run ended without a public answer.";
      const wrapper = addMessage("Agent", text, "agent");
      if (run.memory_used === true) {
        const memoryNote = document.createElement("div");
        memoryNote.className = "memory-note";
        memoryNote.textContent = "Reviewed memory was used during this run.";
        wrapper.appendChild(memoryNote);
      }
      if (!answer) return;
      if (!answer.citations?.length) return;
      const citations = document.createElement("div");
      citations.className = "citations";
      for (const [index, citation] of answer.citations.entries()) {
        const link = document.createElement("a");
        link.textContent = "Source " + (index + 1) + ": " + citation.claim;
        link.href = citation.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        citations.appendChild(link);
      }
      wrapper.appendChild(citations);
    }

    function idempotencyKey() {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    }

    async function ensureSession() {
      if (sessionId) return sessionId;
      const session = await api("/v1/sessions", {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify({ label: "Local research" }),
      });
      sessionId = session.session_id;
      return sessionId;
    }

    async function waitForRun(runId) {
      for (let attempt = 0; attempt < 180; attempt += 1) {
        const run = await api("/v1/runs/" + encodeURIComponent(runId), { headers: authHeaders() });
        setStatus("Run " + run.state.replaceAll("_", " ") + "…");
        if (terminalStates.has(run.state)) return run;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      throw new Error("The run did not finish within three minutes.");
    }

    async function submit() {
      const query = queryInput.value.trim();
      if (query.length < 3) { setStatus("Enter at least three characters.", true); return; }
      sendButton.disabled = true;
      queryInput.disabled = true;
      addMessage("You", query, "user");
      queryInput.value = "";
      try {
        setStatus("Creating or reusing the local session…");
        const activeSession = await ensureSession();
        const accepted = await api("/v1/sessions/" + encodeURIComponent(activeSession) + "/runs", {
          method: "POST",
          headers: { ...authHeaders(true), "Idempotency-Key": idempotencyKey() },
          body: JSON.stringify({ query }),
        });
        const run = await waitForRun(accepted.run_id);
        renderAnswer(run);
        setStatus(run.state === "completed" ? "Research complete." : "Run ended: " + run.state, run.state !== "completed");
      } catch (error) {
        setStatus(error instanceof Error ? error.message : "Unexpected request failure.", true);
      } finally {
        sendButton.disabled = false;
        queryInput.disabled = false;
        queryInput.focus();
      }
    }

    byId("new-session").addEventListener("click", () => {
      sessionId = null;
      conversation.replaceChildren();
      setStatus("New local session. The API key remains only in this tab.");
      queryInput.focus();
    });
    sendButton.addEventListener("click", submit);
    queryInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
    });
    fetch("/health/ready").then((response) => {
      const ready = response.ok;
      byId("health-dot").classList.toggle("ready", ready);
      byId("health").textContent = ready ? "Service ready" : "Service not ready";
    }).catch(() => { byId("health").textContent = "Service unavailable"; });
  </script>
</body>
</html>
"""

_UI_KEY_TEMPLATE = RESEARCH_UI_HTML
_TAB_ONLY_NOTICE = "The API key stays in this tab and is never stored by the page."
_PREFILLED_NOTICE = (
    "This local review key was filled in by the process that started the API."
)
RESEARCH_UI_HTML = _UI_KEY_TEMPLATE.replace("__API_KEY_VALUE__", "").replace(
    "__API_KEY_NOTICE__", _TAB_ONLY_NOTICE
)


def render_research_ui(prefilled_api_key: str | None = None) -> str:
    """Render the reviewer UI, optionally with a local review key filled in.

    Only a caller that already holds the key asks for this, and only for a
    loopback review session, so the page shows what the terminal already printed
    instead of asking a reviewer to paste it back.
    """
    if not prefilled_api_key:
        return RESEARCH_UI_HTML
    # The notice is substituted first: a key that happens to contain the notice
    # placeholder must reach the page unchanged.
    return _UI_KEY_TEMPLATE.replace("__API_KEY_NOTICE__", _PREFILLED_NOTICE).replace(
        "__API_KEY_VALUE__", f' value="{escape(prefilled_api_key, quote=True)}"'
    )


UI_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; frame-ancestors "
        "'none'; form-action 'none'; img-src 'none'; object-src 'none'; "
        "script-src 'unsafe-inline'; style-src 'unsafe-inline'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
