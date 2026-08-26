'use strict';

/* minidsp-gui front-end.
 *
 * The project object held here is the source of truth for DSP settings --
 * the device cannot be read back for filter coefficients. Edits mutate the
 * project, POST it to /api/project, and only reach the hardware when the
 * user presses "Apply to device".
 */

const state = {
  device: null,
  project: null,
  sel: { kind: 'output', index: 0 },
  masterMuted: false,
  applying: false,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids) if (kid) n.append(kid);
  return n;
};

function flash(msg, kind = 'ok') {
  const bar = $('#status-bar');
  bar.textContent = msg;
  bar.className = kind;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => { bar.className = 'hidden'; }, 4000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

const saveProject = (() => {
  let pending = null;
  return () => {
    clearTimeout(pending);
    pending = setTimeout(() => {
      api('/api/project', {
        method: 'POST',
        body: JSON.stringify(state.project),
      }).catch((e) => flash(`save failed: ${e.message}`, 'error'));
    }, 250);
  };
})();

/* ---------------- channel helpers ---------------- */

const currentBank = () =>
  state.sel.kind === 'output' ? state.project.outputs : state.project.inputs;
const currentChan = () => currentBank()[state.sel.index];

function touched() {
  saveProject();
  renderSidebar();
  renderPlot();
}

/* ---------------- sidebar ---------------- */

function renderSidebar() {
  if (!state.project) return;
  const build = (kind, listEl) => {
    listEl.replaceChildren();
    state.project[kind + 's'].forEach((ch, i) => {
      const active = state.sel.kind === kind && state.sel.index === i;
      const bits = [];
      if (kind === 'output') {
        const xo = ch.crossover.filter((g) => g.enabled || g.manual).length;
        const pq = ch.peq.filter((b) => b.enabled || b.manual).length;
        if (xo) bits.push(`${xo}x`);
        if (pq) bits.push(`${pq}q`);
      } else {
        const pq = ch.peq.filter((b) => b.enabled || b.manual).length;
        if (pq) bits.push(`${pq}q`);
      }
      listEl.append(el('li', {
        class: [active ? 'active' : '', ch.mute ? 'muted-chan' : ''].join(' '),
        onclick: () => { state.sel = { kind, index: i }; renderAll(); },
      },
        el('span', { text: ch.name }),
        el('span', { class: 'tag', text: bits.join(' ') }),
      ));
    });
  };
  build('output', $('#output-list'));
  build('input', $('#input-list'));
}

/* ---------------- detail ---------------- */

function numField(label, value, onChange, opts = {}) {
  const input = el('input', {
    type: 'number', value,
    step: opts.step ?? 0.1,
    min: opts.min, max: opts.max,
    onchange: (e) => onChange(parseFloat(e.target.value)),
  });
  return el('div', { class: 'field' }, el('label', { text: label }), input);
}

function selectField(label, value, options, onChange) {
  const sel = el('select', { onchange: (e) => onChange(e.target.value) });
  for (const opt of options) {
    const [val, text] = Array.isArray(opt) ? opt : [opt, opt];
    const o = el('option', { value: val, text });
    if (String(val) === String(value)) o.selected = true;
    sel.append(o);
  }
  return el('div', { class: 'field' }, el('label', { text: label }), sel);
}

function toggleBtn(label, on, onChange) {
  return el('button', {
    class: 'toggle' + (on ? ' on' : ''),
    text: label,
    onclick: () => onChange(!on),
  });
}

function renderBasics(ch) {
  const row = el('div', { class: 'row' });
  row.append(numField('Gain (dB)', ch.gain, (v) => {
    ch.gain = clamp(v, -127, 12); touched(); renderDetail();
  }, { step: 0.5, min: -127, max: 12 }));

  if (state.sel.kind === 'output') {
    row.append(numField('Delay (ms)', ch.delay, (v) => {
      ch.delay = Math.max(0, v); touched();
    }, { step: 0.01, min: 0 }));
  }

  row.append(el('div', { class: 'field' },
    el('label', { text: 'Channel' }),
    el('div', { class: 'row' },
      toggleBtn('Mute', ch.mute, (v) => { ch.mute = v; touched(); renderDetail(); }),
      state.sel.kind === 'output'
        ? toggleBtn('Invert', ch.invert, (v) => { ch.invert = v; touched(); renderDetail(); })
        : null,
    )));

  return card(`${ch.name} — basics`, row, () => {
    const name = prompt('Channel name', ch.name);
    if (name) { ch.name = name; touched(); renderDetail(); }
  });
}

function card(title, body, onRename) {
  const h = el('h3', {}, el('span', { text: title }));
  if (onRename) h.append(el('button', { text: 'Rename', onclick: onRename }));
  return el('div', { class: 'card' }, h, el('div', { class: 'card-body' }, body));
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, isNaN(v) ? lo : v));

function renderCrossover(ch) {
  const table = el('table', { class: 'grid' });
  table.append(el('tr', {},
    ...['', 'Mode', 'Alignment', 'Slope', 'Freq (Hz)', ''].map((t) =>
      el('th', { text: t }))));

  ch.crossover.forEach((g) => {
    const tr = el('tr', { class: g.enabled ? '' : 'off' });
    tr.append(el('td', {}, toggleBtn(g.enabled ? 'On' : 'Off', g.enabled,
      (v) => { g.enabled = v; touched(); renderDetail(); })));

    tr.append(el('td', {}, selectField('', g.mode,
      [['highpass', 'High-pass'], ['lowpass', 'Low-pass']],
      (v) => { g.mode = v; touched(); renderDetail(); })));

    tr.append(el('td', {}, selectField('', g.alignment,
      [['linkwitz-riley', 'Linkwitz-Riley'], ['butterworth', 'Butterworth'],
       ['bessel', 'Bessel']],
      (v) => { g.alignment = v; touched(); renderDetail(); })));

    const orders = g.alignment === 'linkwitz-riley'
      ? [[2, 'LR12'], [4, 'LR24'], [8, 'LR48']]
      : g.alignment === 'bessel'
        ? [[2, '12 dB'], [4, '24 dB'], [6, '36 dB'], [8, '48 dB']]
        : [[1, '6 dB'], [2, '12 dB'], [3, '18 dB'], [4, '24 dB'],
           [6, '36 dB'], [8, '48 dB']];
    tr.append(el('td', {}, selectField('', g.order, orders,
      (v) => { g.order = parseInt(v, 10); touched(); renderDetail(); })));

    tr.append(el('td', {}, el('input', {
      type: 'number', value: g.freq, step: 1, min: 10, max: 24000,
      onchange: (e) => {
        g.freq = clamp(parseFloat(e.target.value), 10, 24000);
        touched();
      },
    })));

    tr.append(el('td', {}, g.manual
      ? el('button', {
          text: 'Clear manual',
          onclick: () => { g.manual = null; touched(); renderDetail(); },
        })
      : el('span', { class: 'muted', text: '' })));

    table.append(tr);
  });

  const note = el('p', { class: 'muted', text:
    'Two groups of 4 biquads each. A slope needing more than 4 sections is ' +
    'rejected on apply rather than silently truncated.' });

  return card('Crossover', el('div', {}, table, note));
}

function renderPeq(ch) {
  const table = el('table', { class: 'grid' });
  table.append(el('tr', {},
    ...['#', '', 'Type', 'Freq (Hz)', 'Q', 'Gain (dB)', ''].map((t) =>
      el('th', { text: t }))));

  ch.peq.forEach((b, i) => {
    const tr = el('tr', { class: (b.enabled || b.manual) ? '' : 'off' });
    tr.append(el('td', { text: String(i + 1) }));
    tr.append(el('td', {}, toggleBtn(b.enabled ? 'On' : 'Off', b.enabled,
      (v) => { b.enabled = v; touched(); renderDetail(); })));

    if (b.manual) {
      tr.append(el('td', { colspan: '4' },
        el('span', { class: 'muted', text: 'manual coefficients (REW import)' })));
    } else {
      tr.append(el('td', {}, selectField('', b.type,
        [['peaking', 'Peaking'], ['lowshelf', 'Low shelf'],
         ['highshelf', 'High shelf'], ['lowpass', 'Low-pass'],
         ['highpass', 'High-pass'], ['notch', 'Notch'],
         ['allpass', 'All-pass'], ['bandpass', 'Band-pass']],
        (v) => { b.type = v; touched(); renderDetail(); })));
      tr.append(el('td', {}, el('input', {
        type: 'number', value: b.freq, step: 1, min: 10, max: 24000,
        onchange: (e) => { b.freq = clamp(parseFloat(e.target.value), 10, 24000); touched(); },
      })));
      tr.append(el('td', {}, el('input', {
        type: 'number', value: b.q, step: 0.01, min: 0.1, max: 20,
        onchange: (e) => { b.q = clamp(parseFloat(e.target.value), 0.1, 20); touched(); },
      })));
      tr.append(el('td', {}, el('input', {
        type: 'number', value: b.gain, step: 0.1, min: -24, max: 24,
        onchange: (e) => { b.gain = clamp(parseFloat(e.target.value), -24, 24); touched(); },
      })));
    }

    tr.append(el('td', {}, b.manual
      ? el('button', { text: 'Clear', onclick: () => {
          b.manual = null; touched(); renderDetail();
        } })
      : el('span', { class: 'muted', text: '' })));
    table.append(tr);
  });

  return card('Parametric EQ', table);
}

function renderRouting(ch) {
  const table = el('table', { class: 'grid' });
  table.append(el('tr', {},
    ...['To output', 'On', 'Gain (dB)'].map((t) => el('th', { text: t }))));
  ch.routing.forEach((r) => {
    const name = state.project.outputs[r.index]?.name ?? `Out ${r.index + 1}`;
    const tr = el('tr', { class: r.enabled ? '' : 'off' });
    tr.append(el('td', { text: name }));
    tr.append(el('td', {}, toggleBtn(r.enabled ? 'On' : 'Off', r.enabled,
      (v) => { r.enabled = v; touched(); renderDetail(); })));
    tr.append(el('td', {}, el('input', {
      type: 'number', value: r.gain, step: 0.5, min: -127, max: 12,
      onchange: (e) => { r.gain = clamp(parseFloat(e.target.value), -127, 12); touched(); },
    })));
    table.append(tr);
  });
  return card('Routing', table);
}

function renderCompressor(ch) {
  const c = ch.compressor;
  const row = el('div', { class: 'row' });
  row.append(el('div', { class: 'field' },
    el('label', { text: 'Enabled' }),
    toggleBtn(c.enabled ? 'On' : 'Off', c.enabled,
      (v) => { c.enabled = v; touched(); renderDetail(); })));
  row.append(numField('Threshold (dB)', c.threshold,
    (v) => { c.threshold = v; touched(); }, { step: 0.5 }));
  row.append(numField('Ratio', c.ratio, (v) => { c.ratio = v; touched(); },
    { step: 0.1, min: 1 }));
  row.append(numField('Attack (ms)', c.attack, (v) => { c.attack = v; touched(); },
    { step: 1, min: 0 }));
  row.append(numField('Release (ms)', c.release, (v) => { c.release = v; touched(); },
    { step: 1, min: 0 }));
  return card('Compressor', row);
}

function renderRewImport() {
  const ta = el('textarea', {
    placeholder: 'Paste REW biquad export here:\n\nbiquad1,\nb0=…,\nb1=…,\nb2=…,\na1=…,\na2=…,',
  });
  const btn = el('button', {
    text: 'Import into this channel’s PEQ',
    onclick: async () => {
      try {
        const r = await api('/api/import-rew', {
          method: 'POST',
          body: JSON.stringify({
            text: ta.value,
            target: state.sel.kind,
            index: state.sel.index,
          }),
        });
        state.project = await api('/api/project');
        flash(`imported ${r.applied} biquad(s)` +
              (r.skipped ? `, ${r.skipped} did not fit` : ''));
        renderAll();
      } catch (e) { flash(e.message, 'error'); }
    },
  });
  return card('REW import', el('div', {}, ta, el('div', { class: 'row' }, btn)));
}

function renderPlotCard() {
  const canvas = el('canvas', { class: 'plot', id: 'plot' });
  return card('Response (this channel)', canvas);
}

function renderDetail() {
  if (!state.project) return;
  const ch = currentChan();
  if (!ch) return;
  const host = $('#detail');
  host.replaceChildren();
  host.append(renderBasics(ch));
  host.append(renderPlotCard());
  if (state.sel.kind === 'output') host.append(renderCrossover(ch));
  host.append(renderPeq(ch));
  if (state.sel.kind === 'input') host.append(renderRouting(ch));
  if (state.sel.kind === 'output') host.append(renderCompressor(ch));
  host.append(renderRewImport());
  renderPlot();
}

/* ---------------- response plot ---------------- */

async function renderPlot() {
  const canvas = $('#plot');
  if (!canvas || !state.project) return;
  const ch = currentChan();
  let data;
  try {
    data = await api('/api/preview', {
      method: 'POST',
      body: JSON.stringify({
        peq: ch.peq,
        crossover: state.sel.kind === 'output' ? ch.crossover : [],
      }),
    });
  } catch { return; }

  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);

  const DB_MIN = -30, DB_MAX = 15;
  const fx = (f) => (Math.log10(f) - Math.log10(20)) /
                    (Math.log10(20000) - Math.log10(20)) * w;
  const fy = (db) => h - ((db - DB_MIN) / (DB_MAX - DB_MIN)) * h;

  g.strokeStyle = '#333844'; g.lineWidth = 1; g.font = '10px monospace';
  g.fillStyle = '#8b93a3';
  for (const f of [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]) {
    const x = fx(f);
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    g.fillText(f >= 1000 ? `${f / 1000}k` : `${f}`, x + 2, h - 2);
  }
  for (let db = DB_MIN; db <= DB_MAX; db += 15) {
    const y = fy(db);
    g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke();
    g.fillText(`${db}`, 2, y - 2);
  }
  g.strokeStyle = '#4a5163';
  g.beginPath(); g.moveTo(0, fy(0)); g.lineTo(w, fy(0)); g.stroke();

  g.strokeStyle = '#4f9cf9'; g.lineWidth = 2; g.beginPath();
  data.freqs.forEach((f, i) => {
    const x = fx(f), y = fy(Math.max(DB_MIN, Math.min(DB_MAX, data.db[i])));
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  });
  g.stroke();
}

/* ---------------- meters + master ---------------- */

function meterRow(i) {
  const fill = el('div', { class: 'fill' });
  const db = el('div', { class: 'db', text: '-∞' });
  const row = el('div', { class: 'meter' },
    el('div', { class: 'n', text: String(i + 1) }),
    el('div', { class: 'bar' }, fill),
    db);
  row._set = (v) => {
    const pct = Math.max(0, Math.min(1, (v + 60) / 60)) * 100;
    fill.style.width = `${pct}%`;
    fill.className = 'fill' + (v > -3 ? ' clip' : v > -12 ? ' hot' : '');
    db.textContent = v <= -119 ? '-∞' : v.toFixed(1);
  };
  return row;
}

function buildMeters() {
  const mk = (host, n) => {
    host.replaceChildren();
    host._rows = [];
    for (let i = 0; i < n; i++) {
      const r = meterRow(i);
      host._rows.push(r);
      host.append(r);
    }
  };
  mk($('#input-meters'), state.device.n_inputs);
  mk($('#output-meters'), state.device.n_outputs);
}

async function pollStatus() {
  try {
    const s = await api('/api/status');
    ($('#input-meters')._rows || []).forEach((r, i) =>
      r._set(s.input_levels?.[i] ?? -120));
    ($('#output-meters')._rows || []).forEach((r, i) =>
      r._set(s.output_levels?.[i] ?? -120));

    const vol = $('#master-volume');
    if (document.activeElement !== vol) {
      vol.value = s.master.volume;
      $('#master-volume-val').textContent = `${s.master.volume.toFixed(1)} dB`;
    }
    state.masterMuted = s.master.mute;
    $('#master-mute').className = 'toggle' + (s.master.mute ? ' on' : '');
    $('#master-preset').value = String(s.master.preset);

    const srcSel = $('#master-source');
    if (!srcSel.options.length && s.available_sources) {
      for (const src of s.available_sources) {
        srcSel.append(el('option', { value: src, text: src }));
      }
    }
    if (document.activeElement !== srcSel && s.master.source) {
      srcSel.value = String(s.master.source).toLowerCase();
    }
  } catch { /* daemon hiccup; next tick retries */ }
}

/* ---------------- snapshots ---------------- */

async function refreshSnapshots() {
  try {
    const { snapshots } = await api('/api/snapshots');
    const list = $('#snapshot-list');
    list.replaceChildren();
    for (const name of snapshots) {
      list.append(el('li', {
        text: name.replace(/\.json$/, ''),
        title: 'Click to restore',
        onclick: async () => {
          if (!confirm(`Restore ${name}? Current project is replaced.`)) return;
          const r = await api('/api/snapshots/restore', {
            method: 'POST', body: JSON.stringify({ name }),
          });
          state.project = r.project;
          flash(`restored ${name} — press Apply to send it to the device`);
          renderAll();
        },
      }));
    }
  } catch { /* non-fatal */ }
}

/* ---------------- wiring ---------------- */

function renderAll() {
  renderSidebar();
  renderDetail();
}

async function init() {
  try {
    state.device = await api('/api/device');
    state.project = await api('/api/project');
  } catch (e) {
    flash(`cannot reach backend: ${e.message}`, 'error');
    return;
  }

  $('#device-info').textContent =
    `${state.device.product_name} · sn ${state.device.serial} · ` +
    `${state.device.n_inputs}in/${state.device.n_outputs}out · ` +
    `${state.device.rate} Hz`;

  buildMeters();
  renderAll();
  refreshSnapshots();

  $('#master-volume').addEventListener('input', (e) => {
    $('#master-volume-val').textContent =
      `${parseFloat(e.target.value).toFixed(1)} dB`;
  });
  $('#master-volume').addEventListener('change', (e) => {
    api('/api/master', {
      method: 'POST',
      body: JSON.stringify({ volume: parseFloat(e.target.value) }),
    }).catch((err) => flash(err.message, 'error'));
  });

  $('#master-mute').addEventListener('click', () => {
    api('/api/master', {
      method: 'POST', body: JSON.stringify({ mute: !state.masterMuted }),
    }).catch((err) => flash(err.message, 'error'));
  });

  $('#master-source').addEventListener('change', (e) => {
    api('/api/master', {
      method: 'POST', body: JSON.stringify({ source: e.target.value }),
    }).catch((err) => flash(err.message, 'error'));
  });

  $('#master-preset').addEventListener('change', (e) => {
    api('/api/master', {
      method: 'POST', body: JSON.stringify({ preset: parseInt(e.target.value, 10) }),
    }).catch((err) => flash(err.message, 'error'));
  });

  $('#panic').addEventListener('click', () => {
    api('/api/mute-all', { method: 'POST' })
      .then(() => flash('MUTED'))
      .catch((err) => flash(err.message, 'error'));
  });

  $('#apply').addEventListener('click', async () => {
    if (state.applying) return;
    state.applying = true;
    $('#apply').disabled = true;
    try {
      const r = await api('/api/apply', { method: 'POST' });
      flash(`applied to ${r.outputs} output(s)`);
    } catch (e) {
      flash(`apply failed: ${e.message}`, 'error');
    } finally {
      state.applying = false;
      $('#apply').disabled = false;
    }
  });

  $('#snap-save').addEventListener('click', async () => {
    const name = $('#snap-name').value.trim() || 'snapshot';
    await api('/api/snapshots', {
      method: 'POST', body: JSON.stringify({ name }),
    });
    $('#snap-name').value = '';
    flash(`saved snapshot "${name}"`);
    refreshSnapshots();
  });

  pollStatus();
  setInterval(pollStatus, 500);
  window.addEventListener('resize', () => renderPlot());
}

init();
