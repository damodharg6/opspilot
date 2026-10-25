// OpsPilot showcase player. Recorded runs come from data/*.json (real runs of the full
// stack). Served by `make showcase`, the same page can also start live runs.
// Tool output is untrusted data: it is only ever inserted as text, never as HTML.
"use strict";

const CATEGORY = {
  bad_deploy: "Bad deploy",
  db_latency: "Database latency",
  db_connection: "Database connection",
  business: "Business issue · not an outage",
  memory: "Memory",
  unknown: "Unknown",
};
const PHASES = ["reset", "warmup", "fault", "alarm", "investigating", "done"];
const PREVIEW_CHARS = 2500;

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") Object.assign(el.style, v); // CSSOM, allowed by the strict CSP
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const state = { demos: [], cache: new Map(), current: null, token: 0, live: false, liveRun: null };

// --- data -----------------------------------------------------------------------------------
async function getJSON(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

async function loadDemo(id) {
  if (!state.cache.has(id)) state.cache.set(id, await getJSON(`data/${id}.json`));
  return state.cache.get(id);
}

// --- rendering pieces ----------------------------------------------------------------------------
function setRail(phase) {
  const idx = PHASES.indexOf(phase);
  document.querySelectorAll("#rail li").forEach((li, i) => {
    li.classList.toggle("done", idx >= 0 && (i < idx || phase === "done"));
    li.classList.toggle("active", i === idx && phase !== "done");
  });
}

function trafficGroup(label, summary) {
  if (!summary || !summary.requests) return null;
  const counts = Object.entries(summary.status_counts || {});
  const max = Math.max(...counts.map(([, n]) => n), 1);
  return h(
    "div",
    { class: "traffic-group" },
    h("h5", {}, `${label} · ${summary.requests} req`),
    counts.map(([code, n]) =>
      h(
        "div",
        { class: "bar-row" },
        h("span", {}, code === "0" ? "err" : code),
        h("div", { class: "bar" }, h("i", { class: `s${String(code)[0]}`, style: { width: `${(100 * n) / max}%` } })),
        h("span", { class: "n" }, n),
      ),
    ),
    h("div", { class: "latency" }, `p50 ${summary.p50_ms} ms · p95 ${summary.p95_ms} ms`),
  );
}

function renderTraffic(record) {
  const box = $("#traffic");
  box.replaceChildren(
    trafficGroup("Healthy", record.traffic.warmup) || "",
    trafficGroup(`Fault: ${record.fault || "none"}`, record.traffic.fault) || "",
  );
}

function pretty(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function stepNode(step) {
  const body = pretty(step.result_preview || "");
  const shown = body.length > PREVIEW_CHARS ? `${body.slice(0, PREVIEW_CHARS)}\n… (${body.length - PREVIEW_CHARS} more chars)` : body;
  return h(
    "li",
    { class: `step${step.is_error ? " err" : ""}` },
    h(
      "div",
      { class: "step-head" },
      h("span", { class: "step-n" }, String(step.step).padStart(2, "0")),
      h("span", { class: "step-tool" }, step.tool),
      h("span", { class: "step-args" }, JSON.stringify(step.arguments)),
      h("span", { class: "step-ms" }, `${Math.round(step.duration_ms)} ms`),
    ),
    h("details", {}, h("summary", {}, "tool output"), h("pre", {}, shown)),
  );
}

function confidencePill(c) {
  return h("span", { class: `pill ${c === "high" ? "ok" : c === "medium" ? "warn" : "bad"}` }, `${c} confidence`);
}

function renderReport(record) {
  const box = $("#report");
  const inc = record.incident;
  const r = inc.report;
  if (!r) {
    box.replaceChildren(h("div", { class: "report-head" }, h("h4", {}, `Incident ${inc.status}`), h("span", { class: "pill bad" }, inc.last_error || "no report")));
    box.hidden = false;
    return;
  }
  const verdict = record.correct
    ? h("span", { class: "pill ok" }, "✓ matches ground truth")
    : h("span", { class: "pill bad" }, `expected ${CATEGORY[record.expected] || record.expected}`);
  const dropped = r.evidence_dropped
    ? h("p", { class: "grounding-note" }, `Grounding check dropped ${r.evidence_dropped} evidence snippet${r.evidence_dropped > 1 ? "s" : ""} not found in any tool output, and lowered confidence.`)
    : null;
  box.replaceChildren(
    h("div", { class: "report-head" }, h("h4", {}, CATEGORY[r.category] || r.category), confidencePill(r.confidence), verdict),
    h(
      "div",
      { class: "report-body" },
      h("div", { class: "wide" }, h("h5", {}, "Summary"), h("p", {}, r.summary)),
      h("div", {}, h("h5", {}, "Root cause"), h("p", {}, r.root_cause)),
      h("div", {}, h("h5", {}, "Suggested fix"), h("p", {}, r.suggested_fix)),
      h(
        "div",
        { class: "wide" },
        h("h5", {}, `Evidence · copied from tool output`),
        r.evidence.length
          ? h("ul", { class: "evidence" }, r.evidence.map((e) => h("li", {}, h("span", { class: "src" }, `${e.tool}${e.timestamp ? ` · ${e.timestamp}` : ""}`), h("pre", {}, e.snippet))))
          : h("p", { class: "muted" }, "No evidence survived grounding."),
        dropped,
      ),
      h(
        "div",
        { class: "wide stats" },
        h("span", {}, "model ", h("b", {}, r.model)),
        h("span", {}, "tool calls ", h("b", {}, r.tool_call_count)),
        h("span", {}, "tokens ", h("b", {}, (r.prompt_tokens + r.completion_tokens).toLocaleString())),
        h("span", {}, "agent time ", h("b", {}, `${r.latency_s} s`)),
        h("span", {}, "stop ", h("b", {}, r.stop_reason)),
        h("span", {}, "recorded ", h("b", {}, record.recorded_at.slice(0, 10))),
      ),
    ),
  );
  box.hidden = false;
}

function resetPlayer(record) {
  $("#p-tag").textContent = record ? record.tagline : "";
  $("#p-title").textContent = record ? record.title : "";
  $("#p-story").textContent = record ? record.story : "";
  $("#traffic").replaceChildren();
  $("#trace").replaceChildren(h("li", { class: "trace-empty" }, "Press Replay to watch the investigation."));
  $("#trace-meta").textContent = "";
  $("#report").hidden = true;
  setRail(null);
}

function showAll(record) {
  state.token++;
  renderTraffic(record);
  $("#trace").replaceChildren(...record.trace.map(stepNode));
  $("#trace-meta").textContent = `· ${record.trace.length} tool calls`;
  renderReport(record);
  setRail("done");
}

// --- replay ---------------------------------------------------------------------------------------
async function replay(record) {
  const token = ++state.token;
  const alive = () => token === state.token;
  const pace = reducedMotion ? 0 : 1;
  resetPlayer(record);
  $("#trace").replaceChildren();

  for (const phase of ["reset", "warmup"]) {
    setRail(phase);
    await sleep(500 * pace);
    if (!alive()) return;
  }
  $("#traffic").replaceChildren(trafficGroup("Healthy", record.traffic.warmup) || "");
  await sleep(700 * pace);
  setRail("fault");
  renderTraffic(record);
  await sleep(900 * pace);
  if (!alive()) return;
  setRail("alarm");
  await sleep(600 * pace);
  setRail("investigating");

  const trace = $("#trace");
  for (const step of record.trace) {
    const thinking = h("li", { class: "thinking" }, "model is choosing the next tool");
    trace.append(thinking);
    await sleep(900 * pace);
    if (!alive()) return;
    thinking.replaceWith(stepNode(step));
    $("#trace-meta").textContent = `· ${step.step} of ${record.trace.length}`;
  }
  const writing = h("li", { class: "thinking" }, "writing the report, then grounding check");
  trace.append(writing);
  await sleep(1100 * pace);
  if (!alive()) return;
  writing.remove();
  $("#trace-meta").textContent = `· ${record.trace.length} tool calls`;
  setRail("done");
  renderReport(record);
}

// --- demo list --------------------------------------------------------------------------------------
async function select(id) {
  state.token++;
  document.querySelectorAll(".demo-tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.id === id)));
  const record = await loadDemo(id);
  state.current = record;
  resetPlayer(record);
  $("#btn-play").disabled = false;
  $("#btn-skip").disabled = false;
  $("#btn-live").disabled = Boolean(state.liveRun);
}

function renderList() {
  $("#demo-list").replaceChildren(
    ...state.demos.map((d) =>
      h(
        "button",
        { class: "demo-tab", role: "tab", type: "button", "data-id": d.id, "aria-selected": "false", onclick: () => select(d.id) },
        h("strong", {}, d.title),
        h("span", {}, d.tagline),
        h("span", { class: `verdict ${d.correct ? "ok" : "miss"}` }, d.correct ? "✓ correct root cause" : "✗ missed"),
      ),
    ),
  );
}

// --- live mode (only when served by scripts/showcase_server.py) -------------------------------------
async function detectLive() {
  // Live runs only exist behind `make showcase` on your own machine; never probe from a host.
  if (!["localhost", "127.0.0.1"].includes(location.hostname)) return;
  try {
    const status = await getJSON("live/status");
    state.live = Boolean(status.live);
  } catch {
    state.live = false;
  }
  $("#btn-live").hidden = !state.live;
  $("#live-note").hidden = !state.live;
}

async function runLive() {
  if (!state.current) return;
  const log = $("#live-log");
  const line = (text, cls) => {
    log.append(h("div", { class: cls }, text));
    log.scrollTop = log.scrollHeight;
  };
  state.token++;
  resetPlayer(state.current);
  $("#trace").replaceChildren(h("li", { class: "trace-empty" }, "Live run in progress. This takes about 2–4 minutes."));
  log.hidden = false;
  log.replaceChildren();
  $("#btn-live").disabled = true;

  let run;
  try {
    run = await getJSON("live/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario: state.current.id }),
    });
  } catch (err) {
    line(`could not start: ${err.message}`, "err");
    $("#btn-live").disabled = false;
    return;
  }
  state.liveRun = run.id;
  let seen = 0;
  for (;;) {
    await sleep(1500);
    let status;
    try {
      status = await getJSON(`live/runs/${run.id}`);
    } catch (err) {
      line(`lost contact with the showcase server: ${err.message}`, "err");
      break;
    }
    for (const ev of status.events.slice(seen)) {
      line(`${ev.at.slice(11, 19)}  ${ev.phase.padEnd(13)} ${ev.detail}`);
      setRail(ev.phase);
    }
    seen = status.events.length;
    if (status.state === "done") {
      state.cache.set(status.record.id, status.record);
      state.current = status.record;
      await replay(status.record);
      break;
    }
    if (status.state === "error") {
      line(status.error, "err");
      break;
    }
  }
  state.liveRun = null;
  $("#btn-live").disabled = false;
}

// --- hero console --------------------------------------------------------------------------------------
async function heroTicker(record) {
  const out = $("#hero-ticker");
  const r = record && record.incident.report;
  const lines = [
    ["t-alarm", "ALARM  shop-health-degraded  window=2m"],
    ["t-dim", "POST /incidents → 202 queued"],
    ["t-dim", "worker claimed job (SKIP LOCKED, lease 300s)"],
    ...(record ? record.trace.map((s) => ["t-tool", `→ ${s.tool}(${JSON.stringify(s.arguments)})`]) : []),
    ["t-dim", "grounding check: evidence verified against tool output"],
    ["t-ok", r ? `REPORT ${r.category}  confidence=${r.confidence}` : "REPORT ready"],
    ["", r ? r.summary : ""],
  ];
  if (reducedMotion) {
    out.replaceChildren(...lines.map(([cls, text]) => h("span", { class: cls }, `${text}\n`)));
    return;
  }
  out.replaceChildren();
  for (const [cls, text] of lines) {
    const span = h("span", { class: cls });
    out.append(span, "\n");
    for (let i = 0; i < text.length; i += 3) {
      span.textContent = text.slice(0, i + 3);
      await sleep(12);
    }
    await sleep(260);
  }
}

// --- boot ------------------------------------------------------------------------------------------------
async function boot() {
  $("#btn-play").addEventListener("click", () => state.current && replay(state.current));
  $("#btn-skip").addEventListener("click", () => state.current && showAll(state.current));
  $("#btn-live").addEventListener("click", runLive);
  detectLive();
  try {
    state.demos = (await getJSON("data/index.json")).demos;
  } catch (err) {
    $("#p-title").textContent = "Recorded demos are missing";
    $("#p-story").textContent = `Run \`make record-demos\` with the stack up. (${err.message})`;
    heroTicker(null);
    return;
  }
  renderList();
  if (state.demos.length) {
    await select(state.demos[0].id);
    heroTicker(state.current);
  }
}

boot();
