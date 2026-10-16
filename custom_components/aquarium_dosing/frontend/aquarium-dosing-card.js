/* Aquarium Dosing cards
 *
 *  1. Editor (full card, for a config dashboard)
 *       type: custom:aquarium-dosing-card
 *       title: Dosing                              # optional
 *       master_entity: input_boolean.reef_dosing   # optional with one aquarium
 *
 *  2. Summary (compact, for a main display): status, next dose, delivered today
 *     and a 24 h dose timeline for every pump
 *       type: custom:aquarium-dosing-summary-card
 *       title: Dosing                              # optional
 *       master_entity: input_boolean.reef_dosing   # optional with one aquarium
 *       pumps: [alk, ca]                           # optional: only these pumps
 *
 * With several aquariums set master_entity to pick which one a card shows.
 * Schedule previews come from the same engine that runs the pumps.
 */
(() => {
"use strict";

const DAY = 1440;
const MODES = [
  ["interval", "Every N minutes"],
  ["times", "Fixed times"],
  ["daily_total", "Daily total, split evenly"],
];
const STATUS_TEXT = {
  idle: "Idle", disabled: "Schedule off", paused: "Paused", queued: "Queued",
  dosing: "Dosing", priming: "Priming", calibrating: "Calibrating", fault: "FAULT",
};
const BUSY = ["queued", "dosing", "priming", "calibrating"];
const STATE_COLOR = {
  done: "var(--success-color, #43a047)",
  pending: "var(--primary-color)",
  skipped: "var(--warning-color, #f9a825)",
  missed: "var(--error-color, #db4437)",
  failed: "var(--error-color, #db4437)",
  interrupted: "var(--error-color, #db4437)",
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const toMin = (hhmm) => {
  const [h, m] = String(hhmm).split(":").map(Number);
  return h * 60 + m;
};
const num = (v, d = 2) => (Number.isFinite(+v) ? +(+v).toFixed(d) : "-");
const hhmm = (iso) => (iso ? iso.slice(11, 16) : "-");
/** "10:15", "tomorrow 08:00" or "10-20 08:00" relative to the HA 'now' string. */
const when = (iso, nowIso) => {
  if (!iso) return "-";
  const day = iso.slice(0, 10), today = nowIso.slice(0, 10);
  if (day === today) return hhmm(iso);
  const tomorrow = new Date(Date.parse(today + "T12:00:00Z") + 864e5).toISOString().slice(0, 10);
  return day === tomorrow ? `tomorrow ${hhmm(iso)}` : `${day.slice(5)} ${hhmm(iso)}`;
};

const CSS_COMMON = `
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
          background: var(--secondary-background-color); font-size: .85rem; white-space: nowrap; }
  .chip.fault { background: var(--error-color); color: #fff; }
  .chip.busy { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .chip.muted { color: var(--secondary-text-color); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  svg { width: 100%; height: auto; display: block; }
  .err { color: var(--error-color); font-size: .85rem; min-height: 1.2em; }
  .muted { color: var(--secondary-text-color); font-size: .85rem; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: .8rem; color: var(--secondary-text-color); margin-top: 4px; }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }`;

/* ----------------------------------------------------------------- timeline */
/** Day strip: operating window band, one bar per dose (height = volume), "now" line. */
function timelineSvg({ slots, cfg, nowMin, height = 110, labels = true }) {
  const W = 720, L = 12, R = 12, T = 8, B = labels ? 22 : 6, H = height;
  const x = (m) => L + (m / DAY) * (W - L - R);
  let out = "";
  const band = (a, b, op) => `<rect x="${x(a)}" y="${T}" width="${x(b) - x(a)}" height="${H - T - B}" fill="var(--primary-color)" opacity="${op}"/>`;
  if (cfg.all_hours) out += band(0, DAY, 0.06);
  else {
    const s = toMin(cfg.window_start), e = toMin(cfg.window_end);
    out += s < e ? band(s, e, 0.14) : band(0, e, 0.14) + band(s, DAY, 0.14);
  }
  for (let h = 0; h <= 24; h += 3) {
    out += `<line x1="${x(h * 60)}" x2="${x(h * 60)}" y1="${T}" y2="${H - B}" stroke="var(--divider-color)" stroke-width="1"/>`;
    if (labels) out += `<text x="${x(h * 60)}" y="${H - 6}" text-anchor="middle" font-size="11" fill="var(--secondary-text-color)">${String(h % 24).padStart(2, "0")}:00</text>`;
  }
  out += `<line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" stroke="var(--divider-color)" stroke-width="1"/>`;
  const maxV = Math.max(...slots.map((s) => s.volume), 1e-9);
  const barW = Math.max(2, Math.min(6, (W - L - R) / Math.max(slots.length, 1) / 2));
  for (const s of slots) {
    const h = (H - T - B) * (0.35 + 0.65 * (s.volume / maxV));
    out += `<rect x="${x(s.minute) - barW / 2}" y="${H - B - h}" width="${barW}" height="${h}" rx="1.5" fill="${STATE_COLOR[s.state] || STATE_COLOR.pending}"><title>${hhmm(s.at)}  ${num(s.volume)} mL  ${esc(s.state)}</title></rect>`;
  }
  out += `<line x1="${x(nowMin)}" x2="${x(nowMin)}" y1="${T}" y2="${H - B}" stroke="var(--primary-text-color)" stroke-width="1.5" stroke-dasharray="4 3"/>`;
  return out;
}

const LEGEND = `<div class="legend">
  <span><i style="background:${STATE_COLOR.done}"></i>done</span>
  <span><i style="background:${STATE_COLOR.pending}"></i>upcoming</span>
  <span><i style="background:${STATE_COLOR.skipped}"></i>skipped</span>
  <span><i style="background:${STATE_COLOR.missed}"></i>missed / failed</span>
  <span><i style="background:var(--primary-color);opacity:.25"></i>operating window</span></div>`;

const statusChip = (p) =>
  `<span class="chip ${p.status === "fault" ? "fault" : BUSY.includes(p.status) ? "busy" : ["disabled", "paused"].includes(p.status) ? "muted" : ""}">${esc(STATUS_TEXT[p.status] || p.status)}</span>`;

/* =========================================================== editor card */
class AquariumDosingCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._sel = null;
    this._draft = null;
    this._dirty = false;
    this._preview = null;
    this._error = "";
  }

  setConfig(config) {
    this._config = { title: "Aquarium Dosing", ...config };
  }
  getCardSize() {
    return 12;
  }
  static getStubConfig() {
    return {};
  }
  connectedCallback() {
    if (this._hass && this._data) this._poll();
  }
  disconnectedCallback() {
    clearTimeout(this._pollTimer);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._shell();
      this._load();
    }
    this._syncMaster();
  }

  /* ------------------------------------------------------------ data */
  _ws(msg) {
    if (this._config.master_entity) msg.master_entity = this._config.master_entity;
    return this._hass.callWS(msg);
  }

  async _load() {
    try {
      this._data = await this._ws({ type: "aquarium_dosing/get" });
    } catch (e) {
      this._root("fatal").textContent = `Could not reach the Aquarium Dosing integration: ${e.message || e.code || e}`;
      this._poll();
      return;
    }
    this._root("fatal").textContent = "";
    const names = Object.keys(this._data.pumps);
    if (!this._sel || !names.includes(this._sel)) {
      this._sel = names[0] || null;
      this._dirty = false;
    }
    if (!this._dirty) this._resetDraft();
    this._syncMaster();
    this._renderTabs();
    this._renderStatus();
    if (!this._dirty || !this._formBuilt) this._renderForm();
    this._drawTimeline(); // today's real outcomes straight away
    this._requestPreview();
    this._poll();
  }

  _poll() {
    clearTimeout(this._pollTimer);
    const busy = this._data && Object.values(this._data.pumps).some((p) => BUSY.includes(p.status));
    this._pollTimer = setTimeout(() => this._load(), busy ? 2000 : 30000);
  }

  _pump() {
    return this._data?.pumps?.[this._sel];
  }

  _resetDraft() {
    const p = this._pump();
    this._draft = p ? JSON.parse(JSON.stringify(p.config)) : null;
    this._dirty = false;
  }

  _payload() {
    const d = this._draft;
    const n = (v) => (v === "" || v == null ? 0 : Number(v));
    return {
      switch: d.switch || "", volume: n(d.volume), multiplier: n(d.multiplier), mode: d.mode,
      enabled: !!d.enabled, all_hours: !!d.all_hours, window_start: d.window_start, window_end: d.window_end,
      interval_minutes: n(d.interval_minutes), offset_minutes: n(d.offset_minutes), times: d.times || [],
      daily_total: n(d.daily_total), doses_per_day: n(d.doses_per_day), max_run_seconds: n(d.max_run_seconds),
      daily_cap: n(d.daily_cap), cooldown_minutes: n(d.cooldown_minutes),
    };
  }

  _requestPreview() {
    clearTimeout(this._previewTimer);
    if (!this._draft) {
      this._preview = null;
      this._drawTimeline();
      return;
    }
    this._previewTimer = setTimeout(async () => {
      try {
        this._preview = await this._hass.callWS({ type: "aquarium_dosing/preview", config: this._payload() });
        this._error = "";
      } catch (e) {
        this._preview = null;
        this._error = e.message || String(e);
      }
      this._drawTimeline();
    }, 150);
  }

  /* ----------------------------------------------------------- actions */
  async _call(msg, okText) {
    try {
      const snap = await this._ws(msg);
      if (okText) this._status(okText);
      return snap;
    } catch (e) {
      this._error = e.message || String(e);
      this._drawTimeline();
      return null;
    }
  }

  async _save() {
    const snap = await this._call({ type: "aquarium_dosing/set_pump", name: this._sel, config: this._payload() }, "Saved");
    if (snap) {
      this._dirty = false;
      this._error = "";
      await this._load();
    }
  }

  async _addPump() {
    const raw = prompt("Pump name (letters, numbers, underscores), e.g. alk, calcium, mg");
    if (!raw) return;
    const name = raw.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").slice(0, 32);
    if (!name) return;
    const snap = await this._call({ type: "aquarium_dosing/set_pump", name, config: { mode: "interval", interval_minutes: 60, enabled: false } }, "Pump added");
    if (snap) {
      this._sel = name;
      this._dirty = false;
      await this._load();
      this._renderForm();
    }
  }

  async _deletePump() {
    if (!confirm(`Delete pump "${this._sel}", its schedule and its dose history?`)) return;
    const snap = await this._call({ type: "aquarium_dosing/delete_pump", name: this._sel }, "Deleted");
    if (snap) {
      this._sel = null;
      this._dirty = false;
      await this._load();
      this._renderForm();
    }
  }

  _selectPump(name) {
    if (name === this._sel) return;
    if (this._dirty && !confirm("Discard unsaved changes?")) return;
    this._sel = name;
    this._resetDraft();
    this._error = "";
    this._renderTabs();
    this._renderStatus();
    this._renderForm();
    this._requestPreview();
  }

  async _doseNow() {
    const vol = this.shadowRoot.getElementById("dosevol")?.value;
    const ignore = this.shadowRoot.getElementById("ignorewin")?.checked || false;
    const msg = { type: "aquarium_dosing/dose", name: this._sel, ignore_window: ignore };
    if (vol) msg.volume = Number(vol);
    if (await this._call(msg, "Dose started")) this._load();
  }

  async _calDose() {
    if (!confirm("Run the 10 mL test dose now? Put the pump output into a measuring container first, not the tank.")) return;
    if (await this._call({ type: "aquarium_dosing/calibration_dose", name: this._sel }, "Test dose started")) this._load();
  }

  async _calApply() {
    const measured = Number(this.shadowRoot.getElementById("measured")?.value);
    if (!(measured > 0)) {
      this._error = "Enter the measured output in mL first";
      this._drawTimeline();
      return;
    }
    const before = this._pump().config.multiplier;
    const snap = await this._call({ type: "aquarium_dosing/calibrate", name: this._sel, measured_ml: measured }, "");
    if (snap) {
      const after = snap.pumps[this._sel].config.multiplier;
      this._draft.multiplier = after;
      this._status(`Calibrated: ${num(before, 3)} to ${num(after, 3)} sec/mL`);
      this._error = "";
      await this._load();
      this._renderForm();
    }
  }

  async _clearFault() {
    if (await this._call({ type: "aquarium_dosing/clear_fault", name: this._sel }, "Fault cleared")) this._load();
  }

  async _toggleMaster(on) {
    const entity = this._masterEntity();
    try {
      await this._hass.callService("input_boolean", on ? "turn_on" : "turn_off", { entity_id: entity });
    } catch (e) {
      this._status(`Could not toggle ${entity}: ${e.message || e}`);
    }
  }

  _markDirty() {
    this._dirty = true;
    const s = this._root("save"), r = this._root("revert");
    if (s) s.disabled = false;
    if (r) r.disabled = false;
  }

  /* ------------------------------------------------------------ events */
  _onInput(e) {
    const t = e.target;
    if (!this._draft) return;
    if (t.dataset.k) {
      const k = t.dataset.k;
      const v = t.type === "checkbox" ? t.checked : t.type === "number" ? (t.value === "" ? "" : Number(t.value)) : t.value;
      this._draft[k] = v;
      if (k === "mode") {
        const d = this._draft;
        if (v === "times" && !(d.times || []).length) d.times = [d.all_hours ? "12:00" : d.window_start];
        if (v === "daily_total" && !(Number(d.daily_total) > 0)) d.daily_total = Number(d.volume) * Number(d.doses_per_day || 1);
        this._renderForm();
      } else if (k === "all_hours") this._renderForm();
    } else if (t.dataset.t !== undefined) {
      this._draft.times[Number(t.dataset.t)] = t.value;
    } else return;
    this._markDirty();
    this._requestPreview();
  }

  _onClick(e) {
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const [act, arg] = btn.dataset.act.split(":");
    const redraw = () => {
      this._markDirty();
      this._renderForm();
      this._requestPreview();
    };
    if (act === "save") this._save();
    else if (act === "revert") {
      this._resetDraft();
      this._error = "";
      this._renderForm();
      this._requestPreview();
    } else if (act === "delete") this._deletePump();
    else if (act === "add") this._addPump();
    else if (act === "tab") this._selectPump(arg);
    else if (act === "dose") this._doseNow();
    else if (act === "caldose") this._calDose();
    else if (act === "calapply") this._calApply();
    else if (act === "clearfault") this._clearFault();
    else if (act === "addtime") {
      this._draft.times.push(this._draft.all_hours ? "12:00" : this._draft.window_start);
      redraw();
    } else if (act === "deltime") {
      this._draft.times.splice(Number(arg), 1);
      redraw();
    }
  }

  /* --------------------------------------------------------- rendering */
  _root(id) {
    return this.shadowRoot.getElementById(id);
  }

  _status(msg) {
    const el = this._root("status");
    if (!el) return;
    el.textContent = msg;
    clearTimeout(this._statusTimer);
    this._statusTimer = setTimeout(() => (el.textContent = ""), 4000);
  }

  _masterEntity() {
    return this._config.master_entity || this._data?.master_entity || "input_boolean.aquarium_dosing";
  }

  _shell() {
    this.shadowRoot.innerHTML = `
      <style>
        ${CSS_COMMON}
        ha-card { padding: 16px; display: block; }
        .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        .head h2 { margin: 0; font-size: 1.25rem; font-weight: 500; }
        .master { display: flex; align-items: center; gap: 8px; color: var(--secondary-text-color); font-size: .9rem; }
        .tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 10px; }
        button { font: inherit; cursor: pointer; border-radius: 8px; padding: 6px 14px; border: 1px solid var(--divider-color);
                 background: transparent; color: var(--primary-text-color); }
        button:focus-visible, input:focus-visible, select:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        button.sel, button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        button.danger { color: var(--error-color); }
        button:disabled { opacity: .45; cursor: default; }
        fieldset { border: 1px solid var(--divider-color); border-radius: 10px; margin: 12px 0 0; padding: 10px 12px 12px; min-width: 0; }
        legend { padding: 0 6px; font-size: .85rem; color: var(--secondary-text-color); }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
        label { display: flex; flex-direction: column; gap: 4px; font-size: .85rem; color: var(--secondary-text-color); }
        label.inline { flex-direction: row; align-items: center; gap: 8px; }
        input, select { font: inherit; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--divider-color);
                        background: var(--card-background-color); color: var(--primary-text-color); min-width: 0; }
        input:disabled { opacity: .5; }
        .times { display: grid; gap: 6px; margin: 6px 0; }
        .time { display: flex; gap: 8px; align-items: center; }
        .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; align-items: center; }
        .spacer { flex: 1; }
        .banner { margin-top: 10px; padding: 10px 12px; border-radius: 8px; background: var(--error-color); color: #fff;
                  display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; }
        .banner button { color: #fff; border-color: #fff; }
        table { width: 100%; border-collapse: collapse; font-size: .82rem; margin-top: 6px; }
        td { padding: 3px 6px; border-top: 1px solid var(--divider-color); }
        #fatal { color: var(--error-color); }
      </style>
      <ha-card>
        <div class="head">
          <h2>${esc(this._config.title)}</h2>
          <label class="master"><span id="masterlabel">Dosing</span><ha-switch id="master"></ha-switch></label>
        </div>
        <div id="fatal"></div>
        <div class="tabs" id="tabs"></div>
        <div id="statusbox"></div>
        <div id="form"></div>
        <svg id="timeline" viewBox="0 0 720 120" role="img" aria-label="Doses over 24 hours"></svg>
        ${LEGEND}
        <div class="err" id="err"></div>
        <div class="muted" id="status"></div>
        <div id="history"></div>
      </ha-card>`;
    const sw = this._root("master");
    sw.addEventListener("change", () => this._toggleMaster(sw.checked));
    this._root("form").addEventListener("input", (e) => this._onInput(e));
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
  }

  _syncMaster() {
    if (!this._root("master")) return;
    const entity = this._masterEntity();
    const st = this._hass.states[entity];
    const sw = this._root("master");
    sw.checked = st?.state === "on";
    sw.disabled = !st;
    this._root("masterlabel").textContent = !st
      ? `Helper ${entity} not found. Create a Toggle helper with that entity id`
      : st.state === "on"
      ? "Scheduled dosing on"
      : "Scheduled dosing paused";
  }

  _renderTabs() {
    const names = Object.keys(this._data.pumps);
    this._root("tabs").innerHTML =
      names.map((n) => `<button data-act="tab:${esc(n)}" class="${n === this._sel ? "sel" : ""}">${esc(n)}</button>`).join("") +
      `<button data-act="add">+ Add pump</button>`;
  }

  _renderStatus() {
    const p = this._pump();
    const box = this._root("statusbox");
    if (!p) {
      box.innerHTML = "";
      this._root("history").innerHTML = "";
      return;
    }
    const now = this._data.now;
    box.innerHTML = `
      <div class="row">${statusChip(p)}
        <span class="chip">Next ${esc(when(p.next_dose, now))}</span>
        <span class="chip">Last ${esc(when(p.last_dose, now))}</span>
        <span class="chip">24 h ${num(p.delivered_24h)} of ${num(p.daily_cap)} mL</span></div>
      ${p.fault ? `<div class="banner"><span><b>Fault:</b> ${esc(p.fault.reason)}. Dosing is blocked for this pump.</span>
        <button data-act="clearfault">Clear fault</button></div>` : ""}`;
    const hist = (p.history || []).slice(0, 8)
      .map((h) => `<tr><td>${esc(h.ts.slice(5, 16).replace("T", " "))}</td><td>${esc(h.result)}</td><td>${num(h.volume)} mL</td><td>${esc(h.source || "")}</td><td>${esc(h.reason || "")}</td></tr>`)
      .join("");
    this._root("history").innerHTML = hist ? `<div class="muted" style="margin-top:12px">Recent activity</div><table>${hist}</table>` : "";
  }

  _renderForm() {
    const el = this._root("form");
    const d = this._draft;
    const p = this._pump();
    this._formBuilt = true;
    if (!d) {
      el.innerHTML = `<p class="muted">No pumps yet. Add one to start building a schedule.</p>`;
      return;
    }
    const f = (k, label, type = "number", extra = "") =>
      `<label>${label}<input data-k="${k}" type="${type}" value="${esc(d[k])}" ${extra}></label>`;
    const vol = (k, label) => f(k, label, "number", 'min="0" step="0.01"');
    const modeFields =
      d.mode === "interval"
        ? `<div class="grid">${vol("volume", "Dose volume (mL)")}${f("interval_minutes", "Every (minutes)", "number", 'min="1" step="1"')}${f("offset_minutes", "Offset (minutes after start)", "number", 'min="0" step="1"')}</div>
           <div class="muted">First dose at the start of the operating window plus the offset (midnight if all hours), then every interval. Give two pumps different offsets so they never dose together.</div>`
        : d.mode === "times"
        ? `<div class="grid">${vol("volume", "Dose volume (mL)")}</div>
           <div class="times">${(d.times || []).map((t, i) => `<div class="time"><input type="time" data-t="${i}" value="${esc(t)}"><button data-act="deltime:${i}" title="Remove">Remove</button></div>`).join("")}</div>
           <button data-act="addtime">Add time</button>
           <div class="muted">Each time must be inside the operating window.</div>`
        : `<div class="grid">${vol("daily_total", "Daily total (mL)")}${f("doses_per_day", "Doses per day", "number", 'min="1" step="1"')}${f("offset_minutes", "Offset (minutes after start)", "number", 'min="0" step="1"')}</div>
           <div class="muted">Spread evenly from the start of the operating window across its length: ${num(this._preview?.manual_volume ?? (d.daily_total / (d.doses_per_day || 1)))} mL per dose.</div>`;
    const cal = p?.calibration;
    const mlps = Number(d.multiplier) > 0 ? num(1 / Number(d.multiplier), 3) : "-";
    el.innerHTML = `
      <fieldset><legend>Schedule</legend>
        <div class="grid">
          <label class="inline"><input data-k="enabled" type="checkbox" ${d.enabled ? "checked" : ""}> Schedule enabled</label>
          <label>Mode<select data-k="mode">${MODES.map(([v, l]) => `<option value="${v}" ${v === d.mode ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        </div>
        <div style="margin-top:10px">${modeFields}</div>
      </fieldset>
      <fieldset><legend>Operating window (when this pump may dose)</legend>
        <label class="inline"><input data-k="all_hours" type="checkbox" ${d.all_hours ? "checked" : ""}> Allow dosing at all hours</label>
        <div class="grid" style="margin-top:8px">
          ${f("window_start", "Window start", "time", d.all_hours ? "disabled" : "")}${f("window_end", "Window end", "time", d.all_hours ? "disabled" : "")}
        </div>
        <div class="muted">Scheduled and manual doses only run inside the window (start included, end excluded). It may cross midnight, for example 20:00 to 06:00.</div>
      </fieldset>
      <fieldset><legend>Pump and calibration</legend>
        <div class="grid">
          <label>Pump switch (entity id)<input data-k="switch" type="text" value="${esc(d.switch)}" placeholder="switch.alk_pump"></label>
          ${f("multiplier", "Calibration (sec per mL)", "number", 'min="0.1" max="600" step="0.001"')}
        </div>
        <div class="muted">${mlps} mL per second. A dose runs for volume x calibration seconds.</div>
        <div class="actions">
          <button data-act="caldose">Run 10 mL test dose</button>
          <input id="measured" type="number" min="0.1" step="0.1" placeholder="measured mL" style="width:120px">
          <button data-act="calapply" ${cal ? "" : "disabled"}>Apply</button>
        </div>
        <div class="muted">${cal ? `Test dose ran at ${num(cal.multiplier, 3)} sec/mL. Measure what came out and apply.` : "Collect the output in a container, not the tank, then measure it."}</div>
      </fieldset>
      <fieldset><legend>Safety</legend>
        <div class="grid">
          ${f("max_run_seconds", "Maximum run time (s)", "number", 'min="1" max="3600" step="1"')}
          ${f("daily_cap", `Daily cap (mL, 0 = automatic${p ? `, now ${num(p.daily_cap)}` : ""})`, "number", 'min="0" step="0.1"')}
          ${f("cooldown_minutes", "Cooldown between doses (min)", "number", 'min="0" step="1"')}
        </div>
      </fieldset>
      <div class="actions">
        <button class="primary" id="save" data-act="save" ${this._dirty ? "" : "disabled"}>Save</button>
        <button id="revert" data-act="revert" ${this._dirty ? "" : "disabled"}>Revert</button>
        <span class="spacer"></span>
        <input id="dosevol" type="number" min="0.01" step="0.01" placeholder="mL (default)" style="width:110px">
        <label class="inline"><input id="ignorewin" type="checkbox"> ignore window</label>
        <button data-act="dose">Dose now</button>
        <button class="danger" data-act="delete">Delete pump</button>
      </div>`;
  }

  _drawTimeline() {
    const svg = this._root("timeline");
    if (!svg || !this._data) return;
    const p = this._pump();
    this._root("err").textContent = this._error;
    if (!this._draft || !p) {
      svg.innerHTML = "";
      return;
    }
    // Unsaved edits: show the draft's slots. Otherwise show today's real outcomes.
    const slots = this._dirty && this._preview ? this._preview.slots : p.slots;
    svg.innerHTML = timelineSvg({ slots, cfg: this._draft, nowMin: this._data.now_minute, height: 120 });
  }
}

/* ========================================================== summary card */
class AquariumDosingSummaryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    this._config = { title: "", ...config };
  }
  getCardSize() {
    return 4;
  }
  static getStubConfig() {
    return {};
  }
  connectedCallback() {
    if (this._hass && this._built) this._load();
  }
  disconnectedCallback() {
    clearTimeout(this._timer);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._shell();
      this._load();
    }
  }

  _shell() {
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>
        ${CSS_COMMON}
        ha-card { padding: 16px; display: block; }
        h2 { margin: 0 0 8px; font-size: 1.25rem; font-weight: 500; }
        .pump { padding: 10px 0; border-top: 1px solid var(--divider-color); }
        .pump:first-of-type { border-top: 0; }
        .name { font-weight: 500; text-transform: capitalize; margin-right: 4px; }
        .bar { height: 6px; border-radius: 3px; background: var(--secondary-background-color); margin: 8px 0 2px; overflow: hidden; }
        .bar i { display: block; height: 100%; background: var(--primary-color); }
        .bar.hot i { background: var(--warning-color, #f9a825); }
        .paused { color: var(--secondary-text-color); font-size: .85rem; margin-bottom: 6px; }
      </style>
      <ha-card>
        ${this._config.title ? `<h2>${esc(this._config.title)}</h2>` : ""}
        <div id="paused" class="paused"></div>
        <div id="pumps"></div>
        <div class="err" id="err"></div>
      </ha-card>`;
  }

  async _load() {
    clearTimeout(this._timer);
    if (!this._hass || !this._built) return;
    const err = this.shadowRoot.getElementById("err");
    let data;
    try {
      const msg = { type: "aquarium_dosing/get" };
      if (this._config.master_entity) msg.master_entity = this._config.master_entity;
      data = await this._hass.callWS(msg);
    } catch (e) {
      err.textContent = `Could not reach the Aquarium Dosing integration: ${e.message || e.code || e}`;
      this._timer = setTimeout(() => this._load(), 30000);
      return;
    }
    err.textContent = "";
    let names = Object.keys(data.pumps);
    if (Array.isArray(this._config.pumps) && this._config.pumps.length) names = names.filter((n) => this._config.pumps.includes(n));
    this.shadowRoot.getElementById("paused").textContent = data.master_on ? "" : "Scheduled dosing is paused";
    this.shadowRoot.getElementById("pumps").innerHTML = names
      .map((n) => {
        const p = data.pumps[n];
        const frac = p.daily_cap > 0 ? Math.min(1, p.delivered_24h / p.daily_cap) : 0;
        return `<div class="pump">
          <div class="row"><span class="name">${esc(n)}</span>${statusChip(p)}
            <span class="chip">Next ${esc(when(p.next_dose, data.now))}</span>
            <span class="chip">24 h ${num(p.delivered_24h)} / ${num(p.daily_cap)} mL</span></div>
          <div class="bar ${frac > 0.9 ? "hot" : ""}"><i style="width:${(frac * 100).toFixed(1)}%"></i></div>
          <svg viewBox="0 0 720 52" role="img" aria-label="${esc(n)} doses over 24 hours">${timelineSvg({ slots: p.slots, cfg: p.config, nowMin: data.now_minute, height: 52, labels: false })}</svg></div>`;
      })
      .join("") + (names.length ? LEGEND : "");
    const busy = names.some((n) => BUSY.includes(data.pumps[n].status));
    this._timer = setTimeout(() => this._load(), busy ? 3000 : 30000);
  }
}

/* ------------------------------------------------------------ registration */
if (!customElements.get("aquarium-dosing-card")) customElements.define("aquarium-dosing-card", AquariumDosingCard);
if (!customElements.get("aquarium-dosing-summary-card")) customElements.define("aquarium-dosing-summary-card", AquariumDosingSummaryCard);
window.customCards = window.customCards || [];
[
  {
    type: "aquarium-dosing-card",
    name: "Aquarium Dosing editor",
    description: "Edit dosing pumps: schedule, operating window, calibration and safety limits, with a dose timeline.",
  },
  {
    type: "aquarium-dosing-summary-card",
    name: "Aquarium Dosing summary",
    description: "Compact status, next dose, 24 h totals and dose timeline for every pump.",
  },
].forEach((card) => {
  if (!window.customCards.some((c) => c.type === card.type)) window.customCards.push(card);
});
})();
