"""The SMART app page: single inline HTML document, no build step, no CDN."""

APP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EpicVibe Order Assistant</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --ink: #14181f; --muted: #5c6673;
    --line: #dfe3e8; --accent: #1a6fd4; --accent-ink: #ffffff; --ok: #1a7f47;
    --warn: #9a6700; --bad: #b42318;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 12px 20px; }
  .banner { display: flex; flex-wrap: wrap; gap: 16px; align-items: baseline; }
  .banner h1 { font-size: 16px; margin: 0; }
  .banner .meta { color: var(--muted); font-size: 13px; }
  main { max-width: 980px; margin: 0 auto; padding: 20px; }
  .bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
         margin-bottom: 16px; }
  .chip { background: var(--panel); border: 1px solid var(--line); border-radius: 999px;
          padding: 3px 10px; font-size: 12px; color: var(--muted); }
  .chip strong { color: var(--ink); }
  section.oset { background: var(--panel); border: 1px solid var(--line);
                 border-radius: 8px; margin-bottom: 18px; overflow: hidden; }
  section.oset > h2 { font-size: 15px; margin: 0; padding: 12px 16px;
                      border-bottom: 1px solid var(--line); }
  .rationale { padding: 10px 16px; color: var(--muted); border-bottom: 1px solid var(--line); }
  .group { border-bottom: 1px solid var(--line); }
  .group:last-child { border-bottom: 0; }
  .group > h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
                color: var(--muted); margin: 0; padding: 10px 16px 4px; }
  .item { display: grid; grid-template-columns: 24px 1fr; gap: 8px;
          padding: 10px 16px 14px; border-top: 1px dotted var(--line); }
  .item.off { opacity: .55; }
  .item h4 { margin: 0 0 2px; font-size: 14px; font-weight: 600; }
  .item .why { color: var(--muted); margin: 2px 0; }
  .evidence { margin: 4px 0 0; padding-left: 18px; color: var(--muted); font-size: 13px; }
  .fields { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .field { display: flex; flex-direction: column; gap: 2px; }
  .field label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
                 color: var(--muted); }
  .field input { border: 1px solid var(--line); border-radius: 5px; padding: 5px 7px;
                 font: inherit; min-width: 150px; background: #fff; color: inherit; }
  select { border: 1px solid var(--line); border-radius: 5px; padding: 4px 6px; font: inherit; }
  button { background: var(--accent); color: var(--accent-ink); border: 0;
           border-radius: 6px; padding: 9px 16px; font: inherit; font-weight: 600;
           cursor: pointer; }
  button.secondary { background: var(--panel); color: var(--ink); border: 1px solid var(--line); }
  button[disabled] { opacity: .5; cursor: default; }
  .status { margin: 12px 0; color: var(--muted); }
  .status.error { color: var(--bad); }
  .spinner { display: inline-block; width: 14px; height: 14px; margin-right: 8px;
             border: 2px solid var(--line); border-top-color: var(--accent);
             border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -2px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .results { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
             padding: 12px 16px; margin-top: 16px; }
  .results li { margin: 3px 0; }
  .ok { color: var(--ok); } .bad { color: var(--bad); }
  .footer-actions { position: sticky; bottom: 0; background: var(--bg);
                    padding: 12px 0; display: flex; gap: 10px; align-items: center; }
</style>
</head>
<body>
<header>
  <div class="banner">
    <h1 id="pt-name">Loading patient&hellip;</h1>
    <span class="meta" id="pt-meta"></span>
    <span class="meta" id="pt-ctx"></span>
  </div>
</header>
<main>
  <div class="bar" id="counts"></div>
  <div class="status" id="status"><span class="spinner"></span>Reviewing the chart&hellip;</div>
  <div id="sets"></div>
  <div class="footer-actions" id="actions" hidden>
    <button id="send">Send to EHR</button>
    <button id="reload" class="secondary">Re-run review</button>
    <span class="status" id="send-status"></span>
  </div>
  <div class="results" id="results" hidden></div>
</main>
<script>
(function () {
  "use strict";
  var proposal = null;
  var submitMode = "fhir";
  var el = function (id) { return document.getElementById(id); };
  var text = function (s) { return String(s === null || s === undefined ? "" : s); };

  function esc(s) {
    return text(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function setStatus(msg, isError) {
    var node = el("status");
    node.className = "status" + (isError ? " error" : "");
    node.innerHTML = msg;
  }

  function renderCounts(counts, confidence) {
    var order = ["conditions", "medications", "observations", "labs", "vitals",
                 "allergies", "active_orders"];
    var html = '<span class="chip">confidence <strong>' + esc(confidence) + "</strong></span>";
    order.forEach(function (key) {
      if (counts[key] === undefined) { return; }
      html += '<span class="chip">' + esc(key.replace("_", " ")) +
              " <strong>" + esc(counts[key]) + "</strong></span>";
    });
    el("counts").innerHTML = html;
  }

  function renderItem(oi, gi, ii, item) {
    var key = oi + "-" + gi + "-" + ii;
    var html = '<div class="item' + (item.include ? "" : " off") + '" data-key="' + key + '">';
    html += '<div><input type="checkbox" data-role="include" data-key="' + key + '"' +
            (item.include ? " checked" : "") + ' aria-label="include order"></div><div>';
    html += "<h4>" + esc(item.name) + "</h4>";
    if (item.variants && item.variants.length > 1) {
      html += '<select data-role="variant" data-key="' + key + '">';
      item.variants.forEach(function (v) {
        html += '<option value="' + esc(v.variant_id) + '"' +
                (v.variant_id === item.variant_id ? " selected" : "") + ">" +
                esc(v.label) + "</option>";
      });
      html += "</select>";
    } else {
      html += '<div class="why">' + esc(item.variant_label) + "</div>";
    }
    if (item.rationale) { html += '<div class="why">' + esc(item.rationale) + "</div>"; }
    if (item.evidence && item.evidence.length) {
      html += '<ul class="evidence">';
      item.evidence.forEach(function (e) { html += "<li>" + esc(e) + "</li>"; });
      html += "</ul>";
    }
    if (item.fields && item.fields.length) {
      html += '<div class="fields">';
      item.fields.forEach(function (f) {
        html += '<div class="field"><label for="f-' + key + "-" + esc(f.name) + '">' +
                esc(f.name) + "</label>" +
                '<input id="f-' + key + "-" + esc(f.name) + '" data-role="field" data-key="' +
                key + '" data-field="' + esc(f.name) + '" value="' + esc(f.value) + '"></div>';
      });
      html += "</div>";
    }
    return html + "</div></div>";
  }

  function render(data) {
    proposal = data;
    var banner = data.banner || {};
    el("pt-name").textContent = banner.name || "Unknown patient";
    var meta = [];
    if (banner.gender) { meta.push(banner.gender); }
    if (banner.birth_date) { meta.push("DOB " + banner.birth_date); }
    if (banner.mrn) { meta.push("MRN " + banner.mrn); }
    el("pt-meta").textContent = meta.join(" \\u00b7 ");
    renderCounts(data.counts || {}, data.confidence || "low");

    if (!data.order_sets || !data.order_sets.length) {
      setStatus("No approved order set matched this chart.", false);
      el("sets").innerHTML = "";
      return;
    }
    var html = "";
    data.order_sets.forEach(function (oset, oi) {
      html += '<section class="oset"><h2>' + esc(oset.name) + "</h2>";
      if (oset.rationale) { html += '<div class="rationale">' + esc(oset.rationale) + "</div>"; }
      (oset.groups || []).forEach(function (group, gi) {
        html += '<div class="group"><h3>' + esc(group.name) + "</h3>";
        (group.items || []).forEach(function (item, ii) {
          html += renderItem(oi, gi, ii, item);
        });
        html += "</div>";
      });
      html += "</section>";
    });
    el("sets").innerHTML = html;
    el("actions").hidden = false;
    setStatus(submitMode === "handback"
      ? "Review and edit below, then send the selected orders back to order entry."
      : "Review and edit below, then send the selected orders to the EHR.", false);

    el("sets").addEventListener("change", function (ev) {
      var target = ev.target;
      if (target.getAttribute("data-role") !== "include") { return; }
      var row = document.querySelector('.item[data-key="' + target.getAttribute("data-key") + '"]');
      if (row) { row.className = "item" + (target.checked ? "" : " off"); }
    });
  }

  function collect() {
    var out = [];
    if (!proposal) { return out; }
    (proposal.order_sets || []).forEach(function (oset, oi) {
      (oset.groups || []).forEach(function (group, gi) {
        (group.items || []).forEach(function (item, ii) {
          var key = oi + "-" + gi + "-" + ii;
          var box = document.querySelector('input[data-role="include"][data-key="' + key + '"]');
          if (!box || !box.checked) { return; }
          var variantEl = document.querySelector('select[data-role="variant"][data-key="' + key + '"]');
          var fields = {};
          var inputs = document.querySelectorAll('input[data-role="field"][data-key="' + key + '"]');
          Array.prototype.forEach.call(inputs, function (input) {
            if (input.value !== "") { fields[input.getAttribute("data-field")] = input.value; }
          });
          out.push({
            order_set_id: oset.order_set_id,
            item_id: item.item_id,
            variant_id: variantEl ? variantEl.value : item.variant_id,
            fields: fields
          });
        });
      });
    });
    return out;
  }

  function loadProposal() {
    el("actions").hidden = true;
    el("results").hidden = true;
    setStatus('<span class="spinner"></span>Reviewing the chart&hellip;', false);
    fetch("/smart/api/proposal", { method: "POST", credentials: "same-origin",
                                   headers: { "Content-Type": "application/json" },
                                   body: "{}" })
      .then(function (r) {
        if (!r.ok) { throw new Error("proposal request failed (" + r.status + ")"); }
        return r.json();
      })
      .then(render)
      .catch(function (err) { setStatus(esc(err.message), true); });
  }

  function send() {
    var items = collect();
    if (!items.length) { el("send-status").textContent = "Nothing selected."; return; }
    el("send").disabled = true;
    el("send-status").innerHTML = '<span class="spinner"></span>Sending ' + items.length + "&hellip;";
    fetch("/smart/api/submit", { method: "POST", credentials: "same-origin",
                                 headers: { "Content-Type": "application/json" },
                                 body: JSON.stringify({ items: items }) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.mode === "handback") { return renderHandback(res); }
        var html = "<h3>Sent to the EHR</h3><ul>";
        (res.created || []).forEach(function (c) {
          html += '<li class="ok">' + esc(c.label) + " &rarr; " + esc(c.resource_type) +
                  "/" + esc(c.id) + ' <a href="' + esc(c.url) + '" target="_blank" rel="noopener">open</a></li>';
        });
        (res.failed || []).forEach(function (f) {
          html += '<li class="bad">' + esc(f.item_id) + " &mdash; " + esc(f.error) + "</li>";
        });
        html += "</ul>";
        el("results").innerHTML = html;
        el("results").hidden = false;
        el("send-status").textContent = (res.created || []).length + " created, " +
                                        (res.failed || []).length + " failed.";
      })
      .catch(function (err) { el("send-status").textContent = "Send failed: " + err.message; })
      .then(function () { el("send").disabled = false; });
  }

  function renderHandback(res) {
    var html = "<h3>Sent to order entry</h3>";
    html += "<p>" + esc(res.message || "") + "</p>";
    html += "<p><strong>Return to the EHR chart</strong> and open order entry " +
            "(in the mock EHR: the Orders tab, then <em>Re-check suggestions</em>). " +
            "The reviewed orders appear as suggestions to accept and sign.</p>";
    if ((res.failed || []).length) {
      html += "<ul>";
      (res.failed || []).forEach(function (f) {
        html += '<li class="bad">' + esc(f.item_id) + " &mdash; " + esc(f.error) + "</li>";
      });
      html += "</ul>";
    }
    el("results").innerHTML = html;
    el("results").hidden = false;
    el("send-status").textContent = res.stored + " selection(s) waiting in order entry" +
      (res.encounter ? " for encounter " + res.encounter : "") + ".";
  }

  fetch("/smart/api/context", { credentials: "same-origin" })
    .then(function (r) {
      if (!r.ok) { throw new Error("No SMART session - relaunch the app from the EHR."); }
      return r.json();
    })
    .then(function (ctx) {
      el("pt-ctx").textContent = ctx.encounter ? "Encounter " + ctx.encounter : "";
      submitMode = ctx.submit_mode || "fhir";
      el("send").textContent = submitMode === "handback"
        ? "Send to order entry" : "Send to EHR";
      loadProposal();
    })
    .catch(function (err) { setStatus(esc(err.message), true); });

  el("send").addEventListener("click", send);
  el("reload").addEventListener("click", loadProposal);
}());
</script>
</body>
</html>
"""
