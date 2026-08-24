/* AEGIS dashboard — vanilla JS, no build step, no external dependencies. */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (x) => (x * 100).toFixed(1) + '%';
const num = (x, d = 2) => (x ?? 0).toFixed(d);

const STATE = { decisions: new Map(), deep: new Map(), selected: null };

/* ------------------------------------------------------------------ nav */
$$('nav button').forEach(b => b.onclick = () => {
  $$('nav button').forEach(x => x.classList.toggle('on', x === b));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + b.dataset.v));
  if (b.dataset.v === 'metrics') loadMetrics();
  if (b.dataset.v === 'queue') loadQueue();
  if (b.dataset.v === 'policy') loadPolicies();
  if (b.dataset.v === 'audit') loadAudit();
});

/* --------------------------------------------------------------- header */
async function loadHealth() {
  const h = await (await fetch('/healthz')).json();
  $('#h-backend').textContent = h.backend;
  $('#h-seed').textContent = h.seed;
  const fitted = Object.values(h.models_fitted).every(Boolean);
  $('#h-models').textContent = fitted ? 'fitted' : 'cold-start prior';
  $('#h-models').className = fitted ? 'good' : 'warn';
  const v = await (await fetch('/v1/control/ledger/verify')).json();
  $('#h-ledger').innerHTML = v.ok
    ? `<b class="good">intact</b> · ${v.records}`
    : `<b class="bad">BROKEN @ ${v.broken_at}</b>`;
}

/* ------------------------------------------------------------------ SSE */
function connect() {
  const es = new EventSource('/api/events');
  es.addEventListener('open', () => {
    $('#h-live').innerHTML = '<b class="good">● live</b>';
  });
  es.addEventListener('error', () => {
    $('#h-live').innerHTML = '<b class="bad">● disconnected</b>';
  });
  es.addEventListener('decision', e => {
    const d = JSON.parse(e.data);
    STATE.decisions.set(d.request_id, d);
    renderFeed();
  });
  es.addEventListener('deep_audit', e => {
    const d = JSON.parse(e.data);
    STATE.deep.set(d.request_id, d);
    renderFeed();
    if (STATE.selected === d.request_id) renderDetail(d.request_id);
  });
  es.addEventListener('human_feedback', () => { loadQueue(); loadHealth(); });
  es.addEventListener('model_retrain', () => loadHealth());
}

/* ----------------------------------------------------------------- feed */
function renderFeed() {
  const rows = [...STATE.decisions.values()].sort((a, b) => b.created_at - a.created_at);
  $('#feed-empty').style.display = rows.length ? 'none' : '';
  $('#feed-count').textContent = rows.length ? `${rows.length} requests` : '';
  $('#feed').innerHTML = rows.map(d => {
    const deep = STATE.deep.get(d.request_id);
    const inline = d.streamed ? 'GREEN' : d.decision;
    const final = deep ? deep.deep_decision : inline;
    const b = d.budget || {};
    const over = b.exhausted || (b.spent_ms > b.total_ms);
    const budget = d.streamed
      ? '<span class="good">streamed 0ms</span>'
      : `<span class="${over ? 'bad' : ''}">${num(b.spent_ms, 0)}/${b.total_ms}ms${over ? ' !' : ''}</span>`;
    const changed = final !== inline
      ? ` <span class="dimmer">→</span> <span class="pill ${final}">${final}</span>` : '';
    const retr = deep && deep.retracted ? ' <span class="bad mono">RETRACTED</span>' : '';
    const verif = d.streamed ? '<span class="dimmer">async</span>'
      : (d.verifier_gated_in ? '<span class="good">ran</span>' : '<span class="dimmer">gated</span>');
    return `<tr class="click ${STATE.selected === d.request_id ? 'sel' : ''}"
              data-id="${d.request_id}">
      <td class="mono">${esc(d.scenario_id || d.request_id)}</td>
      <td>${esc(d.use_case)}</td>
      <td class="mono">${esc(d.tier)}</td>
      <td><span class="pill ${inline}">${inline}</span>${changed}${retr}</td>
      <td class="right mono">${num(d.confidence)}</td>
      <td class="right mono">${budget}</td>
      <td class="mono">${verif}</td>
    </tr>`;
  }).join('');
  $$('#feed tr.click').forEach(tr => tr.onclick = () => {
    STATE.selected = tr.dataset.id; renderFeed(); renderDetail(tr.dataset.id);
  });
}

/* ------------------------------------------- budget readout (req. 5) --- */
function budgetBlock(b, streamed) {
  if (!b) return '';
  if (streamed) {
    return `<div class="budget-head">
        <span class="budget-num good">0ms</span>
        <span class="muted">inline overhead — streamed, audit deferred off the request path</span>
      </div>`;
  }
  const total = b.total_ms || 1;
  const spent = b.spent_ms || 0;
  const over = spent > total || b.exhausted;
  const segs = (b.segments || []).map(s => {
    const w = Math.max(0.4, (s.ms / total) * 100);
    return `<div class="seg" data-n="${esc(s.name)}" style="width:${w}%"
             title="${esc(s.name)}: ${num(s.ms)}ms"></div>`;
  }).join('');
  const overBar = spent > total
    ? `<div class="seg over" style="width:${Math.min(40, ((spent - total) / total) * 100)}%"
        title="over budget by ${num(spent - total)}ms"></div>` : '';
  const legend = (b.segments || []).map(s =>
    `<span><i style="background:var(--x)" data-n="${esc(s.name)}"></i>${esc(s.name)} ${num(s.ms)}ms</span>`
  ).join('');
  const skipped = (b.skipped || []).map(s =>
    `<li>${esc(s.name)} — ${esc(s.reason)} <span class="dimmer">(needed ~${num(s.needed_ms, 0)}ms, ${num(s.remaining_ms, 0)}ms left)</span></li>`
  ).join('');
  const shortfalls = (b.shortfalls || []).map(s =>
    `<li class="warn">${esc(s.name)} — ${esc(s.reason)} <span class="dimmer">(needed ~${num(s.needed_ms, 0)}ms, had ${num(s.remaining_ms, 0)}ms)</span></li>`
  ).join('');
  return `
    <div class="budget-head">
      <span class="budget-num ${over ? 'bad' : 'good'}">${num(spent, 0)}ms / ${total}ms</span>
      ${over ? '<span class="bad mono">BUDGET EXHAUSTED — fallback engaged</span>'
             : '<span class="muted">within the hold-and-release budget</span>'}
    </div>
    <div class="budget-bar">${segs}${overBar}</div>
    <div class="budget-legend">${legend}</div>
    ${(shortfalls || skipped) ? `<ul class="reasons">${shortfalls}${skipped}</ul>` : ''}`;
}

/* --------------------------------- verifier reasoning trace (req. 1) --- */
function traceBlock(t) {
  if (!t) return `<div class="empty">Verifier did not run for this request
      (gated out by the adaptive scheduler, or deferred to the async pass).</div>`;
  if (!t.ran) return `<div class="empty">Verifier skipped: ${esc(t.skip_reason)}</div>`;
  const claims = (t.claims || []).map(c => {
    const col = c.verdict === 'CONTRADICTED' ? 'var(--red)'
      : c.verdict === 'UNSUPPORTED' ? 'var(--yellow)' : 'var(--green)';
    const reasons = (c.reasons || []).map(r =>
      `<li class="${/mismatch|absent|polarity|does not appear/.test(r) ? 'hit' : ''}">${esc(r)}</li>`
    ).join('');
    return `<div class="claim">
      <div class="claim-top">
        <span class="pill ${c.verdict}">${c.verdict}</span>
        <div class="claim-txt">${esc(c.claim)}</div>
        <span class="mono dimmer">d=${num(c.disagreement)}</span>
      </div>
      <div class="dbar"><i style="width:${Math.round(c.disagreement * 100)}%;background:${col}"></i></div>
      <div class="evidence"><span class="dimmer">best matching context
        (sim ${num(c.evidence_similarity)}):</span><br>${esc(c.best_evidence)}</div>
      <ul class="reasons">${reasons}</ul>
    </div>`;
  }).join('');
  const partial = t.budget_exhausted || t.claims_checked < t.claims_total;
  return `
    <div class="kv" style="margin-bottom:10px">
      <span class="k">question</span><span>${esc(t.question)}</span>
      <span class="k">verifier re-answer</span><span>${esc(t.verifier_extractive_answer) || '<span class="dimmer">—</span>'}</span>
      <span class="k">answer-slot disagreement</span>
      <span class="${t.answer_slot_disagreement > 0.3 ? 'bad' : 'good'}">${num(t.answer_slot_disagreement)}</span>
      <span class="k">claims checked</span>
      <span class="${partial ? 'warn' : ''}">${t.claims_checked} of ${t.claims_total}
        ${partial ? '— preempted by the latency budget' : ''}</span>
      <span class="k">context</span>
      <span>${t.context_indexed || t.context_sentences} of ${t.context_sentences} sentences indexed</span>
      <span class="k">verifier time</span><span>${num(t.elapsed_ms)}ms</span>
    </div>${claims}`;
}

function laneBlock(lanes) {
  if (!lanes || !Object.keys(lanes).length)
    return '<div class="empty">No lane scores — response was streamed before any check ran.</div>';
  return Object.entries(lanes).map(([name, l]) => {
    const contrib = Object.entries(l.contributions || {}).slice(0, 4).map(([k, v]) =>
      `<span class="k">${esc(k)}</span><span class="${v > 0 ? 'delta-pos' : 'delta-neg'}">${v > 0 ? '+' : ''}${num(v)}</span>`
    ).join('');
    const band = l.p_high - l.p_low > 0.001
      ? ` <span class="dimmer">band [${num(l.p_low, 3)}, ${num(l.p_high, 3)}]</span>` : '';
    return `<div style="margin-bottom:10px">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
        <span class="pill ${l.decision}">${l.decision}</span>
        <b>${esc(name)}</b>
        <span class="mono">p=${num(l.probability, 4)}${band}</span>
        <span class="dimmer mono">thresholds ${num(l.threshold_yellow, 3)} / ${num(l.threshold_red, 3)}</span>
      </div>
      <div class="kv">${contrib || '<span class="k dimmer">no feature contributed</span><span></span>'}</div>
      ${(l.top_reasons || []).filter(r => /partial|no evidence/.test(r))
        .map(r => `<div class="warn" style="font-size:12px">${esc(r)}</div>`).join('')}
    </div>`;
  }).join('');
}

async function renderDetail(id) {
  const d = STATE.decisions.get(id);
  if (!d) return;
  let deep = STATE.deep.get(id);
  if (!deep) {
    const recs = await (await fetch('/api/decision/' + id)).json();
    const dp = (recs.records || []).filter(r => r.kind === 'deep_audit');
    if (dp.length) { deep = dp[dp.length - 1].payload; STATE.deep.set(id, deep); }
  }
  const inline = d.streamed ? 'GREEN' : d.decision;
  // Show the INLINE trace first. Preferring the async one would hide exactly
  // the thing the budget-miss case exists to demonstrate: a verification that
  // was cut short in front of the user and completed afterwards.
  const inlineTrace = d.verifier_trace;
  const deepTrace = deep && deep.verifier_trace;
  const showBoth = inlineTrace && deepTrace &&
    (inlineTrace.claims_checked !== deepTrace.claims_checked ||
     inlineTrace.budget_exhausted !== deepTrace.budget_exhausted);
  $('#detail').innerHTML = `
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span class="pill ${inline}" style="font-size:13px;padding:4px 12px">${inline}</span>
        ${deep && deep.deep_decision !== inline
          ? `<span class="dimmer">→ after async audit</span>
             <span class="pill ${deep.deep_decision}">${deep.deep_decision}</span>` : ''}
        ${deep && deep.retracted ? '<span class="bad mono">RETRACTED</span>' : ''}
        ${deep && deep.escalated_post_hoc ? '<span class="warn mono">ESCALATED POST HOC</span>' : ''}
        <span class="mono muted">confidence ${num(d.confidence)}</span>
        <span class="spacer"></span>
        <span class="mono dimmer">${esc(d.policy_version)}</span>
      </div>
      <div class="kv" style="margin-top:10px">
        <span class="k">stakes tier</span><span>${esc(d.tier)} — ${esc(d.prior.reasons.slice(-1)[0] || '')}</span>
        <span class="k">action</span><span>${esc(d.action)}</span>
        <span class="k">gate</span><span>${esc(d.gate_reason)}</span>
        ${d.escalate_to_human ? `<span class="k">human review</span>
          <span class="warn">${esc((d.escalation_reasons || []).join('; '))}</span>` : ''}
        ${(d.edits || []).length ? `<span class="k">edits applied</span>
          <span>${esc(d.edits.join('; '))}</span>` : ''}
      </div>
    </div>

    <div class="card" style="margin-bottom:14px">
      <h3>Latency budget</h3>
      ${budgetBlock(d.budget, d.streamed)}
    </div>

    <div class="card" style="margin-bottom:14px">
      <h3>Verifier reasoning trace
        <span class="sub">${inlineTrace ? 'inline — what the user\'s request actually paid for'
                                        : (deepTrace ? 'async deep pass' : '')}</span></h3>
      ${traceBlock(inlineTrace || deepTrace)}
    </div>

    ${showBoth ? `<div class="card" style="margin-bottom:14px">
      <h3>Asynchronous deep pass <span class="sub">same verifier, no latency ceiling</span></h3>
      <p class="muted" style="font-size:12px;margin-top:0">
        The inline pass checked ${inlineTrace.claims_checked} of ${inlineTrace.claims_total}
        claims before the budget ran out. This pass completed
        ${deepTrace.claims_checked} of ${deepTrace.claims_total} off the request path.
      </p>
      ${traceBlock(deepTrace)}
    </div>` : ''}

    <div class="card" style="margin-bottom:14px">
      <h3>Lane scores</h3>
      ${laneBlock(Object.keys(d.lanes || {}).length ? d.lanes : (deep && deep.lanes))}
    </div>

    <div class="card">
      <h3>Response</h3>
      ${d.delivered_text !== d.original_text
        ? `<div class="muted mono" style="font-size:11px">MODEL DRAFT</div>
           <pre class="txt">${esc(d.original_text)}</pre>
           <div class="muted mono" style="font-size:11px;margin-top:8px">DELIVERED</div>
           <pre class="txt">${esc(d.delivered_text)}</pre>`
        : `<pre class="txt">${esc(d.delivered_text)}</pre>`}
    </div>`;
  // colour the legend swatches to match the bars
  $$('#detail .budget-legend i[data-n]').forEach(i => {
    const probe = document.createElement('div');
    probe.className = 'seg'; probe.dataset.n = i.dataset.n;
    document.body.appendChild(probe);
    i.style.background = getComputedStyle(probe).backgroundColor;
    probe.remove();
  });
}

/* -------------------------------------------------------------- metrics */
async function loadMetrics() {
  const m = await (await fetch('/api/metrics')).json();
  const o = m.overall, lat = m.latency, ad = m.adaptive, c = m.cost;
  $('#m-acc').textContent = pct(o.decision_accuracy);
  $('#m-acc-sub').textContent =
    `${o.correct}/${o.scored} after async · inline ${pct(o.inline_decision_accuracy)}`;
  $('#m-lat').textContent = num(lat.inline_overhead_p95_ms, 1) + 'ms';
  $('#m-lat-sub').textContent =
    `p50 ${num(lat.inline_overhead_p50_ms, 1)}ms · ${lat.within_budget}/${m.counts.held} within their own budget`;
  $('#m-verif').textContent = pct(ad.verifier_invocation_rate);
  $('#m-verif-sub').textContent = `${ad.verifier_runs} of ${ad.held_requests} held requests`;

  const laneRow = (n, r, full) => `<tr><td>${esc(n)}</td>
    <td class="right mono">${r.tp}</td><td class="right mono ${r.fp ? 'warn' : ''}">${r.fp}</td>
    <td class="right mono ${r.fn ? 'bad' : ''}">${r.fn}</td><td class="right mono">${r.tn}</td>
    <td class="right mono">${num(r.precision, 3)}</td><td class="right mono">${num(r.recall, 3)}</td>
    ${full ? `<td class="right mono">${num(r.f1, 3)}</td>
      <td class="right mono">${num(r.false_positive_rate, 3)}</td>
      <td class="right mono">${num(r.false_negative_rate, 3)}</td>` : ''}</tr>`;
  $('#m-lanes').innerHTML = Object.entries(m.lanes).map(([n, r]) => laneRow(n, r, true)).join('');
  $('#m-lanes-inline').innerHTML =
    Object.entries(m.lanes_inline || {}).map(([n, r]) => laneRow(n, r, false)).join('');

  $('#m-latency').innerHTML = `
    <span class="k">inline overhead p50 / p95</span><span>${num(lat.inline_overhead_p50_ms, 1)} / ${num(lat.inline_overhead_p95_ms, 1)} ms</span>
    <span class="k">streamed inline overhead</span><span class="good">${num(lat.streamed_inline_overhead_ms, 1)} ms</span>
    <span class="k">within own policy budget</span><span>${lat.within_budget}/${m.counts.held} (${pct(lat.within_budget_rate)})</span>
    <span class="k">p95 overhead as % of budget</span><span>${num(lat.overhead_pct_of_budget_p95, 0)}%</span>
    <span class="k">budget exhausted</span><span class="${lat.budget_exhausted ? 'warn' : ''}">${lat.budget_exhausted} (${pct(lat.budget_exhausted_rate)})</span>
    <span class="k">upstream p50</span><span>${num(lat.upstream_p50_ms, 1)} ms</span>`;
  $('#m-adaptive').innerHTML = Object.entries(ad.by_policy || {}).map(([k, v]) =>
    `<span class="k">${esc(k)}</span><span>${v.verified}/${v.held} held (${pct(v.rate)})</span>`).join('')
    + Object.entries(ad.scheduler || {}).map(([k, v]) =>
      `<span class="k">${esc(k)} gate</span><span class="dimmer">threshold ${num(v.gate_threshold, 3)}, observed ${pct(v.observed_verifier_rate)}</span>`).join('');
  $('#m-cost').innerHTML = `
    <span class="k">estimated total</span><span>$${num(c.estimated_total_usd, 6)}</span>
    <span class="k">per request</span><span>$${(c.estimated_cost_per_request_usd || 0).toFixed(8)}</span>
    <span class="k">verifier token share</span><span>${pct(c.verifier_token_share)}</span>
    <span class="k">verifier tokens</span><span>${c.verifier_tokens_total}</span>`;
  $('#m-calib').innerHTML = m.calibration && m.calibration.n ? `
    <span class="k">Brier score</span><span>${num(m.calibration.brier, 4)}</span>
    <span class="k">expected calibration error</span><span>${num(m.calibration.ece, 4)}</span>
    <span class="k">n / base rate</span><span>${m.calibration.n} / ${num(m.calibration.base_rate)}</span>`
    : '<span class="k dimmer">not enough labelled traffic yet</span><span></span>';
}

/* ---------------------------------------------------------------- queue */
async function loadQueue() {
  const q = await (await fetch('/v1/control/queue')).json();
  $('#q-count').textContent = q.total ? `${q.total} open` : '';
  if (!q.items.length) { $('#queue').innerHTML = '<div class="empty">Nothing awaiting review.</div>'; return; }
  $('#queue').innerHTML = q.items.map(i => `
    <div class="claim" data-id="${i.request_id}">
      <div class="claim-top">
        <span class="pill ${i.decision}">${i.decision}</span>
        <div class="claim-txt"><b>${esc(i.use_case)}</b>
          <span class="mono dimmer">${esc(i.request_id)}</span><br>
          <span class="muted" style="font-size:12px">${esc((i.escalation_reasons || [])[0] || '')}</span>
        </div>
        <span class="mono dimmer">conf ${num(i.confidence)}</span>
      </div>
      <div class="evidence">${esc((i.original_text || '').slice(0, 220))}</div>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="act ok" data-v="confirm" data-id="${i.request_id}">Confirm — flag was right</button>
        <button class="act no" data-v="override" data-id="${i.request_id}">Override — this was fine</button>
      </div>
    </div>`).join('');
  $$('#queue button[data-v]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    await fetch('/v1/control/override', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ request_id: b.dataset.id, verdict: b.dataset.v, operator: 'dashboard' })
    });
    loadQueue();
  });
}

$('#btn-retrain').onclick = async () => {
  const btn = $('#btn-retrain');
  btn.disabled = true; $('#retrain-status').textContent = 'fitting…';
  const out = await (await fetch('/v1/control/retrain', { method: 'POST' })).json();
  btn.disabled = false;
  $('#retrain-status').textContent =
    `${out.training.corpus_rows} corpus + ${out.training.human_rows} human rows`;
  const lanes = Object.keys(out.after);
  $('#retrain-out').innerHTML = lanes.map(ln => {
    const d = out.deltas[ln] || {};
    const rows = Object.entries(d).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 6);
    if (!rows.length) return '';
    return `<div style="margin-bottom:12px"><b>${esc(ln)}</b>
      <table style="margin-top:5px"><thead><tr><th>feature</th><th class="right">before</th>
        <th class="right">after</th><th class="right">Δ</th></tr></thead><tbody>
      ${rows.map(([k, v]) => {
        const b0 = k === '__bias__' ? out.before[ln].bias : out.before[ln].weights[k];
        const a0 = k === '__bias__' ? out.after[ln].bias : out.after[ln].weights[k];
        return `<tr><td class="mono">${esc(k)}</td>
          <td class="right mono">${num(b0, 4)}</td><td class="right mono">${num(a0, 4)}</td>
          <td class="right mono ${v > 0 ? 'delta-pos' : 'delta-neg'}">${v > 0 ? '+' : ''}${num(v, 4)}</td></tr>`;
      }).join('')}</tbody></table></div>`;
  }).join('') + (out.replay && out.replay.length ? `
    <div><b>Effect on the corrected requests</b>
    <table style="margin-top:5px"><thead><tr><th>request</th><th>lane</th>
      <th class="right">p before</th><th class="right">p after</th><th class="right">Δ</th></tr></thead><tbody>
    ${out.replay.flatMap(e => Object.entries(e.lanes).map(([ln, v]) =>
      `<tr><td class="mono">${esc(e.request_id)}</td><td>${esc(ln)}</td>
        <td class="right mono">${num(v.p_before, 4)}</td><td class="right mono">${num(v.p_after, 4)}</td>
        <td class="right mono ${v.delta > 0 ? 'delta-pos' : 'delta-neg'}">${v.delta > 0 ? '+' : ''}${num(v.delta, 4)}</td></tr>`
    )).join('')}</tbody></table>
    <p class="muted" style="font-size:12px">One correction against a 65-row corpus moves a weight
    by ~0.03 and usually will not flip a decision by itself — which is the correct behaviour.</p></div>`
    : '<p class="muted" style="font-size:12px">No change — these labels are already in the fit.</p>');
  loadMetrics();
};

/* --------------------------------------------------------------- policy */
async function updateThresholds() {
  const lam = +$('#lam').value, a = +$('#alpha').value, e = +$('#eta').value;
  $('#lam-v').textContent = `λ = ${lam.toFixed(1)}`;
  $('#alpha-v').textContent = `α = ${a.toFixed(2)}`;
  $('#eta-v').textContent = `η = ${e.toFixed(2)}`;
  const r = await (await fetch(`/api/thresholds?lam=${lam}&hedge_cost=${a}&hedge_efficacy=${e}`)).json();
  $('#thr-out').innerHTML = `
    <span class="k">GREEN → YELLOW at p ≥</span><span class="mono">${num(r.threshold_yellow, 4)}</span>
    <span class="k">YELLOW → RED at p ≥</span><span class="mono">${num(r.threshold_red, 4)}</span>`;
  const y = r.threshold_yellow * 100, rd = r.threshold_red * 100;
  $('#thr-bar').innerHTML =
    `<div class="seg" style="width:${y}%;background:var(--green)"></div>
     <div class="seg" style="width:${rd - y}%;background:var(--yellow)"></div>
     <div class="seg" style="width:${100 - rd}%;background:var(--red)"></div>`;
}
['lam', 'alpha', 'eta'].forEach(id => $('#' + id).oninput = updateThresholds);

async function loadPolicies() {
  updateThresholds();
  const p = await (await fetch('/api/policies')).json();
  $('#policies').innerHTML = Object.entries(p.policies).map(([name, pol]) => `
    <div class="card">
      <h3>${esc(name)} <span class="sub">v${esc(pol.version)}${pol.overlays.length ? ' + ' + esc(pol.overlays.join(',')) : ''}</span></h3>
      <p class="muted" style="font-size:12px;margin-top:0">${esc(pol.description)}</p>
      <table><thead><tr><th>lane</th><th class="right">λ</th><th class="right">α</th>
        <th class="right">η</th><th class="right">t_yellow</th><th class="right">t_red</th></tr></thead><tbody>
      ${Object.entries(pol.lanes).map(([ln, l]) => `<tr><td>${esc(ln)}</td>
        <td class="right mono">${l.lambda}</td><td class="right mono">${l.hedge_cost}</td>
        <td class="right mono">${l.hedge_efficacy}</td>
        <td class="right mono warn">${num(l.threshold_yellow, 3)}</td>
        <td class="right mono bad">${num(l.threshold_red, 3)}</td></tr>`).join('')}
      </tbody></table>
      <div class="kv" style="margin-top:8px">
        <span class="k">inline budget</span><span>${pol.latency.hold_budget_ms}ms</span>
        <span class="k">streams at tiers</span><span>${pol.latency.stream_tiers.join(', ') || '<span class="dimmer">never</span>'}</span>
        <span class="k">verifier target rate</span><span>${pct(pol.adaptive.target_verifier_rate)}</span>
        ${pol.hard_rules.length ? `<span class="k">hard rules</span>
          <span class="warn">${pol.hard_rules.map(h => esc(h.reason)).join('<br>')}</span>` : ''}
      </div>
    </div>`).join('');
}

/* ---------------------------------------------------------------- audit */
async function loadAudit() {
  const a = await (await fetch('/api/audit?limit=120')).json();
  $('#a-integrity').innerHTML = a.integrity.ok
    ? `<span class="good">chain intact — ${a.total} records</span>`
    : `<span class="bad">BROKEN at ${a.integrity.broken_at}: ${esc(a.integrity.reason)}</span>`;
  $('#audit').innerHTML = a.items.map(i => `<tr>
    <td class="mono">${i.seq}</td><td class="mono">${esc(i.kind)}</td>
    <td class="mono dimmer">${esc(i.request_id)}</td><td>${esc(i.summary)}</td>
    <td class="mono dimmer">${esc(i.prev_hash)}…</td><td class="mono">${esc(i.hash)}…</td></tr>`).join('');
}

/* ------------------------------------------------------------ bootstrap */
(async function init() {
  await loadHealth();
  const [d, deep] = await Promise.all([
    (await fetch('/api/decisions?limit=100')).json(),
    (await fetch('/api/deep_audits?limit=200')).json(),
  ]);
  d.items.forEach(x => STATE.decisions.set(x.request_id, x));
  deep.items.forEach(x => STATE.deep.set(x.request_id, x));
  renderFeed();
  connect();
  setInterval(loadHealth, 15000);
})();
