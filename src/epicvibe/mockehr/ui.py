"""The mock EHR chart UI: one self-contained HTML page, no build step."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock EHR - Chart</title>
<style>
:root{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#1a2230; --muted:#5d6b7f; --line:#dde3ea;
  --accent:#1c5f9e; --accent-soft:#e8f1f9; --warn:#b4690e; --warn-soft:#fdf3e3;
  --crit:#a52323; --crit-soft:#fbeaea; --ok:#1d7a4d; --ok-soft:#e7f4ed;
  --shadow:0 1px 2px rgba(20,30,45,.08), 0 2px 8px rgba(20,30,45,.05);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:13px/1.45 "Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif}
button{font:inherit;cursor:pointer}
a{color:var(--accent)}
h1,h2,h3{margin:0}
.topbar{display:flex;align-items:center;gap:16px;padding:10px 18px;background:#22303f;
  color:#eef3f8;box-shadow:var(--shadow);position:sticky;top:0;z-index:20}
.topbar .brand{font-weight:600;letter-spacing:.4px}
.topbar .brand small{opacity:.65;font-weight:400;margin-left:8px}
.topbar .spacer{flex:1}
.topbar .status{font-size:12px;padding:3px 9px;border-radius:99px;background:#33465a}
.topbar .status.ok{background:#1d5c3f}
.topbar .status.bad{background:#7c2b2b}
.topbar button{background:#33465a;color:#eef3f8;border:1px solid #46596d;
  border-radius:5px;padding:5px 11px}
.topbar button:hover{background:#405a70}
.layout{display:grid;grid-template-columns:250px minmax(0,1fr) 340px;gap:14px;
  padding:14px;align-items:start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  box-shadow:var(--shadow)}
.card > header{padding:9px 13px;border-bottom:1px solid var(--line);font-weight:600;
  display:flex;align-items:center;gap:8px;font-size:12.5px;text-transform:uppercase;
  letter-spacing:.5px;color:var(--muted)}
.card > .body{padding:12px 13px}
.plist{list-style:none;margin:0;padding:6px}
.plist li{padding:8px 9px;border-radius:6px;cursor:pointer;border:1px solid transparent}
.plist li:hover{background:#f1f5f9}
.plist li.active{background:var(--accent-soft);border-color:#bcd8ee}
.plist .nm{font-weight:600}
.plist .meta{color:var(--muted);font-size:12px}
.chart-header{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap;
  padding:13px 15px;border-bottom:1px solid var(--line)}
.chart-header .name{font-size:19px;font-weight:650}
.chart-header .demo{color:var(--muted);margin-top:2px}
.chart-header .kv{display:flex;gap:20px;flex-wrap:wrap;margin-top:8px}
.chart-header .kv div{font-size:12px}
.chart-header .kv span{display:block;color:var(--muted);text-transform:uppercase;
  letter-spacing:.4px;font-size:10.5px}
.allergy-chip{background:var(--crit-soft);color:var(--crit);border:1px solid #f0cccc;
  padding:4px 9px;border-radius:6px;font-weight:600;font-size:12px}
.tabs{display:flex;gap:2px;padding:0 12px;border-bottom:1px solid var(--line);background:#fbfcfd}
.tabs button{background:none;border:none;border-bottom:2px solid transparent;
  padding:9px 13px;color:var(--muted);font-weight:600}
.tabs button.active{color:var(--accent);border-bottom-color:var(--accent)}
.tabpane{padding:13px 15px;display:none}
.tabpane.active{display:block}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:1400px){.layout{grid-template-columns:220px minmax(0,1fr) 320px}}
@media(max-width:1100px){.layout{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;
  text-transform:uppercase;letter-spacing:.4px;padding:5px 7px;border-bottom:1px solid var(--line)}
td{padding:6px 7px;border-bottom:1px solid #eef1f5;vertical-align:top}
tr:last-child td{border-bottom:none}
.sub{color:var(--muted);font-size:11.5px}
.flag{font-weight:700;font-size:11px;padding:1px 5px;border-radius:4px}
.flag.H,.flag.HH{background:var(--crit-soft);color:var(--crit)}
.flag.L{background:var(--warn-soft);color:var(--warn)}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;
  background:#eef1f5;color:var(--muted);font-weight:600}
.pill.draft{background:var(--warn-soft);color:var(--warn)}
.pill.active{background:var(--ok-soft);color:var(--ok)}
.pill.completed{background:#eef1f5;color:var(--muted)}
.btn{background:var(--accent);color:#fff;border:none;border-radius:5px;padding:6px 12px;
  font-weight:600}
.btn:hover{background:#17518a}
.btn.sec{background:#fff;color:var(--accent);border:1px solid #bcd8ee}
.btn.sec:hover{background:var(--accent-soft)}
.btn.ghost{background:none;border:1px solid var(--line);color:var(--muted);padding:3px 8px;
  font-size:11.5px;font-weight:600}
.btn:disabled{opacity:.5;cursor:not-allowed}
.orderbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.orderbar input,.orderbar select{border:1px solid var(--line);border-radius:5px;
  padding:6px 9px;font:inherit}
.orderbar input{flex:1;min-width:220px}
.tray{display:flex;flex-direction:column;gap:10px}
.hcard{border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:7px;
  background:var(--panel);box-shadow:var(--shadow);overflow:hidden}
.hcard.warning{border-left-color:var(--warn)}
.hcard.critical{border-left-color:var(--crit)}
.hcard .h{padding:9px 11px;border-bottom:1px solid var(--line)}
.hcard .h .sum{font-weight:650}
.hcard .b{padding:9px 11px;font-size:12.5px}
.hcard .b ul{margin:5px 0 5px 17px;padding:0}
.hcard .b li{margin:2px 0}
.hcard .f{padding:8px 11px;border-top:1px solid var(--line);background:#fbfcfd;
  display:flex;flex-direction:column;gap:6px}
.sugg{display:flex;gap:8px;align-items:center;justify-content:space-between}
.sugg .lbl{flex:1}
.badge{font-size:10.5px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;
  padding:2px 7px;border-radius:4px;background:var(--accent-soft);color:var(--accent)}
.badge.warning{background:var(--warn-soft);color:var(--warn)}
.badge.critical{background:var(--crit-soft);color:var(--crit)}
.src{color:var(--muted);font-size:11px;margin-top:3px}
.empty{color:var(--muted);font-style:italic;padding:6px 0}
.devpanel{margin:0 14px 18px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;box-shadow:var(--shadow)}
.devpanel summary{cursor:pointer;padding:9px 13px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.5px;font-size:12px}
.devbody{padding:0 13px 13px}
.devgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:1100px){.devgrid{grid-template-columns:1fr}}
pre{background:#101a25;color:#d7e3ee;padding:10px;border-radius:6px;overflow:auto;
  max-height:320px;font-size:11.5px;margin:4px 0 0}
.histlist{list-style:none;margin:0;padding:0;max-height:150px;overflow:auto}
.histlist li{padding:4px 0;border-bottom:1px solid #eef1f5;font-size:12px}
.errbar{margin:10px 14px 0;padding:9px 13px;border-radius:6px;
  background:var(--crit-soft);color:var(--crit);border:1px solid #f0cccc;font-weight:600;
  display:none;align-items:center;gap:10px}
.errbar.show{display:flex}
.errbar button{background:none;border:1px solid #e2b9b9;color:var(--crit);border-radius:4px;
  padding:2px 8px;font-size:11.5px;font-weight:600;margin-left:auto}
.toast{position:fixed;right:16px;bottom:16px;background:#22303f;color:#eef3f8;
  padding:9px 14px;border-radius:6px;box-shadow:var(--shadow);z-index:50;display:none}
iframe.smart{width:100%;height:520px;border:1px solid var(--line);border-radius:7px;
  background:#fff;margin-top:10px}
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">Mock EHR <small>ambulatory chart &mdash; demo harness</small></div>
  <div class="spacer"></div>
  <div class="status" id="cdsStatus">CDS: checking&hellip;</div>
  <button id="btnRefresh">Refresh CDS</button>
  <button id="btnReset">Reset data</button>
</div>

<div class="errbar" id="errBar"><span id="errText"></span>
  <button id="errDismiss">Dismiss</button></div>

<div class="layout">
  <section class="card">
    <header>Patients</header>
    <ul class="plist" id="patientList"><li class="empty">Loading&hellip;</li></ul>
  </section>

  <section class="card" id="chartCard">
    <div id="chartHeader" class="chart-header"><div class="empty">Select a patient to open a chart.</div></div>
    <div class="tabs" id="tabs" hidden>
      <button data-tab="summary" class="active">Summary</button>
      <button data-tab="orders">Orders</button>
      <button data-tab="apps">Apps</button>
    </div>
    <div class="tabpane active" id="tab-summary"></div>
    <div class="tabpane" id="tab-orders"></div>
    <div class="tabpane" id="tab-apps"></div>
  </section>

  <section class="card">
    <header>CDS Cards <span id="cardCount" class="pill">0</span></header>
    <div class="body"><div class="tray" id="cardTray"><div class="empty">No cards yet.</div></div></div>
  </section>
</div>

<details class="devpanel" id="devPanel">
  <summary>Developer panel</summary>
  <div class="devbody">
    <div style="margin-bottom:10px"><b>Discovery:</b> <span id="devDiscovery">-</span></div>
    <div style="margin-bottom:10px"><b>Hooks fired:</b>
      <ul class="histlist" id="devHistory"><li class="empty">none yet</li></ul></div>
    <div class="devgrid">
      <div><b>Last hook context</b> <span class="sub">(the raw request is not retained)</span>
        <pre id="devContext">-</pre>
        <div style="margin-top:6px"><b>Prefetch keys:</b> <span id="devPrefetch">-</span></div></div>
      <div><b>Last CDS response</b><pre id="devResponse">-</pre></div>
    </div>
  </div>
</details>

<div class="toast" id="toast"></div>

<script>
const S = {patient:null, chart:null, cards:[], tab:"summary", cdsBase:"http://localhost:8000",
           lastResponse:null, history:[]};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function showError(msg){
  $("errText").textContent = String(msg || "Something went wrong.");
  $("errBar").classList.add("show");
}
function clearError(){ $("errBar").classList.remove("show"); }
/* Every api() call site goes through guard(): a failed fetch must surface in the
   banner, never leave the page silently half-rendered. */
function guard(fn){
  return (...args) => Promise.resolve().then(() => fn(...args)).catch(e => {
    showError(e && e.message ? e.message : String(e));
  });
}

function toast(msg){
  const t = $("toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => { t.style.display = "none"; }, 3200);
}

async function api(path, body){
  const opts = body === undefined ? {} :
    {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  let r;
  try { r = await fetch(path, opts); }
  catch(e){ throw new Error(path + " -> network error: " + e.message); }
  if(!r.ok){
    let detail = "";
    try { const body = await r.json(); detail = body.error || body.detail || ""; } catch(e){}
    throw new Error(path + " -> " + r.status + (detail ? ": " + detail : ""));
  }
  return r.json();
}

/* --- minimal markdown: **bold**, _italic_, `- ` bullets, blank-line paragraphs --- */
function md(text){
  const inline = (s) => esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/_([^_]+)_/g, "<i>$1</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = []; let list = [];
  const flush = () => { if(list.length){ out.push("<ul>" + list.join("") + "</ul>"); list = []; } };
  for(const raw of String(text || "").split("\n")){
    const line = raw.trim();
    if(/^[-*]\s+/.test(line)){ list.push("<li>" + inline(line.replace(/^[-*]\s+/, "")) + "</li>"); }
    else { flush(); if(line) out.push("<p>" + inline(line) + "</p>"); }
  }
  flush();
  return out.join("");
}

/* ------------------------------------------------------------------ patients */
async function loadPatients(){
  const data = await api("/api/patients");
  $("patientList").innerHTML = data.patients.map(p => `
    <li data-id="${esc(p.id)}" class="${p.id === (S.patient||{}).id ? "active" : ""}">
      <div class="nm">${esc(p.name)}</div>
      <div class="meta">${p.age != null ? p.age + "y " : ""}${esc((p.gender||"")[0]||"").toUpperCase()}
        &middot; ${esc(p.mrn)}</div>
      <div class="meta">${esc(p.topProblem || "no active problems")}</div>
    </li>`).join("");
  for(const li of $("patientList").querySelectorAll("li[data-id]")){
    li.onclick = guard(() => openChart(li.dataset.id));
  }
}

async function openChart(id){
  S.patient = {id};
  await loadPatients();
  await refreshChart();
  setCards([], "opening chart");
  const first = await api("/api/hooks/patient-view", {patientId:id});
  applyHook(first);
  // the CDS service warms its cache asynchronously: the summary card lands on call 2
  setTimeout(guard(async () => {
    if(!S.patient || S.patient.id !== id) return;
    applyHook(await api("/api/hooks/patient-view", {patientId:id}));
  }), 1500);
}

async function refreshChart(){
  if(!S.patient) return;
  S.chart = await api(`/api/patients/${encodeURIComponent(S.patient.id)}/chart`);
  renderChart();
}

/* -------------------------------------------------------------------- render */
function renderChart(){
  const c = S.chart; if(!c || c.error){ return; }
  const p = c.patient, enc = c.encounter;
  const allergies = c.allergies.length
    ? c.allergies.map(a => `<span class="allergy-chip">${esc(a.display)}${a.reaction ? " (" + esc(a.reaction) + ")" : ""}</span>`).join(" ")
    : `<span class="pill">No known allergies</span>`;
  $("chartHeader").innerHTML = `
    <div style="flex:1;min-width:260px">
      <div class="name">${esc(p.name)}</div>
      <div class="demo">${p.age != null ? p.age + " y.o. " : ""}${esc(p.gender)} &middot;
        DOB ${esc(p.birthDate)} &middot; MRN ${esc(p.mrn)}</div>
      <div class="kv">
        <div><span>Encounter</span>${enc ? esc(enc.type || enc.class) + " &middot; " + esc(enc.status) : "none"}</div>
        <div><span>Encounter ID</span>${enc ? esc(enc.id) : "-"}</div>
        <div><span>Reason</span>${enc ? esc(enc.reason || "-") : "-"}</div>
      </div>
    </div>
    <div style="max-width:330px"><div class="kv"><div><span>Allergies</span>${allergies}</div></div></div>`;
  $("tabs").hidden = false;
  renderSummary(); renderOrders(); renderApps();
}

function rows(items, cells, emptyText){
  if(!items.length) return `<div class="empty">${esc(emptyText)}</div>`;
  return items.map(cells).join("");
}

function renderSummary(){
  const c = S.chart;
  $("tab-summary").innerHTML = `
  <div class="grid2">
    <div class="card"><header>Problem List</header><div class="body">
      ${c.problems.length ? `<table><tbody>${c.problems.map(p => `
        <tr><td><b>${esc(p.display)}</b>
          <div class="sub">${p.codings.map(cd => esc(cd.code)).join(" &middot; ")}</div></td>
          <td style="width:90px" class="sub">${esc((p.onset||"").slice(0,10))}</td></tr>`).join("")}
        </tbody></table>` : '<div class="empty">No active problems.</div>'}
    </div></div>
    <div class="card"><header>Active Medications</header><div class="body">
      ${c.medications.length ? `<table><tbody>${c.medications.map(m => `
        <tr><td><b>${esc(m.display)}</b><div class="sub">${esc(m.sig)}</div></td></tr>`).join("")}
        </tbody></table>` : '<div class="empty">No active medications.</div>'}
    </div></div>
    <div class="card"><header>Recent Labs</header><div class="body">
      ${c.labs.length ? `<table><thead><tr><th>Test</th><th>Value</th><th>Resulted</th></tr></thead>
        <tbody>${c.labs.slice(0,10).map(l => `
        <tr><td>${esc(l.name)}</td>
          <td><b>${esc(l.value)}</b> ${l.flag ? `<span class="flag ${esc(l.flag)}">${esc(l.flag)}</span>` : ""}</td>
          <td class="sub">${esc((l.when||"").replace("T"," ").slice(0,16))}</td></tr>`).join("")}
        </tbody></table>` : '<div class="empty">No labs on file.</div>'}
    </div></div>
    <div class="card"><header>Vitals</header><div class="body">
      ${c.vitals.length ? `<table><thead><tr><th>Vital</th><th>Value</th><th>Taken</th></tr></thead>
        <tbody>${c.vitals.map(v => `
        <tr><td>${esc(v.name)}</td>
          <td><b>${esc(v.value)}</b> ${v.flag ? `<span class="flag ${esc(v.flag)}">${esc(v.flag)}</span>` : ""}</td>
          <td class="sub">${esc((v.when||"").replace("T"," ").slice(0,16))}</td></tr>`).join("")}
        </tbody></table>` : '<div class="empty">No vitals recorded.</div>'}
    </div></div>
  </div>`;
}

function renderOrders(){
  const o = S.chart.orders;
  $("tab-orders").innerHTML = `
    <div class="orderbar">
      <input id="orderText" placeholder="Order name, e.g. Hemoglobin A1c" autocomplete="off">
      <select id="orderType">
        <option value="ServiceRequest">Procedure / lab</option>
        <option value="MedicationRequest">Medication</option>
      </select>
      <button class="btn" id="btnAddDraft">Add draft</button>
      <button class="btn" id="btnRecheck" title="Re-fires order-select with the current drafts — use this after sending selections back from the SMART app">Re-check suggestions</button>
      <button class="btn sec" id="btnOrderSelect">Re-run order-select</button>
      <button class="btn" id="btnSign" ${o.drafts.length ? "" : "disabled"}>Sign orders (${o.drafts.length})</button>
    </div>
    <div class="card" style="margin-bottom:14px"><header>Unsigned / draft orders</header><div class="body">
      ${o.drafts.length ? `<table><thead><tr><th>Order</th><th>Type</th><th>Origin</th><th></th></tr></thead>
        <tbody>${o.drafts.map(d => `
        <tr><td><b>${esc(d.display)}</b>
              <div class="sub">${d.codings.map(cd => esc(cd.code)).join(" &middot; ")}</div></td>
            <td class="sub">${esc(d.resourceType.replace("Request",""))}</td>
            <td><span class="pill">${esc(d.source || "manual")}</span></td>
            <td style="width:70px"><button class="btn ghost" data-remove="${esc(d.reference)}">Remove</button></td>
        </tr>`).join("")}</tbody></table>`
        : '<div class="empty">No draft orders. Add one above to fire the order-select hook.</div>'}
    </div></div>
    <div class="card"><header>Filed orders</header><div class="body">
      ${o.active.length ? `<table><thead><tr><th>Order</th><th>Status</th><th>Authored</th></tr></thead>
        <tbody>${o.active.map(a => `
        <tr><td><b>${esc(a.display)}</b><div class="sub">${esc(a.instructions || "")}</div></td>
            <td><span class="pill ${esc(a.status)}">${esc(a.status)}</span></td>
            <td class="sub">${esc((a.authoredOn||"").replace("T"," ").slice(0,16))}</td></tr>`).join("")}
        </tbody></table>` : '<div class="empty">No filed orders.</div>'}
    </div></div>`;

  $("btnAddDraft").onclick = guard(addDraft);
  $("orderText").onkeydown = (e) => { if(e.key === "Enter") guard(addDraft)(); };
  const fireOrderSelect = guard(async () => {
    applyHook(await api("/api/hooks/order-select", {patientId:S.patient.id}));
  });
  // Same hook, two affordances: "Re-check suggestions" is the clinician-facing
  // way back from the SMART app (it hands its refined set to the CDS service,
  // which replays it as order-select suggestions).
  $("btnRecheck").onclick = fireOrderSelect;
  $("btnOrderSelect").onclick = fireOrderSelect;
  $("btnSign").onclick = guard(signOrders);
  for(const b of $("tab-orders").querySelectorAll("[data-remove]")){
    b.onclick = guard(async () => {
      await api("/api/orders/remove", {patientId:S.patient.id, reference:b.dataset.remove});
      await refreshChart();
    });
  }
}

function renderApps(){
  $("tab-apps").innerHTML = `
    <div class="card"><header>SMART on FHIR apps</header><div class="body">
      <p class="sub">EHR launch: the chart mints a launch context, then opens the app with
        <code>iss</code> and <code>launch</code>. The app completes the handshake against
        this server's <code>/oauth/authorize</code> and <code>/oauth/token</code>.</p>
      <div class="orderbar">
        <button class="btn" id="btnLaunchApp">Launch EpicVibe Order Assistant</button>
        <button class="btn sec" id="btnLaunchInline">Launch in panel below</button>
      </div>
      <div id="appFrameHost"></div>
    </div></div>`;
  $("btnLaunchApp").onclick = guard(() => launchSmart(S.cdsBase + "/smart/launch", false));
  $("btnLaunchInline").onclick = guard(() => launchSmart(S.cdsBase + "/smart/launch", true));
}

/* --------------------------------------------------------------------- cards */
function setCards(cards, note){
  S.cards = cards || [];
  $("cardCount").textContent = S.cards.length;
  const tray = $("cardTray");
  if(!S.cards.length){
    tray.innerHTML = `<div class="empty">${esc(note || "No cards returned.")}</div>`;
    return;
  }
  tray.innerHTML = S.cards.map((card, ci) => {
    const ind = (card.indicator || "info").toLowerCase();
    const suggestions = (card.suggestions || []).map((s, si) => `
      <div class="sugg"><div class="lbl">${esc(s.label || "suggestion")}</div>
        <button class="btn" data-accept="${ci}:${si}">Accept</button></div>`).join("");
    const links = (card.links || []).map((l, li) => `
      <div class="sugg"><div class="lbl">${esc(l.label || l.url)}
        <span class="pill">${esc(l.type || "absolute")}</span></div>
        <button class="btn sec" data-link="${ci}:${li}">Open</button></div>`).join("");
    return `<div class="hcard ${esc(ind)}">
      <div class="h"><span class="badge ${esc(ind)}">${esc(ind)}</span>
        <div class="sum">${esc(card.summary || "")}</div>
        <div class="src">${esc((card.source || {}).label || "unknown source")}</div></div>
      ${card.detail ? `<div class="b">${md(card.detail)}</div>` : ""}
      ${(suggestions || links) ? `<div class="f">${suggestions}${links}</div>` : ""}
    </div>`;
  }).join("");

  for(const b of tray.querySelectorAll("[data-accept]")){
    b.onclick = guard(() => acceptSuggestion(...b.dataset.accept.split(":").map(Number), b));
  }
  for(const b of tray.querySelectorAll("[data-link]")){
    b.onclick = guard(() => openLink(...b.dataset.link.split(":").map(Number)));
  }
}

function applyHook(hook){
  if(!hook) return;
  S.lastService = hook.serviceId;
  if(hook.response) S.lastResponse = hook.response;
  if(hook.error){
    setCards([], "hook error: " + hook.error);
    toast(hook.hook + ": " + hook.error);
  } else {
    setCards(hook.cards, "No cards for " + hook.hook + ".");
  }
  guard(refreshDev)();
}

async function acceptSuggestion(ci, si, button){
  const card = S.cards[ci], sugg = (card.suggestions || [])[si];
  const action = (sugg.actions || []).find(a => (a.type || "").toLowerCase() === "create");
  if(!action || !action.resource){ toast("Suggestion has no create action."); return; }
  button.disabled = true;
  const res = await api("/api/suggestions/accept", {
    patientId:S.patient.id, resource:action.resource,
    serviceId:S.lastService, cardUuid:card.uuid, suggestionUuid:sugg.uuid});
  if(res.error){ toast(res.error); button.disabled = false; return; }
  toast("Filed draft: " + (res.created.display || res.created.id) +
        (res.feedback && res.feedback.sent ? " (feedback sent)" : ""));
  button.textContent = "Accepted";
  await refreshChart();
  selectTab("orders");
}

async function addDraft(){
  const text = $("orderText").value.trim();
  if(!text){ toast("Type an order name first."); return; }
  const type = $("orderType").value;
  const res = await api("/api/orders/draft", {patientId:S.patient.id, text, orderType:type});
  if(res.error){ toast(res.error); return; }
  await refreshChart();
  selectTab("orders");
  applyHook(res.hook);
}

async function signOrders(){
  const res = await api("/api/orders/sign", {patientId:S.patient.id});
  applyHook(res.hook);
  if(res.blocked){
    showError("Signing blocked by a critical CDS card: " + (res.blockedBy || []).join("; "));
  } else {
    clearError();
    toast("Signed " + res.signed.length + " order(s).");
  }
  await refreshChart();
  selectTab("orders");
}

async function openLink(ci, li){
  const link = (S.cards[ci].links || [])[li];
  if(!link) return;
  if((link.type || "").toLowerCase() === "smart"){ await launchSmart(link.url, false); }
  else { window.open(link.url, "_blank", "noopener"); }
}

async function launchSmart(url, inline){
  const res = await api("/api/smart/launch", {patientId:S.patient.id, url});
  if(inline){
    selectTab("apps");
    $("appFrameHost").innerHTML = `<iframe class="smart" src="${esc(res.url)}"></iframe>
      <div class="sub" style="margin-top:6px">launch id ${esc(res.launchId)} &rarr; ${esc(res.url)}</div>`;
  } else {
    window.open(res.url, "_blank", "noopener");
    toast("Launched SMART app (launch " + res.launchId + ")");
  }
}

/* ------------------------------------------------------------------- chrome */
function selectTab(name){
  S.tab = name;
  for(const b of $("tabs").querySelectorAll("button")) b.classList.toggle("active", b.dataset.tab === name);
  for(const pane of document.querySelectorAll(".tabpane"))
    pane.classList.toggle("active", pane.id === "tab-" + name);
}

async function refreshDev(){
  const data = await api("/api/dev/history");
  const last = data.history[0] || null;
  $("devContext").textContent = last && last.context
    ? JSON.stringify(last.context, null, 2) : "-";
  const counts = (last && last.prefetchCounts) || {};
  const keys = Object.keys(counts);
  $("devPrefetch").textContent = keys.length
    ? keys.map(k => k + ": " + counts[k] + " resource(s)").join(" \u00b7 ") : "none";
  const response = (last && last.response) || S.lastResponse;
  $("devResponse").textContent = response ? JSON.stringify(response, null, 2) : "-";
  const d = data.discovery;
  $("devDiscovery").textContent = d.ok
    ? `${d.cdsBaseUrl} OK - ${d.services.map(s => s.id).join(", ")} (fetched ${d.fetchedAt})`
    : `${d.cdsBaseUrl} UNAVAILABLE - ${d.error}`;
  $("devHistory").innerHTML = data.history.length ? data.history.map(h => `
    <li>${esc(h.at)} &middot; <b>${esc(h.hook)}</b> &rarr; ${esc(h.serviceId || "-")}
      &middot; ${h.error ? "ERROR " + esc(h.error) : (h.cards || []).length + " card(s)"}</li>`).join("")
    : '<li class="empty">none yet</li>';
  setCdsBadge(d);
}

function setCdsBadge(d){
  S.cdsBase = d.cdsBaseUrl || S.cdsBase;
  const el = $("cdsStatus");
  el.className = "status " + (d.ok ? "ok" : "bad");
  el.textContent = d.ok ? `CDS: ${d.services.length} service(s)` : "CDS: unavailable";
  el.title = d.error || d.cdsBaseUrl;
}

$("tabs").onclick = (e) => { if(e.target.dataset.tab) selectTab(e.target.dataset.tab); };
$("errDismiss").onclick = clearError;
$("btnRefresh").onclick = guard(async () => {
  setCdsBadge(await api("/api/cds/refresh", {})); clearError(); toast("Discovery refreshed.");
});
$("btnReset").onclick = guard(async () => {
  await api("/api/reset", {});
  S.patient = null; S.chart = null; S.lastRequest = null; S.lastResponse = null;
  $("chartHeader").innerHTML = '<div class="empty">Select a patient to open a chart.</div>';
  $("tabs").hidden = true;
  for(const pane of document.querySelectorAll(".tabpane")) pane.innerHTML = "";
  setCards([], "Data reset.");
  await loadPatients(); await refreshDev();
  clearError();
  toast("Fixtures reloaded.");
});

guard(async () => { await loadPatients(); await refreshDev(); })();
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE)
