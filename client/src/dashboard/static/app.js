/**
 * Gemi-fly Dashboard — app.js
 *
 * Vanilla ES6+ classes. WebSocket-driven real-time drone mission dashboard
 * with perception overlay, AI activity tracking, and PDF report generation.
 */

// ─── Phase Map ─────────────────────────────────────────────────
const PHASES = ['recon', 'analysis', 'acquire', 'approach', 'inspection'];
const PHASE_INDEX = Object.fromEntries(PHASES.map((p, i) => [p, i]));

const RESULT_ICONS = {
  perception: '\u{1F441}',    // eye
  frame_analysis: '\u{1F50D}', // magnifier
  spatial_instruction: '\u{1F9E0}', // brain
  label: '\u{1F3F7}',          // tag
  inspection: '\u{1F4CB}',     // clipboard
};

const DISTANCE_LABELS = [
  [0.25, 'Very Close'],
  [0.15, 'Close'],
  [0.08, 'Medium'],
  [0.0,  'Far'],
];

// ─── Utilities ─────────────────────────────────────────────────
function ts(unix) {
  const d = new Date((unix || Date.now() / 1000) * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map(n => String(n).padStart(2, '0')).join(':');
}

function syntaxHighlight(obj, indent = 0) {
  if (obj === null) return '<span class="json-null">null</span>';
  if (typeof obj === 'boolean') return `<span class="json-bool">${obj}</span>`;
  if (typeof obj === 'number') return `<span class="json-number">${obj}</span>`;
  if (typeof obj === 'string') return `<span class="json-string">"${escapeHtml(obj)}"</span>`;

  const pad = '  '.repeat(indent);
  const padInner = '  '.repeat(indent + 1);

  if (Array.isArray(obj)) {
    if (obj.length === 0) return '[]';
    const items = obj.map(v => padInner + syntaxHighlight(v, indent + 1));
    return '[\n' + items.join(',\n') + '\n' + pad + ']';
  }

  const keys = Object.keys(obj);
  if (keys.length === 0) return '{}';
  const entries = keys.map(k =>
    padInner + `<span class="json-key">"${escapeHtml(k)}"</span>: ` + syntaxHighlight(obj[k], indent + 1)
  );
  return '{\n' + entries.join(',\n') + '\n' + pad + '}';
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function distanceLabel(relSize) {
  for (const [threshold, label] of DISTANCE_LABELS) {
    if (relSize >= threshold) return label;
  }
  return 'Far';
}

// ─── WebSocket Manager ─────────────────────────────────────────
class WSManager {
  constructor(dashboard) {
    this._dashboard = dashboard;
    this._ws = null;
    this._reconnectTimer = null;
    this._url = `ws://${location.host}/ws`;
  }

  connect() {
    if (this._ws && this._ws.readyState <= WebSocket.OPEN) return;
    try {
      this._ws = new WebSocket(this._url);
    } catch { this._scheduleReconnect(); return; }

    this._ws.onopen = () => {
      this._dashboard.onConnected();
      if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
    };
    this._ws.onclose = () => { this._dashboard.onDisconnected(); this._scheduleReconnect(); };
    this._ws.onerror = () => {};
    this._ws.onmessage = (e) => {
      try { this._dashboard.onMessage(JSON.parse(e.data)); } catch {}
    };
  }

  send(obj) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify(obj));
    }
  }

  _scheduleReconnect() {
    if (this._reconnectTimer) return;
    this._reconnectTimer = setTimeout(() => { this._reconnectTimer = null; this.connect(); }, 3000);
  }
}

// ─── Canvas Renderer ───────────────────────────────────────────
class CanvasRenderer {
  constructor(canvas) {
    this._canvas = canvas;
    this._ctx = canvas.getContext('2d');
    this._perception = null;
    this._perceptionTime = 0;
    this._hasFrame = false;
  }

  drawFrame(b64) {
    const img = new Image();
    img.onload = () => {
      this._ctx.drawImage(img, 0, 0, this._canvas.width, this._canvas.height);
      this._drawOverlay();
      this._hasFrame = true;
    };
    img.src = 'data:image/jpeg;base64,' + b64;
  }

  updatePerception(data) {
    this._perception = data;
    this._perceptionTime = performance.now();
  }

  _drawOverlay() {
    const ctx = this._ctx;
    const w = this._canvas.width;
    const h = this._canvas.height;
    const cx = w / 2;
    const cy = h / 2;

    // Center crosshair — always visible, subtle
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(cx - 20, cy); ctx.lineTo(cx + 20, cy);
    ctx.moveTo(cx, cy - 20); ctx.lineTo(cx, cy + 20);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    if (!this._perception) return;

    const age = (performance.now() - this._perceptionTime) / 1000;
    if (age > 3) return; // too stale — don't draw
    const alpha = age < 1.5 ? 1 : Math.max(0, 1 - (age - 1.5) / 1.5);
    if (alpha <= 0) return;

    const p = this._perception;
    const tx = cx + p.horizontal_offset * (w / 2);
    const ty = cy + p.vertical_offset * (h / 2);
    const radius = Math.max(20, p.relative_size * w * 0.5);

    ctx.save();
    ctx.globalAlpha = alpha;

    // Target crosshair
    const color = p.target_visible ? '#16a34a' : '#dc2626';
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(tx - 14, ty); ctx.lineTo(tx + 14, ty);
    ctx.moveTo(tx, ty - 14); ctx.lineTo(tx, ty + 14);
    ctx.stroke();

    // Confidence circle
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(tx, ty, radius, 0, Math.PI * 2);
    ctx.stroke();

    // Box corners (15px marks)
    const cs = 15;
    const bx = tx - radius;
    const by = ty - radius;
    const bw = radius * 2;
    const bh = radius * 2;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    // top-left
    ctx.moveTo(bx, by + cs); ctx.lineTo(bx, by); ctx.lineTo(bx + cs, by);
    // top-right
    ctx.moveTo(bx + bw - cs, by); ctx.lineTo(bx + bw, by); ctx.lineTo(bx + bw, by + cs);
    // bottom-right
    ctx.moveTo(bx + bw, by + bh - cs); ctx.lineTo(bx + bw, by + bh); ctx.lineTo(bx + bw - cs, by + bh);
    // bottom-left
    ctx.moveTo(bx + cs, by + bh); ctx.lineTo(bx, by + bh); ctx.lineTo(bx, by + bh - cs);
    ctx.stroke();

    // Confidence text
    if (p.target_visible) {
      const conf = Math.round(p.confidence * 100);
      ctx.fillStyle = color;
      ctx.font = '600 12px "DM Sans", sans-serif';
      ctx.fillText(conf + '%', tx + radius + 6, ty - 4);
    }

    // Obstacle warning
    if (p.obstacle_ahead) {
      ctx.fillStyle = '#dc2626';
      ctx.font = '700 14px "DM Sans", sans-serif';
      ctx.fillText('OBSTACLE', cx - 36, h - 50);
    }

    // Age indicator
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '11px "JetBrains Mono", monospace';
    ctx.fillText(age.toFixed(1) + 's ago', w - 72, h - 10);

    ctx.restore();
  }

  get hasFrame() { return this._hasFrame; }
}

// ─── Phase Timeline ────────────────────────────────────────────
class PhaseTimeline {
  constructor() {
    this._activePhase = null;
    this._completedPhases = new Set();
    this._phaseStartTimes = {};
  }

  update(phase) {
    if (!phase || phase === 'complete') {
      // Complete all remaining
      for (const p of PHASES) {
        if (!this._completedPhases.has(p)) this._markComplete(p);
      }
      return;
    }

    const idx = PHASE_INDEX[phase];
    if (idx === undefined) return;

    // Complete all phases before this one
    for (let i = 0; i < idx; i++) {
      if (!this._completedPhases.has(PHASES[i])) {
        this._markComplete(PHASES[i]);
      }
    }

    // Activate current
    if (this._activePhase !== phase) {
      this._activePhase = phase;
      this._phaseStartTimes[phase] = performance.now();
      const n = idx + 1;
      const circle = document.getElementById('step-' + n);
      const label = circle?.parentElement?.querySelector('.step-label');
      const dur = document.getElementById('dur-' + n);
      if (circle) { circle.className = 'step-circle active'; }
      if (label) { label.className = 'step-label active'; }
      if (dur) { dur.textContent = '...'; }
    }
  }

  _markComplete(phase) {
    if (this._completedPhases.has(phase)) return;
    this._completedPhases.add(phase);
    const idx = PHASE_INDEX[phase];
    const n = idx + 1;
    const circle = document.getElementById('step-' + n);
    const label = circle?.parentElement?.querySelector('.step-label');
    const dur = document.getElementById('dur-' + n);

    if (circle) { circle.className = 'step-circle complete'; circle.innerHTML = '\u2713'; }
    if (label) { label.className = 'step-label complete'; }

    // Duration
    if (dur && this._phaseStartTimes[phase]) {
      const elapsed = (performance.now() - this._phaseStartTimes[phase]) / 1000;
      dur.textContent = elapsed.toFixed(1) + 's';
    }

    // Fill connector to next phase
    if (n < 5) {
      const conn = document.getElementById('conn-' + n + '-' + (n + 1));
      if (conn) conn.classList.add('filled');
    }
  }

  reset() {
    this._activePhase = null;
    this._completedPhases.clear();
    this._phaseStartTimes = {};
    for (let i = 1; i <= 5; i++) {
      const circle = document.getElementById('step-' + i);
      const label = circle?.parentElement?.querySelector('.step-label');
      const dur = document.getElementById('dur-' + i);
      if (circle) { circle.className = 'step-circle'; circle.textContent = i; }
      if (label) { label.className = 'step-label'; }
      if (dur) { dur.textContent = ''; }
    }
    for (let i = 1; i < 5; i++) {
      const conn = document.getElementById('conn-' + i + '-' + (i + 1));
      if (conn) conn.classList.remove('filled');
    }
  }
}

// ─── AI Activity Tracker ───────────────────────────────────────
class AITracker {
  constructor() {
    this._totalCalls = 0;
    this._totalLatency = 0;
    this._totalAITime = 0;
    this._activeTimer = null;
    this._activeStart = 0;
    this._activeModel = null;

    this._spinner = document.getElementById('ai-spinner');
    this._opName = document.getElementById('ai-op-name');
    this._opDesc = document.getElementById('ai-op-desc');
    this._elapsed = document.getElementById('ai-elapsed');
    this._callCount = document.getElementById('ai-call-count');
    this._total = document.getElementById('ai-total');
    this._avg = document.getElementById('ai-avg');
    this._time = document.getElementById('ai-time');
    this._flashBadge = document.getElementById('model-flash');
    this._proBadge = document.getElementById('model-pro');
  }

  onActivity(data) {
    if (data.status === 'started') {
      this._activeStart = performance.now();
      this._activeModel = data.model || '';

      if (this._opName) this._opName.textContent = data.operation || '—';
      if (this._opDesc) this._opDesc.textContent = data.description || '';
      if (this._elapsed) this._elapsed.textContent = '0.0s';
      if (this._spinner) this._spinner.classList.add('active');

      // Model badge
      this._flashBadge?.classList.remove('active');
      this._proBadge?.classList.remove('active');
      if (this._activeModel.toLowerCase().includes('flash')) {
        this._flashBadge?.classList.add('active');
      } else if (this._activeModel.toLowerCase().includes('pro')) {
        this._proBadge?.classList.add('active');
      }

      // Start elapsed timer
      this._clearTimer();
      this._activeTimer = setInterval(() => {
        const e = (performance.now() - this._activeStart) / 1000;
        if (this._elapsed) this._elapsed.textContent = e.toFixed(1) + 's';
      }, 100);

    } else if (data.status === 'completed' || data.status === 'error') {
      this._clearTimer();
      this._totalCalls++;
      const latency = data.latency_ms || 0;
      this._totalLatency += latency;
      this._totalAITime += latency;

      // Brief green/red flash on spinner then hide
      if (this._spinner) {
        this._spinner.style.background = data.status === 'completed'
          ? 'rgba(22,163,74,0.08)' : 'rgba(220,38,38,0.08)';
        setTimeout(() => {
          if (this._spinner) {
            this._spinner.classList.remove('active');
            this._spinner.style.background = '';
          }
        }, 1200);
      }

      // Deactivate model badges
      setTimeout(() => {
        this._flashBadge?.classList.remove('active');
        this._proBadge?.classList.remove('active');
      }, 1200);

      this._updateStats();
    }
  }

  _clearTimer() {
    if (this._activeTimer) { clearInterval(this._activeTimer); this._activeTimer = null; }
  }

  _updateStats() {
    if (this._callCount) this._callCount.textContent = this._totalCalls + ' calls';
    if (this._total) this._total.textContent = this._totalCalls;
    if (this._avg) {
      this._avg.textContent = this._totalCalls > 0
        ? (this._totalLatency / this._totalCalls / 1000).toFixed(1) + 's' : '—';
    }
    if (this._time) this._time.textContent = (this._totalAITime / 1000).toFixed(0) + 's';
  }

  reset() {
    this._clearTimer();
    this._totalCalls = 0;
    this._totalLatency = 0;
    this._totalAITime = 0;
    this._spinner?.classList.remove('active');
    this._flashBadge?.classList.remove('active');
    this._proBadge?.classList.remove('active');
    this._updateStats();
  }
}

// ─── Mission Log ───────────────────────────────────────────────
class MissionLog {
  constructor() {
    this._container = document.getElementById('log-container');
    this._countBadge = document.getElementById('log-count');
    this._count = 0;
    this._maxEntries = 100;
  }

  addEntry(level, message, timestamp) {
    const el = document.createElement('div');
    el.className = 'log-entry ' + level;
    el.innerHTML =
      `<span class="log-time">${ts(timestamp)}</span>` +
      `<span class="log-level ${level}">${level}</span>` +
      `<span class="log-message">${escapeHtml(message)}</span>`;

    this._container.appendChild(el);
    this._count++;
    this._prune();
    this._updateCount();
    this._scrollToBottom();
  }

  addResultCard(data, timestamp) {
    const type = data.result_type || 'unknown';
    const icon = RESULT_ICONS[type] || '\u{1F4C4}';
    const summary = data.summary || type;

    const card = document.createElement('div');
    card.className = 'ai-result-card';
    card.innerHTML =
      `<div class="ai-result-header" onclick="this.parentElement.classList.toggle('expanded')">` +
        `<span>${icon}</span>` +
        `<span style="flex:1">${escapeHtml(summary)}</span>` +
        `<span class="log-time">${ts(timestamp)}</span>` +
        `<span class="ai-result-chevron">\u25B6</span>` +
      `</div>` +
      `<div class="ai-result-body">` +
        `<div class="ai-result-json">${syntaxHighlight(data.data || data, 0)}</div>` +
      `</div>`;

    this._container.appendChild(card);
    this._count++;
    this._prune();
    this._updateCount();
    this._scrollToBottom();
  }

  _prune() {
    while (this._container.children.length > this._maxEntries) {
      this._container.removeChild(this._container.firstChild);
      this._count = Math.max(0, this._count - 1);
    }
  }

  _updateCount() {
    if (this._countBadge) this._countBadge.textContent = this._count;
  }

  _scrollToBottom() {
    this._container.scrollTop = this._container.scrollHeight;
  }

  clear() {
    this._container.innerHTML = '';
    this._count = 0;
    this._updateCount();
  }
}

// ─── Dashboard Controller ──────────────────────────────────────
class Dashboard {
  constructor() {
    // State
    this._demoMode = false;
    this._currentState = 'IDLE';
    this._currentPhase = null;
    this._reportData = null;
    this._currentTarget = '';

    // Components
    this._ws = new WSManager(this);
    this._canvas = new CanvasRenderer(document.getElementById('video-canvas'));
    this._timeline = new PhaseTimeline();
    this._ai = new AITracker();
    this._log = new MissionLog();

    // DOM refs
    this._connDot = document.getElementById('conn-dot');
    this._connText = document.getElementById('conn-text');
    this._demoBadge = document.getElementById('demo-badge');
    this._placeholder = document.getElementById('video-placeholder');
    this._statusBadge = document.getElementById('status-badge');
    this._telemBattery = document.getElementById('telem-battery');
    this._batteryBar = document.getElementById('battery-bar');
    this._telemAlt = document.getElementById('telem-altitude');
    this._telemTemp = document.getElementById('telem-temp');
    this._telemPhase = document.getElementById('telem-phase');
    this._percTarget = document.getElementById('perc-target');
    this._percVisibleDot = document.getElementById('perc-visible-dot');
    this._confBar = document.getElementById('conf-bar');
    this._percConf = document.getElementById('perc-conf');
    this._distBar = document.getElementById('dist-bar');
    this._percDist = document.getElementById('perc-dist');
    this._targetInput = document.getElementById('target-input');
    this._targetLabel = document.getElementById('target-label');
    this._targetGroup = document.getElementById('target-group');

    // Buttons
    this._btnStart = document.getElementById('btn-start');
    this._btnPause = document.getElementById('btn-pause');
    this._btnLand = document.getElementById('btn-land');
    this._btnSkip = document.getElementById('btn-skip');
    this._btnEstop = document.getElementById('btn-estop');
    this._btnReport = document.getElementById('btn-report');

    this._bindButtons();
    this._checkDemoMode();
  }

  start() {
    this._bootAnimation();
    this._ws.connect();
  }

  // ── WebSocket Callbacks ──────────────────────────────────────
  onConnected() {
    this._connDot?.classList.add('connected');
    if (this._connText) this._connText.textContent = 'Connected';
  }

  onDisconnected() {
    this._connDot?.classList.remove('connected');
    if (this._connText) this._connText.textContent = 'Disconnected';
  }

  onMessage(msg) {
    const handler = this._handlers[msg.type];
    if (handler) handler.call(this, msg.data, msg.timestamp);
  }

  get _handlers() {
    return {
      frame:        this._onFrame,
      telemetry:    this._onTelemetry,
      status:       this._onStatus,
      perception:   this._onPerception,
      log:          this._onLog,
      ai_activity:  this._onAIActivity,
      ai_result:    this._onAIResult,
      report_data:  this._onReportData,
    };
  }

  // ── Message Handlers ─────────────────────────────────────────
  _onFrame(data) {
    this._canvas.drawFrame(data);
    if (this._placeholder && !this._placeholder.classList.contains('hidden')) {
      this._placeholder.classList.add('hidden');
    }
  }

  _onTelemetry(data) {
    const battery = data.battery ?? 0;
    const altitude = data.altitude ?? 0;
    const temp = data.temperature ?? 0;

    if (this._telemBattery) this._telemBattery.textContent = battery + '%';
    if (this._batteryBar) {
      this._batteryBar.style.width = battery + '%';
      this._batteryBar.classList.toggle('low', battery < 20);
    }
    if (this._telemAlt) this._telemAlt.textContent = altitude + ' cm';
    if (this._telemTemp) this._telemTemp.textContent = temp + '\u00B0C';
  }

  _onStatus(data) {
    const state = data.state || 'IDLE';
    const phase = data.phase || null;
    const step = data.step;
    const maxSteps = data.max_steps;
    const target = data.target;

    this._currentState = state;
    this._currentPhase = phase;
    if (target) this._currentTarget = target;

    // Status badge
    this._updateStatusBadge(state);

    // Phase timeline
    this._timeline.update(phase);

    // Phase row in telemetry
    if (this._telemPhase) {
      let phaseText = phase || '—';
      if (step !== undefined && maxSteps) {
        phaseText += ` (${step}/${maxSteps})`;
      }
      this._telemPhase.textContent = phaseText;
    }

    // Perception target
    if (target && this._percTarget) this._percTarget.textContent = target;

    // Button states
    this._updateButtons();

    // Skip button label
    this._updateSkipLabel(phase);

    // Reset on idle
    if (state === 'IDLE' || state === 'READY') {
      this._timeline.reset();
    }
    if (state === 'COMPLETE') {
      this._timeline.update('complete');
    }
  }

  _onPerception(data) {
    this._canvas.updatePerception(data);

    const visible = data.target_visible;
    const conf = Math.round((data.confidence || 0) * 100);
    const relSize = data.relative_size || 0;

    if (this._percVisibleDot) {
      this._percVisibleDot.classList.toggle('visible', !!visible);
    }
    if (this._confBar) this._confBar.style.width = conf + '%';
    if (this._percConf) this._percConf.textContent = conf + '%';

    // Distance bar — inverse of relative_size (closer = fuller bar)
    const distPct = Math.min(100, relSize * 400); // 0.25 = 100%
    if (this._distBar) this._distBar.style.width = distPct + '%';
    if (this._percDist) this._percDist.textContent = distanceLabel(relSize);
  }

  _onLog(data) {
    this._log.addEntry(data.level || 'INFO', data.message || '', data.timestamp);
  }

  _onAIActivity(data) {
    this._ai.onActivity(data);
  }

  _onAIResult(data, timestamp) {
    this._log.addResultCard(data, timestamp);
  }

  _onReportData(data) {
    this._reportData = data;
    if (this._btnReport) this._btnReport.style.display = '';
  }

  // ── Status Badge ─────────────────────────────────────────────
  _updateStatusBadge(state) {
    const badge = this._statusBadge;
    if (!badge) return;
    badge.textContent = state;
    badge.className = 'status-badge';

    const map = {
      IDLE: 'status-idle', READY: 'status-idle',
      CONNECTING: 'status-executing', CONNECTED: 'status-executing',
      TAKEOFF: 'status-executing', EXECUTING: 'status-executing',
      LANDING: 'status-executing',
      COMPLETE: 'status-complete',
      ERROR: 'status-error', EMERGENCY: 'status-error',
    };
    badge.classList.add(map[state] || 'status-idle');
  }

  // ── Button Logic ─────────────────────────────────────────────
  _bindButtons() {
    this._btnStart?.addEventListener('click', () => this._sendCommand('start'));
    this._btnPause?.addEventListener('click', () => this._sendCommand('pause'));
    this._btnLand?.addEventListener('click', () => this._sendCommand('land'));
    this._btnEstop?.addEventListener('click', () => this._sendCommand('emergency_land'));
    this._btnSkip?.addEventListener('click', () => this._sendCommand('skip_phase'));
    this._btnReport?.addEventListener('click', () => this._generateReport());
  }

  _sendCommand(action) {
    const cmd = { type: 'command', action };
    if (action === 'start') {
      const input = this._targetInput;
      if (this._demoMode && input?.tagName === 'SELECT') {
        cmd.demo_id = input.value;
        cmd.target = input.options[input.selectedIndex]?.textContent || '';
      } else if (input) {
        cmd.target = input.value || '';
      }
      cmd.mode = 'exploration';
    }
    this._ws.send(cmd);
  }

  _updateButtons() {
    const s = this._currentState;
    const executing = ['TAKEOFF', 'EXECUTING', 'LANDING'].includes(s);
    const idle = ['IDLE', 'READY', 'CONNECTED', 'COMPLETE'].includes(s);

    if (this._btnStart) this._btnStart.disabled = !idle;
    if (this._btnPause) this._btnPause.disabled = !executing;
    if (this._btnLand) this._btnLand.disabled = !executing && s !== 'COMPLETE';

    // Target input disabled while executing
    if (this._targetInput) this._targetInput.disabled = executing;
  }

  _updateSkipLabel(phase) {
    if (!this._btnSkip) return;
    if (!this._demoMode) { this._btnSkip.style.display = 'none'; return; }

    this._btnSkip.style.display = '';
    if (phase === 'approach') {
      this._btnSkip.textContent = 'Next Step';
    } else if (phase === 'inspection') {
      this._btnSkip.textContent = 'Skip Wait';
    } else {
      this._btnSkip.textContent = 'Skip Phase';
    }
  }

  // ── Demo Mode ────────────────────────────────────────────────
  async _checkDemoMode() {
    try {
      const res = await fetch('/api/demo-info');
      const info = await res.json();
      this._demoMode = info.demo_mode;
      if (this._demoMode) {
        this._enableDemoUI(info.demos || []);
      }
    } catch {
      // Not in demo mode or server not ready
    }
  }

  _enableDemoUI(demos) {
    if (this._demoBadge) this._demoBadge.style.display = '';
    if (this._btnSkip) this._btnSkip.style.display = '';

    // Replace text input with select dropdown
    if (this._targetInput && this._targetGroup) {
      const select = document.createElement('select');
      select.id = 'target-input';
      select.className = this._targetInput.className;

      if (demos.length === 0) {
        const opt = document.createElement('option');
        opt.textContent = 'No recordings available';
        opt.disabled = true;
        select.appendChild(opt);
      } else {
        for (const demo of demos) {
          const opt = document.createElement('option');
          opt.value = demo.id;
          opt.textContent = demo.label || demo.target || demo.id;
          if (demo.duration_sec) {
            opt.textContent += ` (${Math.round(demo.duration_sec)}s)`;
          }
          select.appendChild(opt);
        }
      }

      this._targetInput.replaceWith(select);
      this._targetInput = select;
      if (this._targetLabel) this._targetLabel.textContent = 'Select Demo';
    }
  }

  // ── PDF Report Generation ────────────────────────────────────
  _generateReport() {
    if (!this._reportData) return;
    const rd = this._reportData;
    const meta = rd.metadata || {};
    const result = rd.inspection_result || {};

    // Open window synchronously to avoid popup blocker
    const win = window.open('', '_blank');
    if (!win) return;

    const findings = (result.findings || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
    const phases = (meta.phases_completed || []).map(p =>
      `<span style="display:inline-block;padding:2px 8px;background:rgba(22,163,74,0.08);color:#16a34a;border-radius:10px;font-size:0.75rem;font-weight:600;margin-right:4px">${p}</span>`
    ).join('');

    const html = `<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Mission Report — ${escapeHtml(meta.target || 'Unknown')}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Instrument+Serif&family=JetBrains+Mono:wght@400;500&display=swap');
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'DM Sans',sans-serif; background:#f6f4f1; color:#1a1816; padding:40px; line-height:1.6; }
  .report { max-width:800px; margin:0 auto; background:white; border-radius:12px; border:1px solid #e8e4df; overflow:hidden; }
  .report-header { padding:24px 32px; border-bottom:1px solid #e8e4df; display:flex; align-items:center; justify-content:space-between; }
  .report-title { font-family:'Instrument Serif',serif; font-size:1.8rem; }
  .report-subtitle { font-size:0.75rem; color:#6b6560; text-transform:uppercase; letter-spacing:0.1em; }
  .report-body { padding:24px 32px; }
  h2 { font-size:0.8rem; text-transform:uppercase; letter-spacing:0.08em; color:#6b6560; margin:20px 0 8px; font-weight:600; }
  .frames { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:12px 0; }
  .frames img { width:100%; border-radius:8px; border:1px solid #e8e4df; }
  .frames .frame-label { font-size:0.7rem; color:#9c9590; text-align:center; margin-top:4px; }
  .findings { padding:12px 16px; background:#faf9f7; border-radius:8px; margin:8px 0; }
  .findings p { font-size:0.9rem; margin-bottom:8px; }
  .findings ul { padding-left:20px; }
  .findings li { font-size:0.85rem; margin-bottom:4px; }
  .meta-table { width:100%; border-collapse:collapse; margin:12px 0; }
  .meta-table td { padding:6px 0; font-size:0.85rem; border-bottom:1px solid #f0ede9; }
  .meta-table td:first-child { color:#6b6560; width:140px; }
  .confidence { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:600; }
  .report-footer { padding:16px 32px; border-top:1px solid #e8e4df; text-align:center; font-size:0.7rem; color:#9c9590; }
  @media print { body { padding:0; background:white; } .report { border:none; border-radius:0; } }
</style></head><body>
<div class="report">
  <div class="report-header">
    <div>
      <div class="report-title">Gemi-fly</div>
      <div class="report-subtitle">Mission Report</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:0.85rem;font-weight:600">${escapeHtml(meta.target || 'Target')}</div>
      <div style="font-size:0.7rem;color:#9c9590">${new Date().toLocaleDateString()}</div>
    </div>
  </div>
  <div class="report-body">
    <h2>Captured Frames</h2>
    <div class="frames">
      <div>
        ${rd.acquisition_frame ? `<img src="data:image/jpeg;base64,${rd.acquisition_frame}">` : '<div style="background:#f6f4f1;aspect-ratio:4/3;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#9c9590">No frame</div>'}
        <div class="frame-label">Acquisition</div>
      </div>
      <div>
        ${rd.inspection_frame ? `<img src="data:image/jpeg;base64,${rd.inspection_frame}">` : '<div style="background:#f6f4f1;aspect-ratio:4/3;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#9c9590">No frame</div>'}
        <div class="frame-label">Inspection</div>
      </div>
    </div>

    <h2>Inspection Findings</h2>
    <div class="findings">
      <p>${escapeHtml(result.description || 'No description available')}</p>
      ${findings ? '<ul>' + findings + '</ul>' : ''}
      ${result.confidence !== undefined ? `<div style="margin-top:8px"><span class="confidence" style="background:rgba(124,58,237,0.08);color:#7c3aed">${Math.round(result.confidence * 100)}% confidence</span></div>` : ''}
    </div>

    <h2>Mission Summary</h2>
    <table class="meta-table">
      <tr><td>Target</td><td>${escapeHtml(meta.target || '—')}</td></tr>
      <tr><td>Duration</td><td>${meta.duration_seconds ? meta.duration_seconds.toFixed(1) + 's' : '—'}</td></tr>
      <tr><td>Battery</td><td>${meta.battery_start ?? '—'}% \u2192 ${meta.battery_end ?? '—'}%</td></tr>
      <tr><td>Phases</td><td>${phases || '—'}</td></tr>
    </table>
  </div>
  <div class="report-footer">Generated by Gemi-fly \u2014 Powered by Gemini</div>
</div></body></html>`;

    win.document.write(html);
    win.document.close();
  }

  // ── Boot Animation ───────────────────────────────────────────
  _bootAnimation() {
    const splash = document.getElementById('splash');
    if (!splash) return;

    // The CSS handles the staggered reveals via animation-delay.
    // We just need to fade out and remove the splash.
    setTimeout(() => { splash.classList.add('fade-out'); }, 1700);
    setTimeout(() => { splash.remove(); }, 3500);
  }
}

// ─── Init ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const dashboard = new Dashboard();
  dashboard.start();
});
