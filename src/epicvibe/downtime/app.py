"""Downtime Ordering capture UI + API.

    python -m epicvibe.downtime          # http://127.0.0.1:8200

Single-file FastAPI app with an inline HTML/vanilla-JS page - no build step and
no template engine, because a downtime tool has to start on whatever box is
available. Three panes: transcript in, filled template in the middle for the
clinician to review and sign, queue and recovery worklist on the right.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from epicvibe.downtime import hl7
from epicvibe.downtime.config import DowntimeSettings
from epicvibe.downtime.engine import DowntimeEngine
from epicvibe.downtime.mllp import STUCK_AFTER_SECONDS, submit_batch
from epicvibe.downtime.providers import build_provider
from epicvibe.downtime.store import DowntimeStore
from epicvibe.downtime.templates import load_templates


class GenerateRequest(BaseModel):
    transcript: str
    template_id: str | None = None


class SaveDraftRequest(BaseModel):
    template_id: str
    patient_fields: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    transcript: str = ""
    order_id: str | None = None


class SignRequest(BaseModel):
    signed_by: str = "downtime clinician"


class SubmitBatchRequest(BaseModel):
    limit: int = 25


class EhrStatusRequest(BaseModel):
    online: bool


def create_app(settings: DowntimeSettings | None = None) -> FastAPI:
    settings = settings or DowntimeSettings()
    library = load_templates(settings.templates_dir)
    store = DowntimeStore(settings.db_path)
    engine = DowntimeEngine(library, build_provider(settings))

    app = FastAPI(title="EpicVibe Downtime Ordering", version="0.1.0")
    app.state.settings = settings
    app.state.library = library
    app.state.store = store
    app.state.engine = engine
    # Server-side, so every browser tab (and the API) agrees on whether the EHR is
    # back. The downtime default is "down" - that is why this app is running.
    app.state.ehr_online = False
    # A previous process may have died mid-send; unstick those rows at startup.
    recovered = store.recover_stuck(STUCK_AFTER_SECONDS)
    if recovered:
        app.state.recovered_at_startup = recovered

    # -- page ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    # -- status / reference data -------------------------------------------

    @app.get("/api/status")
    async def status() -> dict:
        return {
            "provider": settings.provider,
            "model": settings.model if settings.provider == "anthropic" else "keyword-fake",
            "engine": f"{settings.engine_host}:{settings.engine_port}",
            "db_path": str(settings.db_path),
            "templates": len(library),
            "counts": store.counts(),
            "ehr_online": app.state.ehr_online,
            "allow_phi_to_model": settings.allow_phi_to_model,
        }

    @app.post("/api/ehr-status")
    async def set_ehr_status(req: EhrStatusRequest) -> dict:
        app.state.ehr_online = bool(req.online)
        return {"ehr_online": app.state.ehr_online}

    @app.get("/api/ehr-status")
    async def get_ehr_status() -> dict:
        return {"ehr_online": app.state.ehr_online}

    @app.get("/api/templates")
    async def templates() -> list[dict]:
        return library.summaries()

    @app.get("/api/templates/{template_id}")
    async def template(template_id: str) -> dict:
        t = library.get(template_id)
        if t is None:
            raise HTTPException(404, f"unknown template {template_id}")
        return t.model_dump()

    @app.get("/api/transcripts")
    async def transcripts() -> list[dict]:
        d = Path(settings.transcripts_dir)
        if not d.is_dir():
            return []
        return [{"name": p.stem, "text": p.read_text(encoding="utf-8")}
                for p in sorted(d.glob("*.txt"))]

    # -- extraction ---------------------------------------------------------

    @app.post("/api/generate")
    async def generate(req: GenerateRequest) -> dict:
        if not req.transcript.strip():
            raise HTTPException(400, "transcript is empty")
        try:
            result = await engine.generate(req.transcript, template_id=req.template_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        template = library.get(result.filled.template_id)
        return {
            "selection": result.selection.model_dump(),
            "filled": result.filled.model_dump(),
            "template": template.model_dump() if template else None,
        }

    # -- order store --------------------------------------------------------

    @app.post("/api/orders")
    async def save_draft(req: SaveDraftRequest) -> dict:
        if library.get(req.template_id) is None:
            raise HTTPException(400, f"unknown template {req.template_id}")
        if req.order_id:
            try:
                row = store.update_draft(
                    req.order_id, patient_fields=req.patient_fields, orders=req.orders,
                    template_id=req.template_id, transcript=req.transcript,
                )
            except KeyError as exc:
                raise HTTPException(404, f"unknown order {req.order_id}") from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return row
        order_id = store.create_draft(
            req.template_id, req.patient_fields, req.orders, req.transcript
        )
        return store.get(order_id)  # type: ignore[return-value]

    @app.get("/api/orders")
    async def list_orders(status: str | None = None) -> list[dict]:
        return store.list(status=status)

    @app.get("/api/orders/{order_id}")
    async def get_order(order_id: str) -> dict:
        row = store.get(order_id)
        if row is None:
            raise HTTPException(404, f"unknown order {order_id}")
        return row

    @app.post("/api/orders/{order_id}/sign")
    async def sign(order_id: str, req: SignRequest) -> dict:
        try:
            return store.sign(order_id, req.signed_by)
        except KeyError as exc:
            raise HTTPException(404, f"unknown order {order_id}") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/orders/{order_id}/reconcile")
    async def reconcile(order_id: str, payload: dict | None = None) -> dict:
        try:
            return store.mark_reconciled(order_id, (payload or {}).get("mrn"))
        except KeyError as exc:
            raise HTTPException(404, f"unknown order {order_id}") from exc

    @app.get("/api/orders/{order_id}/hl7", response_class=PlainTextResponse)
    async def preview_hl7(order_id: str) -> str:
        row = store.get(order_id)
        if row is None:
            raise HTTPException(404, f"unknown order {order_id}")
        template = library.get(row["template_id"])
        if template is None:
            raise HTTPException(409, f"unknown template {row['template_id']}")
        message, _control = hl7.build_orm(row, template, settings)
        return hl7.pretty(message)

    @app.get("/api/orders/{order_id}/log")
    async def order_log(order_id: str) -> list[dict]:
        if store.get(order_id) is None:
            raise HTTPException(404, f"unknown order {order_id}")
        return [{**e, "message": hl7.pretty(e["message"])} for e in store.hl7_log(order_id)]

    # -- write-back ---------------------------------------------------------

    @app.post("/api/submit-batch")
    async def submit(req: SubmitBatchRequest) -> dict:
        if not app.state.ehr_online:
            raise HTTPException(
                409, "the EHR is still marked OFFLINE -- POST /api/ehr-status "
                     "{\"online\": true} once write-back is available")
        return await submit_batch(store, library, settings, limit=req.limit)

    @app.get("/api/recovery")
    async def recovery() -> list[dict]:
        """Printable recovery worklist: what did NOT make it into Epic."""
        rows = store.recovery_worklist()
        out = []
        for row in rows:
            template = library.get(row["template_id"])
            patient = hl7.field_values(row["patient_fields"])
            selected = []
            for o in row["orders"]:
                if not o.get("selected"):
                    continue
                spec = template.order(o["order_id"]) if template else None
                values = hl7.field_values(o.get("fields", []))
                selected.append({
                    "order_id": o["order_id"],
                    "display": spec.display if spec else o["order_id"],
                    "category": spec.category if spec else "",
                    "detail": " ".join(
                        f"{k}={v}" for k, v in values.items() if v
                    ),
                })
            out.append({
                "id": row["id"], "status": row["status"], "ack_code": row["ack_code"],
                "ack_text": row["ack_text"], "last_error": row["last_error"],
                "attempts": row["attempts"], "signed_by": row["signed_by"],
                "signed_at": row["signed_at"],
                "template": template.name if template else row["template_id"],
                "patient": patient, "orders": selected,
            })
        return out

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError) -> JSONResponse:  # pragma: no cover
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Downtime Ordering</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--line:#d7dbe0;--ink:#1b1f24;--muted:#6b7280;
      --red:#b3261e;--redbg:#fdecea;--green:#1e7d44;--amber:#9a6700;--blue:#1d4ed8}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
     background:var(--bg);color:var(--ink)}
#banner{padding:10px 16px;font-weight:700;letter-spacing:.04em;color:#fff;background:var(--red);
        display:flex;align-items:center;gap:14px}
#banner.online{background:var(--green)}
#banner button{font:inherit;font-weight:600;padding:4px 10px;border-radius:4px;border:1px solid #fff6;
               background:#ffffff22;color:#fff;cursor:pointer}
#meta{margin-left:auto;font-weight:400;font-size:12px;opacity:.9}
main{display:grid;grid-template-columns:300px 1fr 340px;gap:12px;padding:12px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;
       max-height:calc(100vh - 92px);overflow:auto}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px}
h3{font-size:13px;margin:14px 0 6px}
textarea{width:100%;height:340px;font:12px/1.4 ui-monospace,Consolas,monospace;padding:8px;
         border:1px solid var(--line);border-radius:4px;resize:vertical}
select,input{font:inherit;padding:5px 6px;border:1px solid var(--line);border-radius:4px;
             background:#fff;max-width:100%}
button{font:inherit;padding:6px 12px;border-radius:4px;border:1px solid var(--line);
       background:#fff;cursor:pointer}
button.primary{background:var(--blue);color:#fff;border-color:var(--blue)}
button:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:6px 0}
.field{display:grid;grid-template-columns:150px 1fr;gap:8px;align-items:center;
       padding:4px 0;border-bottom:1px solid #f0f1f3}
.field label{color:var(--muted)}
.field input{width:100%}
.field.missing input{border-color:var(--red);background:var(--redbg)}
.req{color:var(--red)}
.chip{display:inline-block;font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--line);
      background:#eef2ff;color:#3730a3;cursor:help;margin-left:4px;max-width:220px;overflow:hidden;
      text-overflow:ellipsis;white-space:nowrap;vertical-align:middle}
.chip.high{background:#e7f6ec;color:#14532d}.chip.medium{background:#fef6e0;color:#7c4a03}
.chip.low{background:#f1f2f4;color:#4b5563}
.chip.none{background:#fdecea;color:var(--red)}
.order{border:1px solid var(--line);border-radius:5px;padding:8px;margin:6px 0;background:#fcfcfd}
.order.on{border-color:var(--blue);background:#f7f9ff}
.order .hdr{display:flex;gap:8px;align-items:flex-start}
.order .rat{color:var(--muted);font-size:12px}
.order .flds{display:none;margin-top:6px}
.order.on .flds{display:block}
.badge{font-size:11px;padding:1px 7px;border-radius:9px;border:1px solid var(--line)}
.badge.draft{background:#f1f2f4}.badge.queued{background:#e8eefc;color:#1e3a8a}
.badge.acked{background:#e7f6ec;color:#14532d}.badge.nacked{background:#fdecea;color:var(--red)}
.badge.failed{background:#fdecea;color:var(--red)}.badge.sending{background:#fef6e0;color:#7c4a03}
.badge.reconciled{background:#ede9fe;color:#4c1d95}
.qrow{border-bottom:1px solid #f0f1f3;padding:6px 0;font-size:13px}
.qrow .id{font:11px ui-monospace,Consolas,monospace;color:var(--muted)}
pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:5px;overflow:auto;font-size:11.5px;
    max-height:340px}
.warn{background:#fef6e0;border:1px solid #f3d38a;border-radius:4px;padding:6px 8px;font-size:12px;
      margin:6px 0}
.err{background:var(--redbg);border:1px solid #f3b6b0;color:var(--red);border-radius:4px;
     padding:6px 8px;font-size:12px;margin:6px 0}
.muted{color:var(--muted)}
#errBar{display:none;margin:8px 12px 0;padding:8px 12px;border-radius:5px;
        background:var(--redbg);border:1px solid #f3b6b0;color:var(--red);font-weight:600;
        align-items:center;gap:10px}
#errBar.show{display:flex}
#errBar button{margin-left:auto;font-size:12px;padding:2px 8px}
.cat{margin-top:10px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
</style></head><body>

<div id="banner"><span id="bannerText">EHR OFFLINE — downtime mode</span>
  <button id="toggleMode">Mark EHR ONLINE</button>
  <span id="meta"></span></div>

<div id="errBar"><span id="errText"></span><button id="errDismiss">Dismiss</button></div>

<main>
  <section class="panel">
    <h2>Ambient transcript</h2>
    <div class="row">
      <select id="sample"><option value="">Load sample…</option></select>
    </div>
    <textarea id="transcript" placeholder="Paste or dictate the encounter transcript…"></textarea>
    <div class="row">
      <button class="primary" id="generate">Generate orders</button>
      <span id="genStatus" class="muted"></span>
    </div>
    <div id="selection"></div>
  </section>

  <section class="panel">
    <h2>Captured order set</h2>
    <div id="empty" class="muted">Generate orders from a transcript to begin.</div>
    <div id="form" hidden>
      <div class="row">
        <label class="muted">Template</label>
        <select id="switchTemplate"></select>
        <span id="orderIdLabel" class="muted"></span>
      </div>
      <div id="warnings"></div>
      <h3>Patient</h3>
      <div id="patientFields"></div>
      <h3>Orders</h3>
      <div id="orders"></div>
      <div class="row" style="margin-top:12px">
        <button id="saveDraft">Save draft</button>
        <button class="primary" id="sign">Sign</button>
        <input id="signedBy" value="Dr. Downtime" style="width:150px">
        <button id="previewHl7">Preview HL7</button>
      </div>
      <div id="hl7"></div>
    </div>
  </section>

  <section class="panel">
    <h2>Queue</h2>
    <div class="row">
      <button class="primary" id="submitBatch" disabled>Submit batch to EHR</button>
      <button id="refresh">Refresh</button>
    </div>
    <div id="submitResult"></div>
    <div id="queue"></div>
    <h2 style="margin-top:16px">Recovery worklist</h2>
    <div id="recovery" class="muted">Nothing to recover.</div>
    <div id="log"></div>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
let online = false, state = null, currentOrderId = null;

const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
};
const post = (url, body) => api(url, {method:"POST", headers:{"content-type":"application/json"},
                                      body: JSON.stringify(body || {})});
const esc = s => (s == null ? "" : String(s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])));

function showError(msg) {
  $("#errText").textContent = String(msg || "Something went wrong.");
  $("#errBar").classList.add("show");
}
function clearError() { $("#errBar").classList.remove("show"); }
/* Nothing in a downtime tool is allowed to fail quietly. */
const guard = fn => (...args) => Promise.resolve().then(() => fn(...args))
  .catch(e => showError(e && e.message ? e.message : String(e)));

/* state helpers: the filled order set is keyed by id, never by render position -
   the template's order list and the filled list are not the same sequence. */
function orderById(orderId) {
  let o = state.filled.orders.find(x => x.order_id === orderId);
  if (!o) { o = {order_id: orderId, selected: false, rationale: "", fields: []};
            state.filled.orders.push(o); }
  return o;
}
function setFieldValue(list, fieldId, value) {
  let f = list.find(x => x.field_id === fieldId);
  if (!f) { f = {field_id: fieldId, value: null, evidence: null, confidence: "low"};
            list.push(f); }
  f.value = value;
}

function renderMode(next) {
  online = next;
  $("#banner").classList.toggle("online", online);
  $("#bannerText").textContent = online ? "EHR ONLINE — write-back enabled"
                                        : "EHR OFFLINE — downtime mode";
  $("#toggleMode").textContent = online ? "Mark EHR OFFLINE" : "Mark EHR ONLINE";
  $("#submitBatch").disabled = !online;
}

/* The flag lives on the server: submit-batch is refused with 409 while offline,
   so a browser-local toggle would only lie to the clinician. */
async function setMode(next) {
  const r = await post("/api/ehr-status", {online: !!next});
  renderMode(r.ehr_online);
}

function metaText(status) {
  const model = status.provider === "anthropic" && status.allow_phi_to_model
    ? `model: ${status.model} (transcript sent to hosted model)`
    : `model ${status.model}`;
  return `provider ${status.provider} · ${model} · engine ${status.engine}`;
}

async function boot() {
  const [status, samples] = await Promise.all([api("/api/status"), api("/api/transcripts")]);
  renderMode(status.ehr_online);
  $("#meta").textContent = `${metaText(status)} · ${status.templates} templates`;
  const sel = $("#sample");
  samples.forEach(s => { const o = document.createElement("option");
    o.value = s.name; o.textContent = s.name; o.dataset.text = s.text; sel.appendChild(o); });
  sel.onchange = () => { const o = sel.selectedOptions[0];
    if (o && o.dataset.text) $("#transcript").value = o.dataset.text; };
  await refreshQueue();
}

async function generate(templateId) {
  const transcript = $("#transcript").value.trim();
  if (!transcript) { $("#genStatus").textContent = "transcript is empty"; return; }
  $("#genStatus").textContent = "extracting…";
  try {
    state = await post("/api/generate", {transcript, template_id: templateId || null});
    currentOrderId = null;
    render();
    $("#genStatus").textContent = "done";
  } catch (e) { $("#genStatus").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

function chip(f) {
  if (f.evidence) return `<span class="chip ${esc(f.confidence)}" title="${esc(f.evidence)}">“${esc(f.evidence.slice(0,60))}”</span>`;
  if (f.value) return `<span class="chip low" title="from template default — no transcript evidence">no quote · ${esc(f.confidence)}</span>`;
  return `<span class="chip none" title="not stated in the transcript">not in transcript</span>`;
}

function fieldRow(spec, filled, scope, orderId) {
  const missing = spec.required && !filled.value;
  return `<div class="field ${missing ? "missing" : ""}">
    <label>${esc(spec.label)}${spec.required ? ' <span class="req">*</span>' : ""}</label>
    <div><input data-scope="${esc(scope)}" data-oid="${esc(orderId || "")}"
      data-fid="${esc(spec.field_id)}" value="${esc(filled.value || "")}"
      placeholder="${esc(spec.hint || "")}">${chip(filled)}</div></div>`;
}

function render() {
  $("#empty").hidden = true; $("#form").hidden = false;
  const {selection, filled, template} = state;
  $("#selection").innerHTML = `<div class="warn"><b>${esc(template.name)}</b>
    — confidence ${esc(selection.confidence)}<br><span class="muted">${esc(selection.rationale)}</span></div>`;

  const sw = $("#switchTemplate"); sw.innerHTML = "";
  const ids = [template.template_id, ...selection.alternatives.map(a => a.template_id)];
  api("/api/templates").catch(e => { showError(e.message); return []; }).then(all => {
    sw.innerHTML = all.map(t => `<option value="${esc(t.template_id)}"
      ${t.template_id === template.template_id ? "selected" : ""}>${esc(t.name)}${
        ids.includes(t.template_id) && t.template_id !== template.template_id ? " (alternative)" : ""
      }</option>`).join("");
  });
  sw.onchange = guard(() => generate(sw.value));

  $("#orderIdLabel").textContent = currentOrderId ? `draft ${currentOrderId.slice(0,8)}` : "unsaved";
  const notes = [];
  if (filled.unresolved.length) notes.push(`<div class="err"><b>Unresolved (${filled.unresolved.length}):</b> ${esc(filled.unresolved.join(", "))}</div>`);
  filled.warnings.forEach(w => notes.push(`<div class="warn">${esc(w)}</div>`));
  $("#warnings").innerHTML = notes.join("");

  const pf = new Map(filled.patient_fields.map(f => [f.field_id, f]));
  $("#patientFields").innerHTML = template.patient_fields.map(spec =>
    fieldRow(spec, pf.get(spec.field_id) || {}, "patient")).join("");

  const fo = new Map(filled.orders.map(o => [o.order_id, o]));
  let html = "", lastCat = null;
  template.orders.forEach(spec => {
    const o = fo.get(spec.order_id) || {selected:false, fields:[], rationale:""};
    if (spec.category !== lastCat) { html += `<div class="cat">${esc(spec.category)}</div>`; lastCat = spec.category; }
    const ff = new Map((o.fields||[]).map(f => [f.field_id, f]));
    html += `<div class="order ${o.selected ? "on" : ""}" data-oid="${esc(spec.order_id)}">
      <div class="hdr"><input type="checkbox" data-order="${esc(spec.order_id)}"
          ${o.selected ? "checked" : ""}>
        <div><b>${esc(spec.display)}</b>
          <div class="rat">${esc(spec.code)} · ${esc(o.rationale || "")}</div></div></div>
      <div class="flds">${spec.fields.map(fs =>
          fieldRow(fs, ff.get(fs.field_id) || {}, "order", spec.order_id)).join("")}</div></div>`;
  });
  $("#orders").innerHTML = html;

  $("#orders").querySelectorAll("input[type=checkbox]").forEach(cb => cb.onchange = () => {
    orderById(cb.dataset.order).selected = cb.checked;
    cb.closest(".order").classList.toggle("on", cb.checked);
  });
  document.querySelectorAll("input[data-scope]").forEach(inp => inp.oninput = () => {
    const value = inp.value || null;
    if (inp.dataset.scope === "patient") {
      setFieldValue(state.filled.patient_fields, inp.dataset.fid, value);
    } else {
      setFieldValue(orderById(inp.dataset.oid).fields, inp.dataset.fid, value);
    }
  });
  $("#hl7").innerHTML = "";
}

async function saveDraft() {
  if (!state) return;
  const row = await post("/api/orders", {
    template_id: state.filled.template_id,
    patient_fields: state.filled.patient_fields,
    orders: state.filled.orders,
    transcript: $("#transcript").value,
    order_id: currentOrderId,
  });
  currentOrderId = row.id;
  $("#orderIdLabel").textContent = `draft ${row.id.slice(0,8)}`;
  await refreshQueue();
  return row;
}

async function sign() {
  if (!currentOrderId) await saveDraft();
  const row = await post(`/api/orders/${currentOrderId}/sign`, {signed_by: $("#signedBy").value});
  $("#orderIdLabel").innerHTML = `<span class="badge ${row.status}">${row.status}</span> ${row.id.slice(0,8)}`;
  await refreshQueue();
}

async function refreshQueue() {
  const [orders, recovery, status] = await Promise.all(
    [api("/api/orders"), api("/api/recovery"), api("/api/status")]);
  renderMode(status.ehr_online);
  $("#meta").textContent = `${metaText(status)} · ` +
    Object.entries(status.counts).map(([k,v]) => `${k}:${v}`).join(" ");
  $("#queue").innerHTML = orders.length ? orders.map(o => `<div class="qrow">
      <span class="badge ${esc(o.status)}">${esc(o.status)}</span>
      <b>${esc(o.template_id)}</b>
      <div class="id">${esc(o.id)}${o.ack_code ? " · ACK " + esc(o.ack_code) : ""}${
        o.last_error ? " · " + esc(o.last_error) : ""}</div>
      <button data-log="${esc(o.id)}">HL7 log</button>
      <button data-prev="${esc(o.id)}">Preview</button>
    </div>`).join("") : `<div class="muted">Queue is empty.</div>`;
  $("#queue").querySelectorAll("[data-log]").forEach(b => b.onclick = guard(async () => {
    const entries = await api(`/api/orders/${b.dataset.log}/log`);
    $("#log").innerHTML = entries.length
      ? entries.map(e => `<div class="cat">${esc(e.direction)} · ${esc(e.ts)}</div><pre>${esc(e.message)}</pre>`).join("")
      : `<div class="muted">No HL7 traffic yet.</div>`;
  }));
  $("#queue").querySelectorAll("[data-prev]").forEach(b => b.onclick = guard(async () => {
    $("#log").innerHTML = `<pre>${esc(await api(`/api/orders/${b.dataset.prev}/hl7`))}</pre>`;
  }));
  $("#recovery").innerHTML = recovery.length ? recovery.map(r => `<div class="qrow">
      <span class="badge ${esc(r.status)}">${esc(r.status)}</span> <b>${esc(r.template)}</b>
      — ${esc(r.patient.patient_name || "unidentified")} (${esc(r.patient.dob || "no DOB")})
      <div class="id">${esc(r.ack_text || r.last_error || "")} · attempts ${r.attempts}</div>
      <ol>${r.orders.map(o => `<li>${esc(o.display)} <span class="muted">${esc(o.detail)}</span></li>`).join("")}</ol>
    </div>`).join("") : `<div class="muted">Nothing to recover.</div>`;
}

$("#errDismiss").onclick = clearError;
$("#toggleMode").onclick = guard(() => setMode(!online));
$("#generate").onclick = guard(() => generate(null));
$("#saveDraft").onclick = guard(saveDraft);
$("#sign").onclick = guard(sign);
$("#refresh").onclick = guard(refreshQueue);
$("#previewHl7").onclick = guard(async () => {
  if (!currentOrderId) await saveDraft();
  $("#hl7").innerHTML = `<pre>${esc(await api(`/api/orders/${currentOrderId}/hl7`))}</pre>`;
});
$("#submitBatch").onclick = guard(async () => {
  $("#submitResult").innerHTML = `<div class="muted">submitting…</div>`;
  try {
    const r = await post("/api/submit-batch", {limit: 25});
    clearError();
    $("#submitResult").innerHTML = `<div class="warn">Attempted ${r.attempted} — ` +
      Object.entries(r.tally).map(([k,v]) => `${esc(k)}: ${v}`).join(", ") + `</div>`;
  } catch (e) {
    $("#submitResult").innerHTML = `<div class="err">${esc(e.message)}</div>`;
    showError(e.message);
  }
  await refreshQueue();
});

guard(boot)();
</script></body></html>
"""


app = None  # created lazily by __main__ so importing this module is side-effect free
