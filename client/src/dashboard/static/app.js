/**
 * Drone Copilot Dashboard — app.js
 *
 * Vanilla ES6+ classes. WebSocket-driven real-time drone mission dashboard
 * with perception overlay, AI activity tracking, and PDF report generation.
 */

// ─── Phase Map ─────────────────────────────────────────────────
const PHASES = ['search', 'approach', 'inspect'];
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
    this._telemetry = null;
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

  updateTelemetry(data) {
    this._telemetry = { ...this._telemetry, ...data };
  }

  _drawHUDPanel(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, r);
    ctx.fill();
  }

  _drawHUD(ctx, w, h) {
    if (!this._telemetry) return;
    const t = this._telemetry;
    ctx.save();

    const panelR = 6;
    const panelBg = 'rgba(0,0,0,0.45)';
    const labelFont = '500 10px "Roboto", sans-serif';
    const valueFont = '600 14px "Roboto Mono", monospace';

    // ── Top-left: Battery + Altitude + Temp + Phase ──
    if (t.battery !== undefined || t.altitude !== undefined) {
      ctx.fillStyle = panelBg;
      this._drawHUDPanel(ctx, 12, 12, 140, 96, panelR);

      // Battery
      const bat = t.battery ?? 0;
      ctx.font = labelFont;
      ctx.fillStyle = 'rgba(255,255,255,0.6)';
      ctx.fillText('BAT', 20, 30);
      ctx.font = valueFont;
      ctx.fillStyle = bat > 50 ? '#34a853' : bat > 20 ? '#e37400' : '#ea4335';
      ctx.fillText(bat + '%', 48, 30);

      // Altitude
      ctx.font = labelFont;
      ctx.fillStyle = 'rgba(255,255,255,0.6)';
      ctx.fillText('ALT', 20, 52);
      ctx.font = valueFont;
      ctx.fillStyle = '#ffffff';
      ctx.fillText((t.altitude ?? 0) + 'cm', 48, 52);

      // Temperature
      ctx.font = labelFont;
      ctx.fillStyle = 'rgba(255,255,255,0.6)';
      ctx.fillText('TMP', 20, 74);
      ctx.font = valueFont;
      ctx.fillStyle = '#ffffff';
      ctx.fillText((t.temp ?? 0) + '\u00B0C', 48, 74);

      // Phase
      ctx.font = labelFont;
      ctx.fillStyle = 'rgba(255,255,255,0.6)';
      ctx.fillText('PHS', 20, 96);
      ctx.font = valueFont;
      ctx.fillStyle = '#ffffff';
      let phaseStr = t.phase ? t.phase.toUpperCase() : '\u2014';
      if (t.phase && t.step && t.maxSteps) phaseStr += ' ' + t.step + '/' + t.maxSteps;
      ctx.fillText(phaseStr, 48, 96);
    }

    // ── Bottom-left: Mission timer ──
    if (t.missionElapsed && t.missionElapsed > 0) {
      const mins = String(Math.floor(t.missionElapsed / 60)).padStart(2, '0');
      const secs = String(t.missionElapsed % 60).padStart(2, '0');
      const timerStr = `T+ ${mins}:${secs}`;

      ctx.fillStyle = panelBg;
      this._drawHUDPanel(ctx, 12, h - 40, 90, 28, panelR);

      ctx.fillStyle = '#ffffff';
      ctx.font = '600 12px "Roboto Mono", monospace';
      ctx.fillText(timerStr, 20, h - 20);
    }

    ctx.restore();
  }

  _drawOverlay() {
    const ctx = this._ctx;
    const w = this._canvas.width;
    const h = this._canvas.height;
    const cx = w / 2;
    const cy = h / 2;

    // HUD telemetry overlay — drawn first so perception renders on top
    this._drawHUD(ctx, w, h);

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
    // 0.9 margin keeps reticle from clipping at edges;
    // negate vertical because +1.0 = top in Gemini but Y increases downward on screen
    const tx = cx + p.horizontal_offset * cx * 0.9;
    const ty = cy - p.vertical_offset * cy * 0.9;
    const radius = Math.max(10, Math.min(22, p.relative_size * w * 0.08));

    ctx.save();
    ctx.globalAlpha = alpha;

    // Target crosshair
    const color = p.target_visible ? '#34a853' : '#ea4335';
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(tx - 8, ty); ctx.lineTo(tx + 8, ty);
    ctx.moveTo(tx, ty - 8); ctx.lineTo(tx, ty + 8);
    ctx.stroke();

    // Confidence circle
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(tx, ty, radius, 0, Math.PI * 2);
    ctx.stroke();

    // Confidence text
    if (p.target_visible) {
      const conf = Math.round(p.confidence * 100);
      ctx.fillStyle = color;
      ctx.font = '600 12px "Roboto", sans-serif';
      ctx.fillText(conf + '%', tx + radius + 4, ty - 2);
    }

    // Bounding box from box_2d [ymin, xmin, ymax, xmax] (0-1000 scale)
    if (p.box_2d && p.target_visible) {
      const [ymin, xmin, ymax, xmax] = p.box_2d;
      const bx = (xmin / 1000) * w;
      const by = (ymin / 1000) * h;
      const bw = ((xmax - xmin) / 1000) * w;
      const bh = ((ymax - ymin) / 1000) * h;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(bx, by, bw, bh);
    }

    // Obstacle warning
    if (p.obstacle_ahead) {
      ctx.fillStyle = '#ea4335';
      ctx.font = '700 14px "Roboto", sans-serif';
      ctx.fillText('OBSTACLE', cx - 36, h - 50);
    }

    // Age indicator
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '11px "Roboto Mono", monospace';
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
      const label = circle?.parentElement?.querySelector('.pill-label');
      const dur = document.getElementById('dur-' + n);
      if (circle) { circle.className = 'pill-circle active'; }
      if (label) { label.className = 'pill-label active'; }
      if (dur) { dur.textContent = '...'; }
    }
  }

  _markComplete(phase) {
    if (this._completedPhases.has(phase)) return;
    this._completedPhases.add(phase);
    const idx = PHASE_INDEX[phase];
    const n = idx + 1;
    const circle = document.getElementById('step-' + n);
    const label = circle?.parentElement?.querySelector('.pill-label');
    const dur = document.getElementById('dur-' + n);

    if (circle) { circle.className = 'pill-circle complete'; circle.innerHTML = '\u2713'; }
    if (label) { label.className = 'pill-label complete'; }

    // Duration
    if (dur && this._phaseStartTimes[phase]) {
      const elapsed = (performance.now() - this._phaseStartTimes[phase]) / 1000;
      dur.textContent = elapsed.toFixed(1) + 's';
    }

    // Fill connector to next phase
    if (n < 3) {
      const conn = document.getElementById('conn-' + n + '-' + (n + 1));
      if (conn) conn.classList.add('filled');
    }
  }

  reset() {
    this._activePhase = null;
    this._completedPhases.clear();
    this._phaseStartTimes = {};
    for (let i = 1; i <= 3; i++) {
      const circle = document.getElementById('step-' + i);
      const label = circle?.parentElement?.querySelector('.pill-label');
      const dur = document.getElementById('dur-' + i);
      if (circle) { circle.className = 'pill-circle'; circle.textContent = i; }
      if (label) { label.className = 'pill-label'; }
      if (dur) { dur.textContent = ''; }
    }
    for (let i = 1; i < 3; i++) {
      const conn = document.getElementById('conn-' + i + '-' + (i + 1));
      if (conn) conn.classList.remove('filled');
    }
  }
}

// ─── Flight Stats ─────────────────────────────────────────────
class FlightStats {
  constructor() {
    this._timer = document.getElementById('fs-timer');
    this._drain = document.getElementById('fs-drain');
    this._distance = document.getElementById('fs-distance');
    this._remaining = document.getElementById('fs-remaining');
    this._activity = document.getElementById('fs-activity');
    this._cmdCount = document.getElementById('cmd-count');
    this._indLive = document.getElementById('ind-live');
    this._indFlash = document.getElementById('ind-flash');

    this._missionStart = null;
    this._timerInterval = null;
    this._totalDistanceCm = 0;
    this._commandCount = 0;
    this._firstBattery = null;
    this._lastBattery = null;
    this._flashTimeout = null;
    this._flashCallCount = 0;
    this._flashCountEl = document.getElementById('flash-call-count');
  }

  onTelemetry(data) {
    const now = Date.now();
    const bat = data.battery;
    if (bat == null) return;
    if (!this._firstBattery) this._firstBattery = {value: bat, time: now};
    this._lastBattery = {value: bat, time: now};
    this._updateDrain();
  }

  onStatus(data) {
    const state = (data.state || '').toUpperCase();
    const phase = data.phase || '';

    if (state === 'EXECUTING' && !this._missionStart) {
      this._missionStart = Date.now();
      this._startTimer();
    }
    if (['COMPLETE', 'IDLE', 'ERROR'].includes(state) && this._missionStart) {
      this._stopTimer();
    }

    const activityMap = {
      search: 'Searching',
      approach: 'Approaching',
      inspect: 'Inspecting',
    };
    if (state === 'IDLE') this._activity.textContent = 'Idle';
    else if (state === 'COMPLETE') this._activity.textContent = 'Complete';
    else if (phase && activityMap[phase]) this._activity.textContent = activityMap[phase];
    else if (state === 'EXECUTING') this._activity.textContent = 'Free Flying';
  }

  onLog(data) {
    if (data.level !== 'COMMAND') return;
    this._commandCount++;
    this._cmdCount.textContent = `${this._commandCount} cmds`;

    const match = (data.message || '').match(/(\d+)\s*cm/);
    if (match) {
      this._totalDistanceCm += parseInt(match[1], 10);
      const m = (this._totalDistanceCm / 100).toFixed(1);
      this._distance.textContent = this._totalDistanceCm >= 100
        ? `${m} m` : `${this._totalDistanceCm} cm`;
    }
  }

  onConnected() { this._indLive?.classList.add('active'); }
  onDisconnected() { this._indLive?.classList.remove('active'); }

  onAIActivity(data) {
    this._indFlash?.classList.add('active-flash');
    if (this._flashTimeout) clearTimeout(this._flashTimeout);
    this._flashTimeout = setTimeout(() => {
      this._indFlash?.classList.remove('active-flash');
    }, 1200);
    if (data.source === 'flash') {
      this._flashCallCount++;
      if (this._flashCountEl) this._flashCountEl.textContent = `(${this._flashCallCount})`;
    }
  }

  onAIResult(data) {
    this._indFlash?.classList.add('active-flash');
    if (this._flashTimeout) clearTimeout(this._flashTimeout);
    this._flashTimeout = setTimeout(() => {
      this._indFlash?.classList.remove('active-flash');
    }, 1200);
  }

  reset() {
    this._missionStart = null;
    this._stopTimer();
    this._totalDistanceCm = 0;
    this._commandCount = 0;
    this._firstBattery = null;
    this._lastBattery = null;
    this._timer.textContent = '00:00';
    this._drain.textContent = '\u2014 %/min';
    this._distance.textContent = '0 cm';
    this._remaining.textContent = '\u2014';
    this._activity.textContent = 'Idle';
    this._cmdCount.textContent = '0 cmds';
    this._flashCallCount = 0;
    if (this._flashCountEl) this._flashCountEl.textContent = '';
  }

  getElapsedSeconds() {
    if (!this._missionStart) return 0;
    return Math.floor((Date.now() - this._missionStart) / 1000);
  }

  _startTimer() {
    this._timerInterval = setInterval(() => {
      if (!this._missionStart) return;
      const elapsed = Math.floor((Date.now() - this._missionStart) / 1000);
      const m = String(Math.floor(elapsed / 60)).padStart(2, '0');
      const s = String(elapsed % 60).padStart(2, '0');
      this._timer.textContent = `${m}:${s}`;
    }, 1000);
  }

  _stopTimer() {
    if (this._timerInterval) clearInterval(this._timerInterval);
    this._timerInterval = null;
  }

  _updateDrain() {
    if (!this._firstBattery || !this._lastBattery) return;
    const elapsedMin = (this._lastBattery.time - this._firstBattery.time) / 60000;
    if (elapsedMin < 0.5) return;
    const drop = this._firstBattery.value - this._lastBattery.value;
    const rate = drop / elapsedMin;
    this._drain.textContent = `${rate.toFixed(1)} %/min`;

    if (rate > 0 && this._lastBattery.value > 0) {
      const safeRemaining = Math.max(0, this._lastBattery.value - 20);
      const remainMin = safeRemaining / rate;
      this._remaining.textContent = `~${Math.round(remainMin)} min`;
    }
  }
}

// ─── Mission Log ───────────────────────────────────────────────
class MissionLog {
  constructor() {
    this._container = document.getElementById('log-container');
    this._countBadge = document.getElementById('log-count');
    this._count = 0;
    this._maxEntries = 100;

    // Voice transcript accumulation state
    this._lastVoiceEl = null;       // DOM element of current accumulating entry
    this._lastVoiceSpeaker = null;  // "USER" or "COPILOT"
    this._lastVoiceTimer = null;    // debounce timer ID
  }

  appendVoice(speaker, text, timestamp) {
    if (this._lastVoiceEl && this._lastVoiceSpeaker === speaker) {
      // Append to existing entry
      const msgSpan = this._lastVoiceEl.querySelector('.log-message');
      if (msgSpan) {
        msgSpan.textContent += ' ' + text;
      }
      this._scrollToBottom();
    } else {
      // New speaker or no active entry — create fresh
      this._clearVoiceAccumulation();
      this.addEntry(speaker, text, timestamp, true);
      this._lastVoiceEl = this._container.lastElementChild;
      this._lastVoiceSpeaker = speaker;
    }

    // Reset debounce timer — after 2s of silence, finalize this entry
    // (needs to survive gaps around tool calls where Gemini pauses >500ms)
    if (this._lastVoiceTimer) clearTimeout(this._lastVoiceTimer);
    this._lastVoiceTimer = setTimeout(() => this._clearVoiceAccumulation(), 2000);
  }

  _clearVoiceAccumulation() {
    if (this._lastVoiceTimer) {
      clearTimeout(this._lastVoiceTimer);
      this._lastVoiceTimer = null;
    }
    this._lastVoiceEl = null;
    this._lastVoiceSpeaker = null;
  }

  addEntry(level, message, timestamp, _isVoice) {
    // Non-voice entries (TOOL_CALL, INFO, ERROR) do NOT reset accumulation.
    // The copilot often fires tool calls mid-sentence — fragments after the
    // tool call should still append to the existing voice entry above.

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

// ─── Voice Client (Browser Mic → Backend) ─────────────────────
class VoiceClient {
  constructor(log) {
    this._log = log;
    this._ws = null;
    this._audioCtx = null;
    this._stream = null;
    this._workletNode = null;
    this._playbackCtx = null;
    this._playbackTime = 0;
    this._active = false;
    this._onStateChange = null;

    this._MOCK_RESPONSES = {
      takeoff: {status: 'ok', message: 'Mock takeoff complete'},
      land: {status: 'ok', message: 'Mock landed'},
      move_drone: {status: 'ok', message: 'Mock move complete'},
      rotate_drone: {status: 'ok', message: 'Mock rotation complete'},
      hover: {status: 'ok', message: 'Mock hovering'},
      set_speed: {status: 'ok', message: 'Speed set'},
    };
  }

  get active() { return this._active; }

  set onStateChange(fn) { this._onStateChange = fn; }

  async toggle() {
    if (this._active) {
      this.stop();
    } else {
      await this.start();
    }
  }

  async start() {
    this._emitState('connecting');
    try {
      // Fetch backend URL
      const res = await fetch('/api/backend-url');
      const {url} = await res.json();

      // Audio context for capture (48kHz native → downsample to 16kHz)
      this._audioCtx = new AudioContext({sampleRate: 48000});

      // Get microphone
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation: true, noiseSuppression: true, sampleRate: 48000},
      });

      // Playback context at 24kHz
      this._playbackCtx = new AudioContext({sampleRate: 24000});
      this._playbackTime = 0;

      // Setup AudioWorklet for capture
      await this._setupAudioCapture();

      // Open WebSocket to backend
      this._openWebSocket(url);
    } catch (err) {
      this._log.addEntry('ERROR', 'Voice: ' + err.message);
      this.stop();
    }
  }

  stop() {
    this._active = false;
    if (this._ws) {
      try { this._ws.close(); } catch {}
      this._ws = null;
    }
    if (this._workletNode) {
      this._workletNode.disconnect();
      this._workletNode = null;
    }
    if (this._stream) {
      this._stream.getTracks().forEach(t => t.stop());
      this._stream = null;
    }
    if (this._audioCtx) {
      this._audioCtx.close().catch(() => {});
      this._audioCtx = null;
    }
    if (this._playbackCtx) {
      this._playbackCtx.close().catch(() => {});
      this._playbackCtx = null;
    }
    this._emitState('idle');
  }

  async _setupAudioCapture() {
    // Inline AudioWorklet processor via Blob URL
    const processorCode = `
      class CaptureProcessor extends AudioWorkletProcessor {
        constructor() {
          super();
          this._buffer = [];
          this._samplesNeeded = 1600; // ~100ms at 16kHz
        }
        process(inputs) {
          const input = inputs[0];
          if (!input || !input[0]) return true;
          const samples48k = input[0];
          // Downsample 48kHz → 16kHz (take every 3rd sample)
          for (let i = 0; i < samples48k.length; i += 3) {
            this._buffer.push(samples48k[i]);
          }
          if (this._buffer.length >= this._samplesNeeded) {
            const chunk = this._buffer.splice(0, this._samplesNeeded);
            // Convert float32 → int16
            const int16 = new Int16Array(chunk.length);
            for (let i = 0; i < chunk.length; i++) {
              const s = Math.max(-1, Math.min(1, chunk[i]));
              int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }
            this.port.postMessage(int16.buffer, [int16.buffer]);
          }
          return true;
        }
      }
      registerProcessor('capture-processor', CaptureProcessor);
    `;
    const blob = new Blob([processorCode], {type: 'application/javascript'});
    const blobUrl = URL.createObjectURL(blob);

    await this._audioCtx.audioWorklet.addModule(blobUrl);
    URL.revokeObjectURL(blobUrl);

    const source = this._audioCtx.createMediaStreamSource(this._stream);
    this._workletNode = new AudioWorkletNode(this._audioCtx, 'capture-processor');

    this._workletNode.port.onmessage = (e) => {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        const b64 = this._arrayBufferToBase64(e.data);
        this._ws.send(JSON.stringify({type: 'audio_in', data: b64}));
      }
    };

    source.connect(this._workletNode);
    this._workletNode.connect(this._audioCtx.destination);
  }

  _openWebSocket(url) {
    this._ws = new WebSocket(url);

    this._ws.onopen = () => {
      this._active = true;
      this._emitState('recording');
      this._log.addEntry('VOICE', 'Voice session started');
    };

    this._ws.onclose = () => {
      if (this._active) {
        this._log.addEntry('VOICE', 'Voice session ended');
        this.stop();
      }
    };

    this._ws.onerror = () => {
      this._log.addEntry('ERROR', 'Voice WebSocket error');
    };

    this._ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        switch (msg.type) {
          case 'audio_out':
            this._enqueuePlayback(msg.data);
            break;
          case 'tool_call':
            this._handleToolCalls(msg.calls || msg.data || []);
            break;
          case 'transcript': {
            const speaker = (msg.speaker || 'ai').toUpperCase();
            const text = msg.text || msg.data || '';
            this._log.appendVoice(speaker, text, msg.timestamp);
            break;
          }
          case 'interrupted':
            this._stopPlayback();
            break;
          case 'session_status':
            this._log.addEntry('INFO', 'Session: ' + (msg.status || JSON.stringify(msg.data)));
            break;
          case 'error':
            this._log.addEntry('ERROR', 'Backend: ' + (msg.message || msg.data || 'unknown'));
            break;
        }
      } catch {}
    };
  }

  _handleToolCalls(calls) {
    for (const call of calls) {
      const name = call.name || call.function_name || 'unknown';
      const args = call.args || call.arguments || {};
      this._log.addEntry('TOOL_CALL', `${name}(${JSON.stringify(args)})`);

      const result = this._MOCK_RESPONSES[name] || {status: 'ok', message: 'Acknowledged'};

      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({
          type: 'tool_response',
          id: call.id,
          name: name,
          response: result,
        }));
      }
    }
  }

  _enqueuePlayback(b64) {
    if (!this._playbackCtx) return;
    const raw = atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const int16 = new Int16Array(bytes.buffer);

    // Convert int16 → float32
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768;
    }

    const buffer = this._playbackCtx.createBuffer(1, float32.length, 24000);
    buffer.getChannelData(0).set(float32);

    const source = this._playbackCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(this._playbackCtx.destination);

    const now = this._playbackCtx.currentTime;
    const startTime = Math.max(now, this._playbackTime);
    source.start(startTime);
    this._playbackTime = startTime + buffer.duration;
  }

  _stopPlayback() {
    // Reset playback schedule (barge-in)
    this._playbackTime = 0;
  }

  _arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  _emitState(state) {
    if (this._onStateChange) this._onStateChange(state);
  }
}

// ─── Dashboard Controller ──────────────────────────────────────
class Dashboard {
  constructor() {
    // State
    this._demoMode = false;
    this._currentState = 'IDLE';
    this._missionActive = false;
    this._currentPhase = null;
    this._reportData = null;
    this._currentTarget = '';

    // Components
    this._ws = new WSManager(this);
    this._canvas = new CanvasRenderer(document.getElementById('video-canvas'));
    this._timeline = new PhaseTimeline();
    this._flightStats = new FlightStats();
    this._log = new MissionLog();

    // DOM refs
    this._connDot = document.getElementById('conn-dot');
    this._connText = document.getElementById('conn-text');
    this._demoBadge = document.getElementById('demo-badge');
    this._placeholder = document.getElementById('video-placeholder');
    this._statusBadge = document.getElementById('status-badge');
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
    this._btnMic = document.getElementById('btn-mic');
    this._micLabel = document.getElementById('mic-label');

    this._micLabel.textContent = 'Mic Off';

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
    this._flightStats.onConnected();
  }

  onDisconnected() {
    this._connDot?.classList.remove('connected');
    if (this._connText) this._connText.textContent = 'Disconnected';
    this._flightStats.onDisconnected();
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
      transcript:   this._onTranscript,
      ai_activity:  this._onAIActivity,
      ai_result:    this._onAIResult,
      report_data:  this._onReportData,
      mic_state:    this._onMicState,
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

    this._flightStats.onTelemetry(data);
    this._canvas.updateTelemetry({ battery, altitude, temp });
  }

  _onStatus(data) {
    const state = data.state || 'IDLE';
    const phase = data.phase || null;
    const step = data.step;
    const maxSteps = data.max_steps;
    const target = data.target;
    const missionStatus = data.status || null;

    this._currentState = state;
    this._missionActive = ['searching', 'approaching', 'repositioning', 'inspecting'].includes(missionStatus);
    this._currentPhase = phase;
    if (target) this._currentTarget = target;

    // Status badge
    this._updateStatusBadge(state);

    // Phase timeline
    this._timeline.update(phase);
    this._flightStats.onStatus(data);
    this._canvas.updateTelemetry({ phase, step, maxSteps, missionElapsed: this._flightStats.getElapsedSeconds() });

    // Perception target
    if (target && this._percTarget) this._percTarget.textContent = target;

    // Button states
    this._updateButtons();

    // Skip button label
    this._updateSkipLabel(phase);

    // Reset on idle
    if (state === 'IDLE' || state === 'READY') {
      this._timeline.reset();
      this._flightStats.reset();
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
    this._flightStats.onLog(data);
  }

  _onTranscript(data, timestamp) {
    this._log.appendVoice(data.speaker, data.text, timestamp);
  }

  _onAIActivity(data) {
    this._flightStats.onAIActivity(data);
  }

  _onAIResult(data, timestamp) {
    this._log.addResultCard(data, timestamp);
    this._flightStats.onAIResult(data);
  }

  _onReportData(data) {
    this._reportData = data;
    if (this._btnReport) this._btnReport.style.display = '';
    this._updateConditionalGroup();
  }

  _updateConditionalGroup() {
    const group = document.getElementById('ctrl-conditional');
    if (!group) return;
    const hasVisible = Array.from(group.querySelectorAll('.btn'))
      .some(btn => btn.style.display !== 'none');
    group.style.display = hasVisible ? '' : 'none';
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
    this._btnPause?.addEventListener('click', () => {
      if (this._missionActive) {
        this._sendCommand('abort_mission');
      } else {
        this._sendCommand('pause');
      }
    });
    this._btnLand?.addEventListener('click', () => this._sendCommand('land'));
    this._btnEstop?.addEventListener('click', () => this._sendCommand('emergency_land'));
    this._btnSkip?.addEventListener('click', () => this._sendCommand('skip_phase'));
    this._btnReport?.addEventListener('click', () => this._generateReport());
    this._btnMic?.addEventListener('click', () => this._toggleMic());
  }

  _toggleMic() {
    this._ws.send({ type: 'command', action: 'mic_toggle' });
  }

  _onMicState(data) {
    this._updateMicUI(data.muted);
  }

  _updateMicUI(muted) {
    if (!this._btnMic) return;
    this._btnMic.classList.remove('mic-active', 'connecting', 'recording');
    if (!muted) {
      this._btnMic.classList.add('mic-active');
    }
    if (this._micLabel) this._micLabel.textContent = muted ? 'Mic Off' : 'Mic On';
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

    if (this._btnStart) this._btnStart.disabled = !idle && !this._missionActive;
    if (this._btnPause) {
      if (this._missionActive) {
        this._btnPause.disabled = false;
        this._btnPause.textContent = 'Stop Mission';
        this._btnPause.classList.add('btn-abort');
      } else {
        this._btnPause.disabled = !executing;
        this._btnPause.textContent = 'Pause';
        this._btnPause.classList.remove('btn-abort');
      }
    }
    if (this._btnLand) this._btnLand.disabled = !executing && !this._missionActive && s !== 'COMPLETE';

    // Target input disabled while executing or mission active
    if (this._targetInput) this._targetInput.disabled = executing || this._missionActive;
  }

  _updateSkipLabel(phase) {
    if (!this._btnSkip) return;
    if (!this._demoMode) { this._btnSkip.style.display = 'none'; this._updateConditionalGroup(); return; }

    this._btnSkip.style.display = '';
    if (phase === 'approach') {
      this._btnSkip.textContent = 'Next Step';
    } else if (phase === 'inspect') {
      this._btnSkip.textContent = 'Skip Wait';
    } else {
      this._btnSkip.textContent = 'Skip Phase';
    }
    this._updateConditionalGroup();
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
      `<span style="display:inline-block;padding:2px 8px;background:rgba(52,168,83,0.06);color:#34a853;border-radius:10px;font-size:0.75rem;font-weight:600;margin-right:4px">${p}</span>`
    ).join('');

    // Condition badge color
    const conditionColor = (() => {
      const c = (result.condition || '').toLowerCase();
      if (c === 'excellent' || c === 'good') return { bg: 'rgba(52,168,83,0.08)', fg: '#34a853' };
      if (c === 'fair') return { bg: 'rgba(251,188,4,0.1)', fg: '#e37400' };
      return { bg: 'rgba(234,67,53,0.08)', fg: '#ea4335' };
    })();

    // Build per-angle cards
    const perAngleCards = (result.per_angle || []).map(pa => {
      // Try to match a frame by label containing the angle name
      const matchedFrame = (rd.inspection_frames || []).find(f =>
        f.label && pa.angle && f.label.toLowerCase().includes(pa.angle.toLowerCase())
      );
      const thumb = matchedFrame
        ? `<img src="data:image/jpeg;base64,${matchedFrame.base64}" style="width:120px;height:90px;object-fit:cover;border-radius:6px;border:1px solid #dadce0;flex-shrink:0">`
        : '';
      return `<div style="display:flex;gap:12px;align-items:flex-start;padding:10px 12px;background:#f8f9fa;border-radius:8px;margin-bottom:8px">
        ${thumb}
        <div>
          <div style="font-size:0.75rem;font-weight:600;color:#5f6368;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:2px">${escapeHtml(pa.angle)}</div>
          <div style="font-size:0.85rem">${escapeHtml(pa.observation)}</div>
        </div>
      </div>`;
    }).join('');

    // Build visible text chips
    const textChips = (result.visible_text || []).map(t =>
      `<span style="display:inline-block;padding:3px 10px;background:#e8f0fe;color:#1967d2;border-radius:12px;font-family:'Roboto Mono',monospace;font-size:0.8rem;font-weight:500;margin:3px 4px">${escapeHtml(t)}</span>`
    ).join('');

    // Build damage section
    const damageItems = result.damage_details || [];
    const damageSection = damageItems.length > 0
      ? `<div style="padding:12px 16px;background:rgba(234,67,53,0.04);border:1px solid rgba(234,67,53,0.15);border-radius:8px;margin:8px 0">
          <ul style="padding-left:20px;margin:0">${damageItems.map(d => `<li style="font-size:0.85rem;margin-bottom:4px;color:#c5221f">${escapeHtml(d)}</li>`).join('')}</ul>
        </div>`
      : `<div style="padding:10px 16px;background:rgba(52,168,83,0.04);border:1px solid rgba(52,168,83,0.15);border-radius:8px;margin:8px 0;font-size:0.85rem;color:#34a853">No damage detected — object appears clean.</div>`;

    const html = `<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Mission Report — ${escapeHtml(meta.target || 'Unknown')}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Roboto:wght@400;500;700&family=Roboto+Mono:wght@400;500&display=swap');
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'Roboto',sans-serif; background:#f8f9fa; color:#202124; padding:40px; line-height:1.6; }
  .report { max-width:800px; margin:0 auto; background:white; border-radius:12px; border:1px solid #dadce0; overflow:hidden; }
  .report-header { padding:24px 32px; border-bottom:1px solid #dadce0; display:flex; align-items:center; justify-content:space-between; }
  .report-title { font-family:'Poppins',sans-serif; font-size:1.8rem; font-weight:600; }
  .report-subtitle { font-size:0.75rem; color:#5f6368; text-transform:uppercase; letter-spacing:0.1em; }
  .report-body { padding:24px 32px; }
  h2 { font-size:0.8rem; text-transform:uppercase; letter-spacing:0.08em; color:#5f6368; margin:20px 0 8px; font-weight:600; }
  .frames { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:12px 0; }
  .frames img { width:100%; border-radius:8px; border:1px solid #dadce0; }
  .frames .frame-label { font-size:0.7rem; color:#80868b; text-align:center; margin-top:4px; }
  .findings { padding:12px 16px; background:#f1f3f4; border-radius:8px; margin:8px 0; }
  .findings p { font-size:0.9rem; margin-bottom:8px; }
  .findings ul { padding-left:20px; }
  .findings li { font-size:0.85rem; margin-bottom:4px; }
  .meta-table { width:100%; border-collapse:collapse; margin:12px 0; }
  .meta-table td { padding:6px 0; font-size:0.85rem; border-bottom:1px solid #e8eaed; }
  .meta-table td:first-child { color:#5f6368; width:140px; }
  .confidence { display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:600; }
  .report-footer { padding:16px 32px; border-top:1px solid #dadce0; text-align:center; font-size:0.7rem; color:#80868b; }
  @media print { body { padding:0; background:white; } .report { border:none; border-radius:0; } }
</style></head><body>
<div class="report">
  <div class="report-header">
    <div>
      <div class="report-title">Drone Copilot</div>
      <div class="report-subtitle">Mission Report</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:0.85rem;font-weight:600">${escapeHtml(meta.target || 'Target')}</div>
      <div style="font-size:0.7rem;color:#80868b">${new Date().toLocaleDateString()} ${new Date().toLocaleTimeString()}</div>
    </div>
  </div>
  <div class="report-body">
    ${result.object_identity ? `<div style="padding:14px 20px;background:linear-gradient(135deg,#e8f0fe,#f0f4ff);border-left:4px solid #4285f4;border-radius:0 8px 8px 0;margin-bottom:16px">
      <div style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;color:#5f6368;margin-bottom:2px">Object Identified</div>
      <div style="font-size:1.15rem;font-weight:600;color:#1a73e8">${escapeHtml(result.object_identity)}</div>
    </div>` : ''}

    <h2>Captured Frames</h2>
    <div class="frames">
      ${rd.inspection_frames && rd.inspection_frames.length > 0 ? (() => {
        let html = '';
        html += '<div>' +
          (rd.acquisition_frame ? `<img src="data:image/jpeg;base64,${rd.acquisition_frame}">` : '<div style="background:#f8f9fa;aspect-ratio:4/3;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#80868b">No frame</div>') +
          '<div class="frame-label">Acquisition</div></div>';
        for (const f of rd.inspection_frames) {
          html += '<div>' +
            `<img src="data:image/jpeg;base64,${f.base64}">` +
            `<div class="frame-label">${escapeHtml(f.label)}</div></div>`;
        }
        return html;
      })() : `<div>
        ${rd.acquisition_frame ? `<img src="data:image/jpeg;base64,${rd.acquisition_frame}">` : '<div style="background:#f8f9fa;aspect-ratio:4/3;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#80868b">No frame</div>'}
        <div class="frame-label">Acquisition</div>
      </div>
      <div>
        ${rd.inspection_frame ? `<img src="data:image/jpeg;base64,${rd.inspection_frame}">` : '<div style="background:#f8f9fa;aspect-ratio:4/3;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#80868b">No frame</div>'}
        <div class="frame-label">Inspection</div>
      </div>`}
    </div>

    ${perAngleCards ? `<h2>Per-Angle Observations</h2>${perAngleCards}` : ''}

    ${textChips ? `<h2>Visible Text &amp; Markings</h2>
    <div style="padding:12px 16px;background:#f8f9fa;border-radius:8px;margin:8px 0;display:flex;flex-wrap:wrap;gap:2px">${textChips}</div>` : ''}

    <h2>Inspection Findings</h2>
    <div class="findings">
      <p>${escapeHtml(result.description || 'No description available')}
        ${result.condition ? ` <span class="confidence" style="background:${conditionColor.bg};color:${conditionColor.fg}">${escapeHtml(result.condition)}</span>` : ''}
      </p>
      ${findings ? '<ul>' + findings + '</ul>' : ''}
      ${result.confidence !== undefined ? `<div style="margin-top:8px"><span class="confidence" style="background:rgba(161,66,244,0.06);color:#a142f4">${Math.round(result.confidence * 100)}% confidence</span></div>` : ''}
    </div>

    <h2>Damage Assessment</h2>
    ${damageSection}

    <h2>Mission Summary</h2>
    <table class="meta-table">
      <tr><td>Target</td><td>${escapeHtml(meta.target || '—')}</td></tr>
      <tr><td>Duration</td><td>${meta.duration_seconds ? meta.duration_seconds.toFixed(1) + 's' : '—'}</td></tr>
      <tr><td>Battery</td><td>${meta.battery_start ?? '—'}% \u2192 ${meta.battery_end ?? '—'}%</td></tr>
      <tr><td>Phases</td><td>${phases || '—'}</td></tr>
    </table>
  </div>
  <div class="report-footer">Generated by Drone Copilot \u2014 Gemini Live Agent Challenge</div>
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
