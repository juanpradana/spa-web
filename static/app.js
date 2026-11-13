/**
 * Prism — Client Application
 * - Autentikasi JWT HS256 & RBAC
 * - Stream WebSocket Biner: /ws/spectrum, /ws/audio, /ws/events
 * - Render Spektrum 60 FPS (Canvas 2D DPI-aware, Peak Hold, Marker)
 * - Waterfall Spektrogram Bergulir (Turbo / Viridis / Jet)
 * - Web Audio API Live Player (Float32 PCM Streaming)
 */

// --------------------------------------------------------------------------- //
// State Aplikasi
// --------------------------------------------------------------------------- //
const TOKEN_KEY = "prism_token";
const LEGACY_TOKEN_KEY = "handfish_token";   // migrasi mulus dari nama lama

const state = {
  token: localStorage.getItem(TOKEN_KEY) || localStorage.getItem(LEGACY_TOKEN_KEY) || null,
  user: null,
  activeView: "monitor",
  device: null,
  rfState: "IDLE",

  // Spektrum & Waterfall
  spectrumFrame: null,
  peakLevels: null,
  peakHold: false,
  scale: "auto", // "auto" | "60" | "70" | "80" | "100" (rentang tampilan dB)
  refLevelDbm: -20,
  minLevelDbm: -120,
  smoothedNoiseFloor: null,
  rbwHz: 100000,
  dwellFreqMhz: null,
  lastFrameAt: 0,

  // Mode trace (spektrum)
  traceRealtime: true,
  traceMaxHold: false,
  traceAverage: false,
  avgLevels: null,
  avgAlpha: 0.2, // EMA: A(t) = (1-a)*A(t-1) + a*L(t)

  // Marker spektrum
  markerMode: "none", // "none" | "peak" | "manual"
  markerPeak: null, // { freqHz, dbm, index }
  markerManual: null, // { freqHz, dbm, index }
  markerDragActive: false,

  // Parameter kanal preset aktif (untuk legenda ITU-R)
  presetMeta: null,

  // Palet Warna Waterfall
  paletteName: "turbo",
  waterfallGain: 0,
  waterfallCanvas: null,
  waterfallCtx: null,

  // Audio Player
  audioCtx: null,
  audioGainNode: null,
  audioNextPlayTime: 0,
  isListening: false,
  listenFreqHz: 0,          // frekuensi audio aktif (selaras dengan engine)
  listenDemod: "FM",

  // Geser frekuensi audio langsung pada kanvas (drag garis pembawa)
  listenDragActive: false,
  listenDragStartX: 0,
  listenDragFreqHz: 0,
  listenDragTimer: null,
  listenDragMoved: false,    // membedakan klik (retune sekali) dari tarikan

  // Rekam audio manual
  isManualRecording: false,
  manualRecordStartedAt: 0,
  manualRecordTimerId: null,
  manualRecordFilename: null,

  // Pengukuran okupansi
  occupancyTracking: true,

  // Tabel Okupansi
  occChannels: [],
  occSortCol: "occupancy_pct",
  occSortAsc: false,
  occSelectedKeys: new Set(),

  // Rekaman
  recordings: [],
  recSelectedFilenames: new Set(),

  // Crop & Pan spektrum
  cropActive: false,
  cropStartX: 0,
  cropCurrentX: 0,
  panActive: false,
  panStartX: 0,
  wheelZoomTimeout: null,

  // Anti-spam toast bila polling status gagal berulang
  statusErrShown: false,
};

// --------------------------------------------------------------------------- //
// Palet Warna Spektrogram (Turbo, Viridis, Jet, Gray)
// --------------------------------------------------------------------------- //
function generatePalette(type) {
  const lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) {
    const t = i / 255.0;
    let r = 0, g = 0, b = 0;

    if (type === "turbo") {
      // Polinomial aproksimasi Turbo Colormap Google
      r = 34.61 + t * (1172.4 + t * (-10729.0 + t * (33048.0 + t * (-41555.0 + t * 18767.0))));
      g = -19.1 + t * (785.2 + t * (-7023.4 + t * (27029.4 + t * (-40553.3 + t * 21053.8))));
      b = 17.47 + t * (1753.1 + t * (-11507.0 + t * (26361.0 + t * (-24795.0 + t * 8831.0))));
    } else if (type === "viridis") {
      r = Math.sin(t * Math.PI) * 120 + t * 180;
      g = t * 230;
      b = (1.0 - t) * 180 + 70;
    } else if (type === "jet") {
      r = Math.min(Math.max(1.5 - Math.abs(t * 4 - 3), 0), 1) * 255;
      g = Math.min(Math.max(1.5 - Math.abs(t * 4 - 2), 0), 1) * 255;
      b = Math.min(Math.max(1.5 - Math.abs(t * 4 - 1), 0), 1) * 255;
    } else {
      // Grayscale
      r = i; g = i; b = i;
    }

    lut[i * 4 + 0] = Math.min(255, Math.max(0, Math.round(r)));
    lut[i * 4 + 1] = Math.min(255, Math.max(0, Math.round(g)));
    lut[i * 4 + 2] = Math.min(255, Math.max(0, Math.round(b)));
    lut[i * 4 + 3] = 255;
  }
  return lut;
}

let colorLUT = generatePalette("turbo");

// --------------------------------------------------------------------------- //
// REST API Client
// --------------------------------------------------------------------------- //
async function api(path, options = {}) {
  const headers = options.headers || {};
  // Token yang benar-benar dipakai untuk permintaan ini, agar respons 401
  // yang datang terlambat tidak menendang sesi yang baru saja dibuat.
  const tokenUsed = state.token;
  if (tokenUsed) {
    headers["Authorization"] = `Bearer ${tokenUsed}`;
  }
  if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  options.headers = headers;

  const res = await fetch(path, options);
  if (res.status === 401) {
    // Hanya akhiri sesi bila permintaan ini membawa token DAN token itu masih
    // yang berlaku sekarang. Tanpa penjaga ini, polling latar belakang yang
    // memakai token kedaluwarsa akan menghapus token hasil login yang baru.
    if (tokenUsed && state.token === tokenUsed) {
      logout();
      throw new Error("Sesi telah kedaluwarsa. Silakan login kembali.");
    }
    throw new Error("Kredensial tidak sah.");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err.detail) msg = err.detail;
    } catch {}
    throw new Error(msg);
  }
  const ctype = res.headers.get("content-type");
  if (ctype && ctype.includes("application/json")) {
    return await res.json();
  }
  return res;
}

// --------------------------------------------------------------------------- //
// Autentikasi
// --------------------------------------------------------------------------- //
async function checkAuth() {
  if (!state.token) {
    showLogin();
    return;
  }
  try {
    const user = await api("/api/auth/me");
    state.user = user;
    showApp();
  } catch {
    logout();
  }
}

function showLogin() {
  document.getElementById("loginScreen").hidden = false;
  document.getElementById("app").hidden = true;
}

function showApp() {
  document.getElementById("loginScreen").hidden = true;
  document.getElementById("app").hidden = false;

  document.getElementById("userName").textContent = state.user.username;
  document.getElementById("userRole").textContent = state.user.role;

  // Sembunyikan elemen admin jika bukan admin
  if (state.user.role !== "admin") {
    document.querySelectorAll('[data-view="admin"]').forEach(el => el.style.display = "none");
    const rBtn = document.getElementById("resetOccBtn");
    if (rBtn) rBtn.style.display = "none";
  }

  initApp();
  maybeShowPasswordGate();
}

// --------------------------------------------------------------------------- //
// Gerbang wajib ganti kata sandi
//
// Akun yang dibuat otomatis (atau direset admin) menyimpan
// `must_change_password`. Tanpa gerbang ini peringatan itu hanya muncul sekali
// di konsol server saat basis data pengguna pertama dibuat — setelah itu tidak
// ada lagi yang menampilkannya, sehingga kata sandi awal tetap terpakai.
// --------------------------------------------------------------------------- //
let pwGateShown = false;

function maybeShowPasswordGate() {
  if (!state.user || !state.user.must_change_password) return;
  if (pwGateShown) return;
  pwGateShown = true;
  const gate = document.getElementById("pwGate");
  const errBox = document.getElementById("pgError");
  if (errBox) errBox.hidden = true;
  if (gate) gate.hidden = false;
  document.getElementById("pgOld")?.focus();
}

document.getElementById("pwGateForm")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const errBox = document.getElementById("pgError");
  const btn = document.getElementById("pgSubmit");
  const oldPw = document.getElementById("pgOld").value;
  const newPw = document.getElementById("pgNew").value;
  const newPw2 = document.getElementById("pgNew2").value;

  errBox.hidden = true;

  if (newPw.length < 8) {
    errBox.textContent = "Kata sandi baru minimal 8 karakter.";
    errBox.hidden = false;
    return;
  }
  if (newPw !== newPw2) {
    errBox.textContent = "Ulangi kata sandi baru dengan tepat.";
    errBox.hidden = false;
    return;
  }
  if (newPw === oldPw) {
    errBox.textContent = "Kata sandi baru harus berbeda dari yang lama.";
    errBox.hidden = false;
    return;
  }

  btn.disabled = true;
  btn.textContent = "Menyimpan...";
  try {
    await api("/api/auth/change-password", {
      method: "POST",
      body: { old_password: oldPw, new_password: newPw },
    });
    toast("Kata sandi diperbarui. Silakan login ulang.", "ok");
    document.getElementById("pwGate").hidden = true;
    pwGateShown = false;
    // Stempel `pwd` di token ikut berubah, jadi token lama otomatis gugur.
    logout();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Simpan & Login Ulang";
  }
});


function logout() {
  state.token = null;
  state.user = null;
  state.activeView = "monitor";
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LEGACY_TOKEN_KEY);
  if (statusTimer) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
  closeWebSockets();
  // Sesi berakhir: gerbang ganti-sandi harus bisa muncul lagi pada login
  // berikutnya bila akun masih menandai `must_change_password`.
  pwGateShown = false;
  const gate = document.getElementById("pwGate");
  if (gate) gate.hidden = true;
  showLogin();
}

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const u = document.getElementById("loginUser").value.trim();
  const p = document.getElementById("loginPass").value;
  const errBox = document.getElementById("loginError");
  const btn = document.getElementById("loginBtn");

  errBox.hidden = true;
  btn.disabled = true;
  btn.textContent = "Memverifikasi...";

  try {
    const res = await api("/api/auth/login", {
      method: "POST",
      body: { username: u, password: p },
    });
    state.token = res.access_token;
    state.user = res.user;
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.removeItem(LEGACY_TOKEN_KEY);   // bersihkan kunci nama lama
    // Tampilkan aplikasi lebih dulu sehingga tunnel WebSocket dibangun dengan
    // token yang baru, lalu muat data.
    document.getElementById("loginScreen").hidden = true;
    document.getElementById("app").hidden = false;
    document.getElementById("userName").textContent = state.user.username;
    document.getElementById("userRole").textContent = state.user.role;
    initApp();
    maybeShowPasswordGate();
    toast("Login berhasil", "ok");
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Masuk";
  }
});

document.getElementById("logoutBtn").addEventListener("click", logout);

// --------------------------------------------------------------------------- //
// Inisialisasi Aplikasi
// --------------------------------------------------------------------------- //
let isInitialized = false;
let statusTimer = null;

function initApp() {
  // Penyiapan sekali saja (listener & kanvas) — jangan didaftarkan ulang
  // setiap kali pengguna login kembali.
  if (!isInitialized) {
    isInitialized = true;
    initNavigation();
    initSpectrumCanvas();
    initWaterfallCanvas();
    initControls();
  }

  connectWebSockets();
  refreshPresets();
  refreshStatus();
  refreshOccupancy();
  refreshRecordings();
  refreshSchedules();

  // Polling status background tiap 3 detik
  if (!statusTimer) statusTimer = setInterval(refreshStatus, 3000);
}

// --------------------------------------------------------------------------- //
// Navigasi Tampilan (Tab Views)
// --------------------------------------------------------------------------- //
function initNavigation() {
  const navBtns = document.querySelectorAll("[data-view]");
  navBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const view = btn.getAttribute("data-view");
      switchView(view);
    });
  });

  // Mobile drawer toggle
  const ctrlToggle = document.getElementById("controlsToggle");
  const ctrlBar = document.querySelector(".controlbar");
  const scrim = document.getElementById("scrim");

  if (ctrlToggle && ctrlBar && scrim) {
    ctrlToggle.addEventListener("click", () => {
      ctrlBar.classList.toggle("is-open");
      scrim.hidden = !ctrlBar.classList.contains("is-open");
    });
    scrim.addEventListener("click", () => {
      ctrlBar.classList.remove("is-open");
      scrim.hidden = true;
    });
  }

  // Theme Toggle
  const themeBtn = document.getElementById("themeToggle");
  themeBtn.addEventListener("click", () => {
    const html = document.documentElement;
    const cur = html.getAttribute("data-theme") || "dark";
    const next = cur === "dark" ? "light" : "dark";
    html.setAttribute("data-theme", next);
    colorLUT = generatePalette(state.paletteName);
  });
}

function switchView(viewName) {
  state.activeView = viewName;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("is-active"));
  document.querySelectorAll("[data-view]").forEach((b) => b.classList.remove("is-active"));

  const targetView = document.getElementById(`view-${viewName}`);
  if (targetView) targetView.classList.add("is-active");

  document.querySelectorAll(`[data-view="${viewName}"]`).forEach((b) => b.classList.add("is-active"));

  if (viewName === "occupancy") refreshOccupancy();
  if (viewName === "recordings") refreshRecordings();
  if (viewName === "schedule") refreshSchedules();
  if (viewName === "admin") refreshUsers();
}

// --------------------------------------------------------------------------- //
// WebSockets Connection Management
// --------------------------------------------------------------------------- //
let wsSpectrum = null;
let wsAudio = null;
let wsEvents = null;
let wsReconnectTimer = null;
let spectrumFrameSeq = 0;

function connectWebSockets() {
  if (!state.token) return;

  const loc = window.location;
  const proto = loc.protocol === "https:" ? "wss:" : "ws:";
  const base = `${proto}//${loc.host}`;

  // 1. Spectrum Stream
  wsSpectrum = new WebSocket(`${base}/ws/spectrum?token=${encodeURIComponent(state.token)}`);
  wsSpectrum.binaryType = "arraybuffer";
  wsSpectrum.onmessage = (e) => {
    if (typeof e.data !== "string") {
      parseSpectrumBinary(e.data);
    }
  };
  wsSpectrum.onclose = scheduleReconnect;

  // 2. Audio Stream
  wsAudio = new WebSocket(`${base}/ws/audio?token=${encodeURIComponent(state.token)}`);
  wsAudio.binaryType = "arraybuffer";
  wsAudio.onmessage = (e) => {
    if (typeof e.data !== "string" && state.isListening) {
      queueAudioChunk(e.data);
    }
  };
  wsAudio.onclose = scheduleReconnect;

  // 3. Events Stream
  wsEvents = new WebSocket(`${base}/ws/events?token=${encodeURIComponent(state.token)}`);
  wsEvents.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleSystemEvent(evt);
    } catch {}
  };
  wsEvents.onclose = scheduleReconnect;
}

function closeWebSockets() {
  if (wsSpectrum) wsSpectrum.close();
  if (wsAudio) wsAudio.close();
  if (wsEvents) wsEvents.close();
}

function scheduleReconnect() {
  if (!wsReconnectTimer && state.token) {
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      connectWebSockets();
    }, 2500);
  }
}

// --------------------------------------------------------------------------- //
// Parser Spektrum Biner & Rendering
// --------------------------------------------------------------------------- //
function parseSpectrumBinary(buffer) {
  if (buffer.byteLength < 44) return;
  const view = new DataView(buffer);

  // Verifikasi magic "SPEC". Server memakai struct.pack("<4s", b"SPEC")
  // sehingga byte di memori adalah 'S','P','E','C' (0x53,0x50,0x45,0x43);
  // dibaca sebagai uint32 little-endian nilainya 0x43455053.
  if (view.getUint8(0) !== 0x53 || view.getUint8(1) !== 0x50 ||
      view.getUint8(2) !== 0x45 || view.getUint8(3) !== 0x43) {
    return;
  }

  const length = view.getUint32(4, true);
  if (length === 0 || 40 + length * 4 > buffer.byteLength) return;
  const timestamp = view.getFloat64(8, true);
  const startHz = view.getFloat64(16, true);
  const binSizeHz = view.getFloat64(24, true);
  const noiseFloor = view.getFloat32(32, true);
  const threshold = view.getFloat32(36, true);

  const levels = new Float32Array(buffer, 40, length);

  // Auto-range Y: noise floor dihaluskan (EMA) supaya garis dasar tidak bergetar
  // tiap frame, lalu dipakai untuk menempatkan noise floor ~18% dari dasar.
  state.smoothedNoiseFloor =
    state.smoothedNoiseFloor === null
      ? noiseFloor
      : state.smoothedNoiseFloor * 0.85 + noiseFloor * 0.15;
  applyYRange();

  // Increment frame sequence: mencegah spektrum lama menimpa frame terkini
  const thisSeq = ++spectrumFrameSeq;
  state.spectrumFrame = {
    seq: thisSeq,
    length,
    timestamp,
    startHz,
    binSizeHz,
    stopHz: startHz + length * binSizeHz,   // geometri kanvas pan/zoom bergantung pada ini
    noiseFloor,
    threshold,
    levels,
  };

  // Multi-trace buffers
  // 1. Max Hold
  if (state.traceMaxHold) {
    if (!state.peakLevels || state.peakLevels.length !== length) {
      state.peakLevels = new Float32Array(levels);
    } else {
      for (let i = 0; i < length; i++) {
        if (levels[i] > state.peakLevels[i]) state.peakLevels[i] = levels[i];
      }
    }
  }

  // 2. Average (Exponential Moving Average / EMA)
  if (state.traceAverage) {
    if (!state.avgLevels || state.avgLevels.length !== length) {
      state.avgLevels = new Float32Array(levels);
    } else {
      const a = state.avgAlpha;
      const invA = 1.0 - a;
      for (let i = 0; i < length; i++) {
        state.avgLevels[i] = invA * state.avgLevels[i] + a * levels[i];
      }
    }
  }

  // Pelacakan Marker Puncak (Peak Search): cari level daya terbesar di sapuan
  let maxIdx = 0;
  let maxDbm = levels[0];
  for (let i = 1; i < length; i++) {
    if (levels[i] > maxDbm) {
      maxDbm = levels[i];
      maxIdx = i;
    }
  }
  const peakFreqHz = startHz + maxIdx * binSizeHz;
  state.markerPeak = { freqHz: peakFreqHz, dbm: maxDbm, index: maxIdx };

  // Perbarui nilai dBm marker manual jika operator sedang menaruh marker manual
  if (state.markerManual) {
    const mFreq = state.markerManual.freqHz;
    let mIdx = Math.round((mFreq - startHz) / binSizeHz);
    mIdx = Math.max(0, Math.min(length - 1, mIdx));
    state.markerManual.index = mIdx;
    state.markerManual.dbm = levels[mIdx];
  }

  // Perbarui status tombol dengar frekuensi marker
  updateMarkerListenButton();

  // Update UI legend teknis (ITU-R SM.1880)
  const spanHz = length * binSizeHz;
  const stopHz = startHz + spanHz;
  const elSpan = document.getElementById("spanInfo");
  if (elSpan) {
    elSpan.textContent = `Span: ${(spanHz / 1e6).toFixed(3)} MHz (${(startHz / 1e6).toFixed(3)} – ${(stopHz / 1e6).toFixed(3)} MHz)`;
  }
  const rbwVal = state.rbwHz || binSizeHz;
  const elRbw = document.getElementById("rbwInfo");
  if (elRbw) {
    elRbw.textContent = `RBW: ${rbwVal >= 1e6 ? `${(rbwVal / 1e6).toFixed(2)} MHz` : `${(rbwVal / 1e3).toFixed(1)} kHz`}`;
  }
  const elVbw = document.getElementById("vbwInfo");
  if (elVbw) {
    const vbwVal = state.presetMeta?.vbw_hz || rbwVal;
    elVbw.textContent = `VBW: ${vbwVal >= 1e6 ? `${(vbwVal / 1e6).toFixed(2)} MHz` : `${(vbwVal / 1e3).toFixed(1)} kHz`}`;
  }
  const elStep = document.getElementById("stepInfo");
  if (elStep) {
    const stepVal = state.presetMeta?.channel_step_hz;
    elStep.textContent = `Step: ${stepVal ? (stepVal >= 1e6 ? `${(stepVal / 1e6).toFixed(3)} MHz` : `${(stepVal / 1e3).toFixed(1)} kHz`) : "—"}`;
  }
  const elChBw = document.getElementById("chbwInfo");
  if (elChBw) {
    const bwVal = state.presetMeta?.channel_bw_hz;
    elChBw.textContent = `Ch BW: ${bwVal ? (bwVal >= 1e6 ? `${(bwVal / 1e6).toFixed(3)} MHz` : `${(bwVal / 1e3).toFixed(1)} kHz`) : "—"}`;
  }
  const elNf = document.getElementById("nfInfo");
  if (elNf) {
    elNf.textContent = `Noise floor: ${Number.isFinite(noiseFloor) ? noiseFloor.toFixed(1) : "—"} dBm`;
  }
  const elThr = document.getElementById("thrInfo");
  if (elThr) {
    elThr.textContent = `Threshold: ${Number.isFinite(threshold) ? threshold.toFixed(1) : "—"} dBm`;
  }

  state.lastFrameAt = performance.now();
  scheduleDraw();
  drawWaterfallRow(levels, noiseFloor);
}

// --------------------------------------------------------------------------- //
// Canvas Spektrum (DPI-Aware 60 FPS)
// --------------------------------------------------------------------------- //
let specCanvas, specCtx;
let markerX = null;

// Penjaga rantai requestAnimationFrame. Sebelum ini, drawSpectrum() menjadwalkan
// frame berikutnya setiap kali dipanggil DAN refreshStatus() (poll 3 detik)
// menambah satu lagi — sehingga sesi dengar panjang menumpuk ratusan rantai rAF
// paralel sampai ribuan gambar per detik (CPU 100%, UI tersendat).
let drawRafPending = false;

/**
 * Jadwalkan SATU frame gambar. Aman dipanggil dari banyak tempat sekaligus:
 * selama satu frame belum tergambar, pemanggilan berikutnya tidak menumpuk.
 */
function scheduleDraw() {
  if (drawRafPending) return;
  drawRafPending = true;
  requestAnimationFrame(() => {
    drawRafPending = false;
    drawSpectrum();
  });
}

function initSpectrumCanvas() {
  specCanvas = document.getElementById("spectrumCanvas");
  specCtx = specCanvas.getContext("2d");

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = specCanvas.getBoundingClientRect();
    specCanvas.width = rect.width * dpr;
    specCanvas.height = rect.height * dpr;
    specCtx.scale(dpr, dpr);
    drawSpectrum();
  }
  window.addEventListener("resize", resize);
  setTimeout(resize, 50);

  // Interaksi sentuh / kursor untuk Marker
  const readout = document.getElementById("markerReadout");

  const _freqAtX = (clientX) => {
    const rect = specCanvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(rect.width, clientX - rect.left));
    const f = state.spectrumFrame;
    if (!f) return null;
    const ratio = x / rect.width;
    const idx = Math.round(ratio * (f.length - 1));
    return { idx, freqHz: f.startHz + idx * f.binSizeHz, x };
  };

  specCanvas.addEventListener("mouseleave", () => {
    markerX = null;
    // Tombol terlepas di luar kanvas: tuntaskan geser frekuensi audio
    // supaya tidak menggantung setengah jalan.
    if (state.listenDragActive) {
      _setListenFreq(state.listenDragFreqHz, false);
      _endListenDrag();
    }
    state.markerDragActive = false;
    readout.hidden = true;
    drawSpectrum();
  });

  function placeManualMarker(clientX) {
    const hit = _freqAtX(clientX);
    if (!hit) return;
    state.markerMode = "manual";
    state.markerManual = { freqHz: hit.freqHz, dbm: null, index: hit.idx };
    setMarkerButtons();
    updateMarkerListenButton();
    drawSpectrum();
  }

  // ================================================================
  // Spectrum Canvas Interactions: Click-to-Listen, Box-Zoom, Pan, Wheel
  // ================================================================
  function _freqAtCanvasX(clientX) {
    const rect = specCanvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(rect.width, clientX - rect.left));
    const f = state.spectrumFrame;
    if (!f) return null;
    const ratio = x / rect.width;
    const idx = Math.round(ratio * (f.length - 1));
    return { idx, freqHz: f.startHz + idx * f.binSizeHz, x };
  }

  // -- geser frekuensi audio saat mendengarkan --------------------------- //
  // Lebar passband per mode demod kini ada di _demodBandwidthHz() tingkat modul
  // (dipakai juga oleh drawCarrierLine di luar closure ini).

  /** Posisi X (relatif kanvas) dari frekuensi audio — `null` bila di luar sapuan. */
  function _listenLineX() {
    const f = state.spectrumFrame;
    const freq = state.listenDragActive ? state.listenDragFreqHz : state.listenFreqHz;
    if (!f || !Number.isFinite(freq) || freq <= 0) return null;
    const stopHz = f.startHz + f.length * f.binSizeHz;
    if (stopHz <= f.startHz) return null;
    const rect = specCanvas.getBoundingClientRect();
    return ((freq - f.startHz) / (stopHz - f.startHz)) * rect.width;
  }

  /** Kocok frekuensi audio sebesar `deltaPx` piksel kanvas (untuk pan/roda). */
  function _nudgeListenFreq(deltaPx) {
    const f = state.spectrumFrame;
    if (!f || !state.isListening) return;
    const rect = specCanvas.getBoundingClientRect();
    const spanHz = f.length * f.binSizeHz;
    const deltaHz = (deltaPx / rect.width) * spanHz;
    _setListenFreq(state.listenFreqHz + deltaHz, /*throttle*/ true);
  }

  /**
   * Set frekuensi audio baru dari kanvas. Selama tarikan, tampilan diperbarui
   * seketika (pra-lapar) sementara panggilan API dithrottle supaya backend
   * tidak dibanjiri `configure_audio` pada setiap piksel.
   */
  function _setListenFreq(freqHz, throttle) {
    const minF = 100_000;
    const maxF = 12_400_000_000;
    const clamped = Math.max(minF, Math.min(maxF, Math.round(freqHz)));
    if (!Number.isFinite(clamped) || clamped <= 0) return;
    state.listenDragFreqHz = clamped;
    _syncListenInputs(clamped);
    drawSpectrum();

    if (state.listenDragTimer) clearTimeout(state.listenDragTimer);
    const fire = () => {
      state.listenDragTimer = null;
      if (!state.isListening) return;
      startListening(clamped, getMonitorDemod());
    };
    if (throttle) {
      state.listenDragTimer = setTimeout(fire, 90);
    } else {
      fire();
    }
  }

  function _endListenDrag() {
    if (state.listenDragTimer) {
      clearTimeout(state.listenDragTimer);
      state.listenDragTimer = null;
    }
    state.listenDragActive = false;
    specCanvas.style.cursor = "";
  }


  function _drawCropOverlay() {
    const overlay = document.getElementById("cropOverlay");
    const box = document.getElementById("cropBox");
    const label = document.getElementById("cropLabel");
    if (!overlay || !box || !label) return;
    if (!state.cropActive || state.cropStartX === state.cropCurrentX) {
      overlay.hidden = true;
      return;
    }
    const rect = specCanvas.getBoundingClientRect();
    const startX = Math.min(state.cropStartX, state.cropCurrentX);
    const endX = Math.max(state.cropStartX, state.cropCurrentX);
    const width = Math.max(1, endX - startX);
    box.style.left = `${startX}px`;
    box.style.width = `${width}px`;

    // Hitung frekuensi
    const f = state.spectrumFrame;
    if (f) {
      const f1 = (f.startHz + (startX / rect.width) * (f.length - 1) * f.binSizeHz) / 1e6;
      const f2 = (f.startHz + (endX / rect.width) * (f.length - 1) * f.binSizeHz) / 1e6;
      const lo = Math.min(f1, f2).toFixed(4);
      const hi = Math.max(f1, f2).toFixed(4);
      const span = Math.abs(f2 - f1).toFixed(4);
      label.textContent = `Rentang: ${lo} – ${hi} MHz  (Span: ${span} MHz)`;
    }
    overlay.hidden = false;
  }

  function _applyCrop() {
    if (!state.spectrumFrame) return;
    const rect = specCanvas.getBoundingClientRect();
    const f = state.spectrumFrame;
    const startX = Math.min(state.cropStartX, state.cropCurrentX);
    const endX = Math.max(state.cropStartX, state.cropCurrentX);
    if (endX - startX < 15) return; // terlalu kecil, abaikan

    const f1 = f.startHz + (startX / rect.width) * (f.length - 1) * f.binSizeHz;
    const f2 = f.startHz + (endX / rect.width) * (f.length - 1) * f.binSizeHz;
    const center = (f1 + f2) / 2;
    const span = Math.max(100000, Math.abs(f2 - f1));

    api("/api/rf/sweep", {
      method: "POST",
      body: { center_hz: center, span_hz: span },
    }).then(() => {
      toast(`Rentang disesuaikan: ${(center / 1e6).toFixed(4)} MHz ± ${(span / 2 / 1e6).toFixed(4)} MHz`, "ok");
      // Input center & span di drawer akan tersinkron via refreshStatus
    }).catch(err => toast(err.message, "err"));
  }

  function _applyPan(deltaX) {
    if (!state.spectrumFrame) return;
    const f = state.spectrumFrame;
    if (state.isListening) {
      // Perangkat sedang mendemodulasi audio: geser frekuensi siaran,
      // bukan center sapuan (sapuan memang sedang dijeda — tak ada gunanya).
      _nudgeListenFreq(deltaX);
      return;
    }
    const rect = specCanvas.getBoundingClientRect();
    const stopHz = f.startHz + f.length * f.binSizeHz;
    const curCenter = (f.startHz + stopHz) / 2;
    const freqDelta = - (deltaX / rect.width) * (f.length * f.binSizeHz);
    const newCenter = curCenter + freqDelta;
    const minFreq = 100000;
    const maxFreq = 12400000000;
    const clamped = Math.max(minFreq + f.binSizeHz * f.length / 2, Math.min(maxFreq - f.binSizeHz * f.length / 2, newCenter));
    if (Math.abs(clamped - curCenter) < 1) return;
    api("/api/rf/sweep", { method: "POST", body: { center_hz: clamped } })
      .catch(err => toast(err.message, "err"));
  }

  specCanvas.addEventListener("click", (e) => {
    const hit = _freqAtCanvasX(e.clientX);
    if (!hit) return;

    // Click sesudah tarikan garis pembawa hanya menuntaskan posisi —
    // frekuensinya sudah dikirim saat `mouseup`, jangan dobel-panggil.
    if (state.listenDragMoved) {
      state.listenDragMoved = false;
      return;
    }

    // Alt+click atau double-click (detail: 2) -> mulai dengar di frekuensi ini
    if (e.altKey || e.detail === 2) {
      const demod = getMonitorDemod();
      startListening(hit.freqHz, demod);
      return;
    }

    // Jika sedang mendengarkan: klik normal = pindah frekuensi audio
    if (state.isListening) {
      const demod = getMonitorDemod();
      startListening(hit.freqHz, demod);
      // Tempatkan marker manual visual
      state.markerManual = { freqHz: hit.freqHz, dbm: null, index: hit.idx };
      setMarkerButtons();
      updateMarkerListenButton();
      drawSpectrum();
      return;
    }

    // Standar: klik kanan -> taruh marker manual
    if (state.markerMode === "manual") {
      placeManualMarker(e.clientX);
    }
  });

  specCanvas.addEventListener("mousedown", (e) => {
    // Tombol kiri drag -> crop box zoom (atau marker drag / geser frekuensi audio)
    if (e.button === 0) {
      // Saat mendengarkan, tekan di mana pun mulai tarikan untuk menyesuaikan
      // frekuensi siaran secara kontinu — ini cara ngepasin sinyal cepat.
      if (state.isListening) {
        const lineX = _listenLineX();
        const rect = specCanvas.getBoundingClientRect();
        const nearLine = lineX === null || Math.abs(e.clientX - rect.left - lineX) < 24;
        if (nearLine) {
          const hit = _freqAtCanvasX(e.clientX);
          if (hit) {
            state.listenDragActive = true;
            state.listenDragMoved = false;
            state.listenDragStartX = e.clientX;
            state.listenDragFreqHz = hit.freqHz;
            specCanvas.style.cursor = "ew-resize";
            drawSpectrum();
            return;
          }
        }
      }
      // Jika marker manual aktif dan klik dekat marker: mulai drag marker
      if (state.markerMode === "manual" && state.markerManual && state.spectrumFrame) {
        const f = state.spectrumFrame;
        const rect = specCanvas.getBoundingClientRect();
        const stopHz = f.startHz + f.length * f.binSizeHz;
        const mx = ((state.markerManual.freqHz - f.startHz) / (stopHz - f.startHz)) * rect.width;
        if (Math.abs(e.clientX - rect.left - mx) < 16) {
          state.markerDragActive = true;
          return;
        }
      }
      // Crop/box-zoom drag mulai
      state.cropActive = true;
      state.cropStartX = e.clientX - specCanvas.getBoundingClientRect().left;
      state.cropCurrentX = state.cropStartX;
      _drawCropOverlay();
      return;
    }
    // Tombol kanan / tengah drag -> pan frekuensi (audio bila sedang dengar)
    if (e.button === 2 || e.button === 1) {
      e.preventDefault();
      state.panActive = true;
      state.panStartX = e.clientX;
      specCanvas.style.cursor = "grabbing";
      return;
    }
  });

  specCanvas.addEventListener("mousemove", (e) => {
    const rect = specCanvas.getBoundingClientRect();
    markerX = e.clientX - rect.left;

    // Tarikan geser frekuensi audio: tampilan live, API dithrottle
    if (state.listenDragActive) {
      if (Math.abs(e.clientX - state.listenDragStartX) > 2) state.listenDragMoved = true;
      const hit = _freqAtCanvasX(e.clientX);
      if (hit) _setListenFreq(hit.freqHz, true);
      return;
    }

    updateMarkerReadout(markerX, rect.width, document.getElementById("markerReadout"));

    // Drag marker manual
    if (state.markerDragActive && state.markerMode === "manual") {
      const hit = _freqAtCanvasX(e.clientX);
      if (hit) {
        state.markerManual = { freqHz: hit.freqHz, dbm: null, index: hit.idx };
        updateMarkerListenButton();
        drawSpectrum();
      }
      return;
    }

    // Crop/box-zoom drag update
    if (state.cropActive) {
      state.cropCurrentX = e.clientX - rect.left;
      _drawCropOverlay();
      return;
    }

    // Pan drag update
    if (state.panActive) {
      const deltaX = e.clientX - state.panStartX;
      if (Math.abs(deltaX) > 8) { // threshold agar tidak terlalu sensitif
        _applyPan(deltaX);
        state.panStartX = e.clientX;
      }
      return;
    }
  });

  window.addEventListener("mouseup", () => {
    // Crop selesai
    if (state.cropActive) {
      state.cropActive = false;
      _applyCrop();
      _drawCropOverlay(); // sembunyikan
    }
    // Tarikan geser frekuensi audio tuntas: kirim posisi akhir tepat (tak dithrottle)
    if (state.listenDragActive) {
      _setListenFreq(state.listenDragFreqHz, false);
      _endListenDrag();
      drawSpectrum();
    }
    state.markerDragActive = false;

    // Pan selesai
    if (state.panActive) {
      state.panActive = false;
      specCanvas.style.cursor = "";
    }
  });

  // Prevent context menu on right-click drag
  specCanvas.addEventListener("contextmenu", (e) => {
    if (state.panActive) e.preventDefault();
  });

  // Wheel zoom (debounced)
  specCanvas.addEventListener("wheel", (e) => {
    if (!state.spectrumFrame) return;
    e.preventDefault();

    // Saat mendengarkan, zoom span tak ada gunanya (sapuan dijeda) — arahkan
    // roda ke penggeseran frekuensi audio supaya operator tetap bisa tuning.
    if (state.isListening) {
      const step = e.deltaY > 0 ? 40 : -40; // naik = turunkan freq
      _nudgeListenFreq(-step);
      return;
    }

    const dir = e.deltaY > 0 ? 1 : -1;
    const factor = dir > 0 ? 1.25 : 0.8;
    const f = state.spectrumFrame;
    const stopHz = f.startHz + f.length * f.binSizeHz;
    const newSpan = Math.max(100000, Math.min(12400000000, (stopHz - f.startHz) * factor));
    const newCenter = (f.startHz + stopHz) / 2;

    if (state.wheelZoomTimeout) clearTimeout(state.wheelZoomTimeout);
    state.wheelZoomTimeout = setTimeout(() => {
      api("/api/rf/sweep", { method: "POST", body: { center_hz: newCenter, span_hz: newSpan } })
        .catch(err => toast(err.message, "err"));
    }, 250);
  }, { passive: false });

  // Sentuh (tablet / ponsel)
  specCanvas.addEventListener("touchstart", (e) => {
    if (!e.touches.length) return;
    const touch = e.touches[0];
    const rect = specCanvas.getBoundingClientRect();
    const f = state.spectrumFrame;
    if (!f) return;

    // Sedang mendengarkan: jari mulai menarikan frekuensi audio
    if (state.isListening) {
      const hit = _freqAtCanvasX(touch.clientX);
      if (hit) {
        state.listenDragActive = true;
        state.listenDragMoved = false;
        state.listenDragStartX = touch.clientX;
        state.listenDragFreqHz = hit.freqHz;
        specCanvas.style.cursor = "ew-resize";
        drawSpectrum();
        e.preventDefault();
        return;
      }
    }

    const ratio = Math.max(0, Math.min(1, (touch.clientX - rect.left) / rect.width));
    const idx = Math.round(ratio * (f.length - 1));
    state.markerMode = "manual";
    state.markerManual = { freqHz: f.startHz + idx * f.binSizeHz, dbm: null, index: idx };
    setMarkerButtons();
    updateMarkerListenButton();
    drawSpectrum();
    e.preventDefault();
  }, { passive: false });

  specCanvas.addEventListener("touchmove", (e) => {
    if (!state.listenDragActive || !e.touches.length) return;
    const touch = e.touches[0];
    if (Math.abs(touch.clientX - state.listenDragStartX) > 2) state.listenDragMoved = true;
    const hit = _freqAtCanvasX(touch.clientX);
    if (hit) _setListenFreq(hit.freqHz, true);
    e.preventDefault();
  }, { passive: false });

  specCanvas.addEventListener("touchend", () => {
    if (!state.listenDragActive) return;
    _setListenFreq(state.listenDragFreqHz, false);
    _endListenDrag();
    drawSpectrum();
  });
}

function updateMarkerReadout(x, width, el) {
  if (!state.spectrumFrame) return;
  const f = state.spectrumFrame;
  const ratio = Math.max(0, Math.min(1, x / width));
  const idx = Math.floor(ratio * (f.length - 1));
  const freq = (f.startHz + idx * f.binSizeHz) / 1e6;
  const pwr = f.levels[idx];

  el.hidden = false;
  const dbmTxt = Number.isFinite(pwr) ? pwr.toFixed(1) : "—";
  el.textContent = `M: ${freq.toFixed(4)} MHz · ${dbmTxt} dBm`;
  drawSpectrum();
}

// Hitung rentang Y dari ref level, noise floor terukur, dan mode skala.
// Mode "auto" menempatkan noise floor sekitar 18% di atas dasar canvas sehingga
// trace tidak lagi terkompresi di 20 px terbawah saat ref level preset terlalu
// tinggi dibanding noise floor riil perangkat.
function applyYRange() {
  const ref = Number.isFinite(state.refLevelDbm) ? state.refLevelDbm : -20;
  const nf = state.smoothedNoiseFloor;

  if (state.scale !== "auto") {
    const span = parseFloat(state.scale);
    if (Number.isFinite(span) && span > 0) {
      state.minLevelDbm = ref - span;
      return;
    }
  }

  if (!Number.isFinite(nf)) {
    state.minLevelDbm = ref - 80;
    return;
  }

  // 12 dB margin di bawah noise floor, dibulatkan ke kelipatan 10 dB.
  const desired = Math.max(50, Math.ceil((ref - (nf - 12)) / 10) * 10);
  state.minLevelDbm = ref - desired;
}

function drawSpectrum() {
  if (!specCtx || !specCanvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = specCanvas.width / dpr;
  const h = specCanvas.height / dpr;

  specCtx.clearRect(0, 0, w, h);

  // Background Grid
  specCtx.strokeStyle = "rgba(42, 54, 71, 0.4)";
  specCtx.lineWidth = 1;
  const numGridY = 6;
  for (let i = 0; i <= numGridY; i++) {
    const y = (h / numGridY) * i;
    specCtx.beginPath();
    specCtx.moveTo(0, y);
    specCtx.lineTo(w, y);
    specCtx.stroke();

    const dbm = state.refLevelDbm - ((state.refLevelDbm - state.minLevelDbm) / numGridY) * i;
    specCtx.fillStyle = "rgba(132, 147, 166, 0.7)";
    specCtx.font = "10px monospace";
    specCtx.fillText(`${dbm.toFixed(0)} dBm`, 6, y - 4);
  }

  const f = state.spectrumFrame;
  const isListeningMode = state.isListening || state.rfState === "MANUAL_AUDIO";
  if (!f || (!isListeningMode && performance.now() - state.lastFrameAt > 3500)) {
    drawSpectrumStatus(w, h, isListeningMode ? "Demodulasi Audio Manual" : "Menunggu sapuan…");
    if (isListeningMode) scheduleDraw(); // jaga jedaan pulsa
    return;
  }

  const yRange = state.refLevelDbm - state.minLevelDbm;
  const toY = (dbm) => h - ((dbm - state.minLevelDbm) / yRange) * h;
  const toX = (idx) => (idx / Math.max(1, f.length - 1)) * w;

  // Shading baseline di bawah noise floor agar dasar trace terbaca
  const nfY = toY(f.noiseFloor);
  if (Number.isFinite(nfY) && nfY < h) {
    specCtx.fillStyle = "rgba(34, 197, 94, 0.06)";
    specCtx.fillRect(0, nfY, w, h - nfY);
  }

  // Garis Basis Noise Floor
  if (Number.isFinite(nfY)) {
    specCtx.strokeStyle = "rgba(34, 197, 94, 0.35)";
    specCtx.setLineDash([2, 4]);
    specCtx.beginPath();
    specCtx.moveTo(0, nfY);
    specCtx.lineTo(w, nfY);
    specCtx.stroke();
    specCtx.setLineDash([]);
  }

  // Garis Threshold
  const thrY = toY(f.threshold);
  specCtx.strokeStyle = "rgba(245, 158, 11, 0.7)";
  specCtx.setLineDash([4, 4]);
  specCtx.beginPath();
  specCtx.moveTo(0, thrY);
  specCtx.lineTo(w, thrY);
  specCtx.stroke();
  specCtx.setLineDash([]);

  // Trace Max Hold (selubung puncak tertinggi)
  if (state.traceMaxHold && state.peakLevels && state.peakLevels.length === f.length) {
    specCtx.strokeStyle = "rgba(239, 68, 68, 0.75)";
    specCtx.lineWidth = 1.4;
    specCtx.beginPath();
    for (let i = 0; i < f.length; i++) {
      const x = toX(i);
      const y = toY(state.peakLevels[i]);
      if (i === 0) specCtx.moveTo(x, y);
      else specCtx.lineTo(x, y);
    }
    specCtx.stroke();
  }

  // Trace Average (EMA — merumputkan noise grass, menonjolkan emisi stabil)
  if (state.traceAverage && state.avgLevels && state.avgLevels.length === f.length) {
    specCtx.strokeStyle = "rgba(250, 204, 21, 0.95)";
    specCtx.lineWidth = 1.8;
    specCtx.beginPath();
    for (let i = 0; i < f.length; i++) {
      const x = toX(i);
      const y = toY(state.avgLevels[i]);
      if (i === 0) specCtx.moveTo(x, y);
      else specCtx.lineTo(x, y);
    }
    specCtx.stroke();
  }

  // Trace Spektrum Utama (Realtime)
  // Saat sapuan dijeda (dengar/dwell), frame ini adalah SNAPSHOT terakhir —
  // diredupkan supaya operator tidak salah mengira masih data live.
  if (state.traceRealtime) {
    const isSnapshot = isListeningMode;
    const grad = specCtx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, `rgba(59, 130, 246, ${isSnapshot ? 0.14 : 0.35})`);
    grad.addColorStop(1, "rgba(59, 130, 246, 0.0)");

    specCtx.save();
    if (isSnapshot) specCtx.globalAlpha = 0.45;
    specCtx.fillStyle = grad;
    specCtx.strokeStyle = "#3b82f6";
    specCtx.lineWidth = 1.5;

    specCtx.beginPath();
    specCtx.moveTo(0, h);
    for (let i = 0; i < f.length; i++) {
      const x = toX(i);
      const y = toY(f.levels[i]);
      specCtx.lineTo(x, y);
    }
    specCtx.lineTo(w, h);
    specCtx.closePath();
    specCtx.fill();

    // Garis luar trace
    specCtx.beginPath();
    for (let i = 0; i < f.length; i++) {
      const x = toX(i);
      const y = toY(f.levels[i]);
      if (i === 0) specCtx.moveTo(x, y);
      else specCtx.lineTo(x, y);
    }
    specCtx.stroke();
    specCtx.restore();
  }

  // Sumbu-X: garis grid vertikal + label frekuensi MHz.
  // Digambar setelah trace agar label tetap tajam di atas dasar kanvas.
  drawFrequencyAxis(w, h, f);

  // Marker Puncak (Peak Search) & Marker Manual
  if (state.markerMode === "peak" && state.markerPeak) {
    drawPeakMarker(specCtx, state.markerPeak, f, toX, toY, w, h);
  }
  if (state.markerMode === "manual" && state.markerManual) {
    drawManualMarker(specCtx, state.markerManual, f, toX, toY, w, h);
  }

  // Gambar Garis Vertikal Kursor jika hover & marker manual tidak aktif
  if (markerX !== null && state.markerMode !== "manual") {
    specCtx.strokeStyle = "rgba(245, 158, 11, 0.55)";
    specCtx.lineWidth = 1;
    specCtx.beginPath();
    specCtx.moveTo(markerX, 0);
    specCtx.lineTo(markerX, h);
    specCtx.stroke();
  }

  // Overlay status: DWELL_RECORD / MANUAL_AUDIO menandai sapuan sedang dijeda.
  // Karena tidak ada frame baru saat jeda, animasi denyut digerakkan sendiri
  // oleh rAF sampai state kembali SWEEPING.
  if (state.rfState === "DWELL_RECORD") {
    drawSpectrumStatus(w, h, "Merekam Audio Dwell");
    drawWaterfallPauseOverlay();
    scheduleDraw();
  } else if (state.rfState === "MANUAL_AUDIO") {
    // Jangan lagi menutupi seluruh kanvas dengan kotak besar — taruh badge ringkas
    // di kanan atas dan gambar garis pembawa (carrier line) untuk tuning visual.
    drawListenBadge(specCtx, w, h);
    drawCarrierLine(specCtx, w, h);
    drawSnapshotWatermark(w, h);
    drawWaterfallPauseOverlay();
    scheduleDraw();
  }
}

// --------------------------------------------------------------------------- //
// Sumbu-X: garis grid vertikal + label frekuensi MHz
// --------------------------------------------------------------------------- //
function drawFrequencyAxis(w, h, f) {
  if (!f || !(f.length > 1)) return;
  const startMhz = f.startHz / 1e6;
  const stopMhz = (f.startHz + f.length * f.binSizeHz) / 1e6;
  const spanMhz = stopMhz - startMhz;
  if (!Number.isFinite(spanMhz) || spanMhz <= 0) return;

  // Langkah label & grid "bulat" (1/2/5 x 10^n) supaya tidak bertumpuk.
  const targetCols = Math.max(4, Math.min(10, Math.floor(w / 96)));
  const rawStep = spanMhz / targetCols;
  const mag = Math.max(1e-9, Math.pow(10, Math.floor(Math.log10(rawStep))));
  const norm = rawStep / mag;
  const niceNorm = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  const stepMhz = niceNorm * mag;
  const decimals = stepMhz >= 10 ? 1 : stepMhz >= 1 ? 2 : stepMhz >= 0.1 ? 3 : 4;

  specCtx.save();
  specCtx.font = "10px monospace";
  specCtx.textAlign = "center";
  specCtx.textBaseline = "bottom";

  const firstTick = Math.ceil(startMhz / stepMhz - 1e-9) * stepMhz;
  for (let fMhz = firstTick; fMhz <= stopMhz + 1e-6; fMhz += stepMhz) {
    const ratio = (fMhz - startMhz) / spanMhz;
    const x = Math.max(1, Math.min(w - 1, ratio * w));

    specCtx.strokeStyle = "rgba(42, 54, 71, 0.55)";
    specCtx.setLineDash([2, 5]);
    specCtx.lineWidth = 1;
    specCtx.beginPath();
    specCtx.moveTo(x, 0);
    specCtx.lineTo(x, h);
    specCtx.stroke();
    specCtx.setLineDash([]);

    const label = `${fMhz.toFixed(decimals)}`;
    const tw = specCtx.measureText(label).width;
    const lx = Math.max(tw / 2 + 3, Math.min(w - tw / 2 - 3, x));
    specCtx.fillStyle = "rgba(10, 14, 20, 0.72)";
    specCtx.fillRect(lx - tw / 2 - 3, h - 16, tw + 6, 13);
    specCtx.fillStyle = "rgba(183, 194, 207, 0.9)";
    specCtx.fillText(label, lx, h - 4);
  }

  specCtx.textAlign = "right";
  specCtx.fillStyle = "rgba(132, 147, 166, 0.8)";
  specCtx.fillText("MHz", w - 4, h - 4);
  specCtx.restore();
}

// Label kapsul untuk marker (agar terbaca di atas trace)
function markerTag(ctx, x, y, text, color, w, h) {
  ctx.save();
  ctx.font = "bold 11px monospace";
  const tw = ctx.measureText(text).width;
  const boxW = tw + 12;
  const boxH = 19;
  let bx = x + 12;
  if (bx + boxW > w - 4) bx = x - boxW - 12;
  bx = Math.max(4, bx);
  const by = Math.max(4, Math.min(h - boxH - 20, y - boxH / 2));

  ctx.fillStyle = "rgba(10, 14, 20, 0.88)";
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(bx, by, boxW, boxH, 4);
  else ctx.rect(bx, by, boxW, boxH);
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = color;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(text, bx + 6, by + boxH / 2 + 0.5);
  ctx.restore();
}

function drawPeakMarker(ctx, peak, f, toX, toY, w, h) {
  const idx = Math.max(0, Math.min(f.length - 1, peak.index));
  const x = toX(idx);
  const y = toY(peak.dbm);

  ctx.save();
  // Titik puncak
  ctx.fillStyle = "#fbbf24";
  ctx.beginPath();
  ctx.arc(x, y, 3.2, 0, Math.PI * 2);
  ctx.fill();

  // Tanda segitiga terbalik di atas puncak
  const triY = Math.max(4, y - 14);
  ctx.fillStyle = "#f59e0b";
  ctx.beginPath();
  ctx.moveTo(x - 6, triY);
  ctx.lineTo(x + 6, triY);
  ctx.lineTo(x, triY + 9);
  ctx.closePath();
  ctx.fill();

  // Garis vertikal putus-putus
  ctx.strokeStyle = "rgba(245, 158, 11, 0.55)";
  ctx.setLineDash([3, 4]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x, h);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();

  markerTag(ctx, x, y, `PK ${(peak.freqHz / 1e6).toFixed(4)} MHz · ${peak.dbm.toFixed(1)} dBm`, "#f59e0b", w, h);
}

function drawManualMarker(ctx, m, f, toX, toY, w, h) {
  const idx = Math.max(0, Math.min(f.length - 1,
      m.index ?? Math.round((m.freqHz - f.startHz) / f.binSizeHz)));
  const x = toX(idx);
  const dbm = Number.isFinite(m.dbm) ? m.dbm : f.levels[idx];
  const y = toY(dbm);

  ctx.save();
  // Berlian penanda
  ctx.fillStyle = "#60a5fa";
  ctx.beginPath();
  ctx.moveTo(x, y - 6);
  ctx.lineTo(x + 5, y);
  ctx.lineTo(x, y + 6);
  ctx.lineTo(x - 5, y);
  ctx.closePath();
  ctx.fill();

  // Garis vertical penuh
  ctx.strokeStyle = "rgba(96, 165, 250, 0.8)";
  ctx.lineWidth = 1.25;
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, h);
  ctx.stroke();
  ctx.restore();

  markerTag(ctx, x, y, `M1 ${(m.freqHz / 1e6).toFixed(4)} MHz · ${dbm.toFixed(1)} dBm`, "#60a5fa", w, h);
}

function setMarkerButtons() {
  const peakBtn = document.getElementById("markerPeakBtn");
  const manBtn = document.getElementById("markerManualBtn");
  if (peakBtn) {
    const on = state.markerMode === "peak";
    peakBtn.classList.toggle("is-on", on);
    peakBtn.setAttribute("aria-pressed", on);
  }
  if (manBtn) {
    const on = state.markerMode === "manual";
    manBtn.classList.toggle("is-on", on);
    manBtn.setAttribute("aria-pressed", on);
  }
  updateMarkerListenButton();
}

function updateMarkerListenButton() {
  const btn = document.getElementById("monListenMarkerBtn");
  const peak = state.markerMode === "peak" ? state.markerPeak : null;
  const manual = state.markerMode === "manual" ? state.markerManual : null;
  const active = peak || manual;
  if (!btn) return;
  if (active && Number.isFinite(active.freqHz)) {
    btn.disabled = false;
    btn.textContent = `🎯 Dengar ${(active.freqHz / 1e6).toFixed(4)} MHz`;
    btn.dataset.freqHz = String(active.freqHz);
  } else {
    btn.disabled = true;
    btn.textContent = "🎯 Frek. Marker";
    delete btn.dataset.freqHz;
  }
}

// --------------------------------------------------------------------------- //
// Badge Status & Garis Pembawa Tuning Audio Manual
// --------------------------------------------------------------------------- //
function drawListenBadge(ctx, w, h) {
  const f = state.spectrumFrame;
  const freq = state.listenDragActive ? state.listenDragFreqHz : state.listenFreqHz;
  const demod = getMonitorDemod();
  const freqMhz = (freq / 1e6).toFixed(4);

  ctx.save();
  const now = performance.now();
  const pulse = (Math.sin(now / 200) + 1) / 2;
  const text = `🎧 ${freqMhz} MHz (${demod}) · Geser garis untuk tuning`;

  ctx.font = "bold 11px monospace";
  const tw = ctx.measureText(text).width;
  const badgeW = tw + 28;
  const badgeH = 24;
  const badgeX = Math.max(8, w - badgeW - 10);
  const badgeY = 8;

  // Latar semi-transparan elegan
  ctx.fillStyle = "rgba(10, 14, 20, 0.85)";
  ctx.strokeStyle = `rgba(59, 130, 246, ${0.45 + 0.55 * pulse})`;
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 6);
  ctx.fill();
  ctx.stroke();

  // Titik denyut status "Mendengarkan"
  ctx.fillStyle = `rgba(59, 130, 246, ${0.6 + 0.4 * pulse})`;
  ctx.beginPath();
  ctx.arc(badgeX + 11, badgeY + badgeH / 2, 4, 0, Math.PI * 2);
  ctx.fill();

  // Teks info
  ctx.fillStyle = "#e5e7eb";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(text, badgeX + 20, badgeY + badgeH / 2);
  ctx.restore();
}

// Lebar passband per mode demodulasi — dipakai oleh overlay garis pembawa
// (drawCarrierLine) dan drag-to-tune, jadi harus berada di scope modul.
function _demodBandwidthHz(demod) {
  switch (String(demod || "").toUpperCase()) {
    case "FM": return 150_000;   // passband siaran FM ~150 kHz
    case "AM": return 8_000;
    case "USB":
    case "LSB": return 3_000;
    case "CW": return 1_000;
    default: return 15_000;
  }
}

function drawSnapshotWatermark(w, h) {
  specCtx.save();
  const text = "SNAPSHOT SAPUAN TERAKHIR · SAPUAN DIJEDA SAAT DENGAR";
  specCtx.font = "bold 10px monospace";
  const tw = specCtx.measureText(text).width;
  const bx = 10;
  const by = 8;
  const bw = tw + 18;
  const bh = 20;

  specCtx.fillStyle = "rgba(10, 14, 20, 0.78)";
  specCtx.strokeStyle = "rgba(148, 163, 184, 0.45)";
  specCtx.lineWidth = 1;
  specCtx.beginPath();
  specCtx.roundRect(bx, by, bw, bh, 4);
  specCtx.fill();
  specCtx.stroke();

  specCtx.fillStyle = "rgba(148, 163, 184, 0.95)";
  specCtx.textAlign = "left";
  specCtx.textBaseline = "middle";
  specCtx.fillText(`⏸ ${text}`, bx + 8, by + bh / 2);
  specCtx.restore();
}

function drawCarrierLine(ctx, w, h) {
  const f = state.spectrumFrame;
  if (!f) return;
  const freq = state.listenDragActive ? state.listenDragFreqHz : state.listenFreqHz;
  if (!Number.isFinite(freq) || freq <= 0) return;
  const stopHz = f.startHz + f.length * f.binSizeHz;
  if (stopHz <= f.startHz) return;

  const ratio = (freq - f.startHz) / (stopHz - f.startHz);
  const x = ratio * w;
  // Di luar tampilan: abaikan
  if (x < -20 || x > w + 20) return;

  const demod = getMonitorDemod();
  const bwHz = _demodBandwidthHz(demod);
  const bwPx = Math.max(3, (bwHz / (stopHz - f.startHz)) * w);

  ctx.save();

  // 1. Pita Passband Demodulasi (bayangan cyan transparan)
  ctx.fillStyle = "rgba(56, 189, 248, 0.14)";
  ctx.fillRect(x - bwPx / 2, 0, bwPx, h);

  // 2. Garis Batas Passband Kiri-Kanan (garis putus-putus halus)
  ctx.strokeStyle = "rgba(56, 189, 248, 0.4)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.moveTo(x - bwPx / 2, 0);
  ctx.lineTo(x - bwPx / 2, h);
  ctx.moveTo(x + bwPx / 2, 0);
  ctx.lineTo(x + bwPx / 2, h);
  ctx.stroke();
  ctx.setLineDash([]);

  // 3. Garis Pembawa Utama (Carrier Line)
  const isDragging = state.listenDragActive;
  ctx.strokeStyle = isDragging ? "#38bdf8" : "rgba(56, 189, 248, 0.9)";
  ctx.lineWidth = isDragging ? 2.2 : 1.6;
  ctx.beginPath();
  ctx.moveTo(x, 0);
  ctx.lineTo(x, h);
  ctx.stroke();

  // 4. Handle Penarik (Lingkaran di bagian atas garis)
  const handleY = 32;
  ctx.fillStyle = isDragging ? "#0284c7" : "#0f172a";
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(x, handleY, 8, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  // Titik tengah putih kecil di dalam handle
  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.arc(x, handleY, 2.5, 0, Math.PI * 2);
  ctx.fill();

  // 5. Label Frekuensi di Bawah Handle
  const mhzStr = `${(freq / 1e6).toFixed(4)} MHz`;
  ctx.font = "bold 10px monospace";
  const tw = ctx.measureText(mhzStr).width;
  const lx = Math.max(tw / 2 + 4, Math.min(w - tw / 2 - 4, x));
  const ly = handleY + 18;

  ctx.fillStyle = "rgba(10, 14, 20, 0.82)";
  ctx.strokeStyle = "rgba(56, 189, 248, 0.5)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(lx - tw / 2 - 4, ly - 9, tw + 8, 14, 3);
  ctx.fill();
  ctx.stroke();

  ctx.fillStyle = "#38bdf8";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(mhzStr, lx, ly - 2);

  ctx.restore();
}

function drawSpectrumStatus(w, h, title) {
  const sub =
    title === "Merekam Audio Dwell"
      ? "Sapuan spektrum dijeda sementara perangkat menerima audio"
      : title === "Demodulasi Audio Manual"
      ? "Audio manual diakhiri dengan 'Hentikan' untuk melanjutkan sapuan"
      : "Belum ada frame spektrum diterima dari server";
  const paused =
    title === "Demodulasi Audio Manual" || title === "Merekam Audio Dwell";
  if (paused) {
    const now = performance.now();
    const pulse = (Math.sin(now / 180) + 1) / 2;
    const dotRgB = title === "Merekam Audio Dwell" ? "239, 68, 68" : "59, 130, 246";
    specCtx.save();
    const boxW = Math.min(400, w - 32);
    const boxH = 58;
    const boxX = (w - boxW) / 2;
    const boxY = (h - boxH) / 2;

    specCtx.fillStyle = "rgba(10, 14, 20, 0.8)";
    specCtx.strokeStyle = `rgba(${dotRgB}, ${0.6 + 0.4 * pulse})`;
    specCtx.lineWidth = 1.5;
    specCtx.beginPath();
    specCtx.roundRect(boxX, boxY, boxW, boxH, 8);
    specCtx.fill();
    specCtx.stroke();

    specCtx.fillStyle = `rgba(${dotRgB}, ${0.7 + 0.3 * pulse})`;
    specCtx.beginPath();
    specCtx.arc(boxX + 18, boxY + 29, 7, 0, Math.PI * 2);
    specCtx.fill();

    specCtx.fillStyle = "#f3f4f6";
    specCtx.font = "bold 13px sans-serif";
    specCtx.textAlign = "left";
    specCtx.fillText(title, boxX + 36, boxY + 24);
    specCtx.font = "11px sans-serif";
    specCtx.fillStyle = "rgba(156, 163, 175, 0.9)";
    specCtx.fillText(sub, boxX + 36, boxY + 44);
    specCtx.restore();
  }
}

// --------------------------------------------------------------------------- //
// Waterfall Display (Rolling Spectrogram)
// --------------------------------------------------------------------------- //
let wfCanvas, wfCtx, wfOffscreen, wfOffCtx;

function initWaterfallCanvas() {
  wfCanvas = document.getElementById("waterfallCanvas");
  wfCtx = wfCanvas.getContext("2d", { alpha: false });

  wfOffscreen = document.createElement("canvas");
  wfOffCtx = wfOffscreen.getContext("2d", { alpha: false });

  function resize() {
    const rect = wfCanvas.getBoundingClientRect();
    wfCanvas.width = rect.width;
    wfCanvas.height = rect.height;
    wfOffscreen.width = rect.width;
    wfOffscreen.height = rect.height;
    wfOffCtx.fillStyle = "#0a0e14";
    wfOffCtx.fillRect(0, 0, wfOffscreen.width, wfOffscreen.height);
  }
  window.addEventListener("resize", resize);
  setTimeout(resize, 50);
}

function drawWaterfallRow(levels, noiseFloor) {
  if (!wfCtx || !wfOffscreen || wfCanvas.width === 0) return;
  const w = wfCanvas.width;
  const h = wfCanvas.height;

  // Gulir kanvas offscreen 1 piksel ke bawah
  wfOffCtx.drawImage(wfOffscreen, 0, 0, w, h - 1, 0, 1, w, h - 1);

  // Buat 1 baris ImageData di baris paling atas (y = 0)
  const imgData = wfOffCtx.createImageData(w, 1);
  const data = imgData.data;
  const lut = colorLUT;
  const n = levels.length;
  const gain = state.waterfallGain;

  // Jendela dinamis waterfall: 45 dB di atas noise floor, bukan seluruh
  // rentang ref-noise (bisa >85 dB) — dengan seluruh rentang, seluruh
  // rumput derau jatuh di indeks ~0 dan panel tampak hitam total.
  const wfRangeDb = 45.0;

  for (let x = 0; x < w; x++) {
    const idx = Math.floor((x / w) * n);
    const dbm = levels[idx];

    // Normalisasi: noise floor -> 0, noise floor + 45 dB -> 1
    let norm = (dbm - noiseFloor + gain) / wfRangeDb;
    norm = Math.max(0.0, Math.min(1.0, norm));
    norm = Math.pow(norm, 0.7); // gamma: angkat nada tengah agar emisi lemah terlihat
    const lutIdx = Math.floor(norm * 255) * 4;

    const px = x * 4;
    data[px + 0] = lut[lutIdx + 0];
    data[px + 1] = lut[lutIdx + 1];
    data[px + 2] = lut[lutIdx + 2];
    data[px + 3] = 255;
  }

  wfOffCtx.putImageData(imgData, 0, 0);
  wfCtx.drawImage(wfOffscreen, 0, 0);
}

/**
 * Overlay "DIJEDA" pada panel waterfall.
 *
 * SA124B hanya punya SATU tuner RF: saat LO dipakai mendemodulasi audio, LO tidak
 * bisa menyapu — jadi tidak ada frame spektrum baru dan waterfall secara fisik
 * berhenti bergulir. Tanpa penanda ini panel tampak "mati/macet" dan operator
 * menyangka aplikasi hang. Overlay membuat penyebabnya eksplisit.
 *
 * Dipanggil dari drawSpectrum() setiap kali sapuan dijeda; saat frame baru masuk,
 * drawWaterfallRow() langsung menimpanya sehingga overlay hilang sendiri.
 */
function drawWaterfallPauseOverlay() {
  if (!wfCtx || wfCanvas.width === 0) return;
  const w = wfCanvas.width;
  const h = wfCanvas.height;

  wfCtx.save();

  // Gelap tipis supaya spektrogram terakhir masih terlihat di balik overlay
  wfCtx.fillStyle = "rgba(10, 14, 20, 0.55)";
  wfCtx.fillRect(0, 0, w, h);

  // Garis putus-putus horizontal penanda status dijeda
  wfCtx.strokeStyle = "rgba(56, 189, 248, 0.55)";
  wfCtx.lineWidth = 1;
  wfCtx.setLineDash([6, 6]);
  wfCtx.beginPath();
  wfCtx.moveTo(0, h / 2);
  wfCtx.lineTo(w, h / 2);
  wfCtx.stroke();
  wfCtx.setLineDash([]);

  // Kapsul label di tengah panel
  const label = "WATERFALL DIJEDA";
  const sub = "SA124B 1 tuner — dengar audio & sapuan spektrum tidak bisa bersamaan";
  wfCtx.font = "bold 12px monospace";
  const lw = wfCtx.measureText(label).width;
  wfCtx.font = "10px monospace";
  const sw = wfCtx.measureText(sub).width;
  const boxW = Math.max(lw, sw) + 24;
  const boxH = 44;
  const bx = (w - boxW) / 2;
  const by = (h - boxH) / 2;

  wfCtx.fillStyle = "rgba(10, 14, 20, 0.88)";
  wfCtx.strokeStyle = "rgba(56, 189, 248, 0.6)";
  wfCtx.lineWidth = 1;
  wfCtx.beginPath();
  wfCtx.roundRect(bx, by, boxW, boxH, 6);
  wfCtx.fill();
  wfCtx.stroke();

  wfCtx.textAlign = "center";
  wfCtx.textBaseline = "middle";
  wfCtx.fillStyle = "#38bdf8";
  wfCtx.font = "bold 12px monospace";
  wfCtx.fillText(label, w / 2, by + 15);
  wfCtx.fillStyle = "rgba(183, 194, 207, 0.9)";
  wfCtx.font = "10px monospace";
  wfCtx.fillText(sub, w / 2, by + 30);

  wfCtx.restore();
}

// --------------------------------------------------------------------------- //
// Web Audio API Streaming Player (PCM Float32)
// --------------------------------------------------------------------------- //
function initAudio() {
  if (!state.audioCtx) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    state.audioCtx = new AudioCtx({ sampleRate: 32000 });
    state.audioGainNode = state.audioCtx.createGain();
    state.audioGainNode.connect(state.audioCtx.destination);
  }
  const volEl = document.getElementById("volumeRange") || document.getElementById("monVolumeRange");
  const vol = volEl ? parseFloat(volEl.value) / 100.0 : 0.7;
  if (state.audioGainNode && state.audioCtx) {
    try {
      state.audioGainNode.gain.setValueAtTime(vol, state.audioCtx.currentTime);
    } catch (_) {}
  }
  if (state.audioCtx.state === "suspended") {
    state.audioCtx.resume();
  }
}

function queueAudioChunk(arrayBuffer) {
  if (!state.isListening) return; // Buang paket audio segera bila sesi dengar sudah dimatikan
  if (!state.audioCtx || state.audioCtx.state !== "running") return;
  const floatData = new Float32Array(arrayBuffer);
  const audioBuf = state.audioCtx.createBuffer(1, floatData.length, state.audioCtx.sampleRate);
  audioBuf.copyToChannel(floatData, 0);

  const src = state.audioCtx.createBufferSource();
  src.buffer = audioBuf;
  src.connect(state.audioGainNode);

  const now = state.audioCtx.currentTime;
  if (state.audioNextPlayTime < now) {
    state.audioNextPlayTime = now + 0.05; // 50ms buffer head
  }
  src.start(state.audioNextPlayTime);
  state.audioNextPlayTime += audioBuf.duration;

  // Hitung peak untuk VU Meter (drawer & bilah monitor)
  let peak = 0;
  for (let i = 0; i < floatData.length; i += 32) {
    const abs = Math.abs(floatData[i]);
    if (abs > peak) peak = abs;
  }
  const pct = `${Math.min(100, Math.round(peak * 120))}%`;
  const vuBar = document.getElementById("vuBar");
  if (vuBar) vuBar.style.width = pct;
  const monVu = document.getElementById("monVuBar");
  if (monVu) monVu.style.width = pct;
}

// --------------------------------------------------------------------------- //
// Aksi audio terpadu (dipakai drawer & bilah monitor)
// --------------------------------------------------------------------------- //
function getMonitorDemod() {
  const sel = document.getElementById("monListenDemod");
  return sel ? sel.value : "FM";
}

/**
 * Tulis frekuensi ke kedua kotak input (drawer samping & bilah monitor).
 * Scope modul: dipakai oleh startListening() dan window.tuneAndListen() yang
 * keduanya berada di luar closure initSpectrumCanvas().
 */
function _syncListenInputs(freqHz) {
  const mhz = (freqHz / 1e6).toFixed(4);
  const mon = document.getElementById("monListenFreq");
  if (mon) mon.value = mhz;
  const side = document.getElementById("listenFreq");
  if (side) side.value = mhz;
}

function syncAudioButtons() {
  const monBtn = document.getElementById("monListenToggleBtn");
  if (monBtn) {
    monBtn.textContent = state.isListening ? "⏹ Stop Dengar" : "🎧 Dengar";
    monBtn.classList.toggle("btn-danger", state.isListening);
    monBtn.classList.toggle("btn-primary", !state.isListening);
  }
  const sideBtn = document.getElementById("listenBtn");
  if (sideBtn) sideBtn.disabled = state.isListening;

  const recBtn = document.getElementById("monRecordToggleBtn");
  if (recBtn) {
    recBtn.textContent = state.isManualRecording ? "⏹ Stop Rekam" : "⏺ Rekam";
    recBtn.classList.toggle("is-recording", state.isManualRecording);
  }
}

async function startListening(freqHz, demod) {
  initAudio();
  const dem = demod || "FM";
  try {
    await api("/api/rf/listen", {
      method: "POST",
      body: { freq_hz: freqHz, demod: dem },
    });
    state.isListening = true;
    state.listenFreqHz = Number(freqHz) || 0;
    state.listenDemod = dem;

    // Sinkronkan kedua antarmuka
    _syncListenInputs(state.listenFreqHz);
    const sideDemod = document.getElementById("listenDemod");
    if (sideDemod) sideDemod.value = dem;

    syncAudioButtons();
    // Saat operator menaris garis pembawa, jangan munculkan toast setiap 90 ms.
    if (!state.listenDragActive) {
      toast(`Mendengarkan ${(state.listenFreqHz / 1e6).toFixed(4)} MHz (${dem})`, "ok");
    }
    drawSpectrum();
  } catch (err) {
    toast(err.message, "err");
    drawSpectrum();
  }
}

async function stopListening() {
  state.isListening = false;
  // Batalkan geseran frekuensi yang mungkin masih menggantung
  if (state.listenDragTimer) {
    clearTimeout(state.listenDragTimer);
    state.listenDragTimer = null;
  }
  state.listenDragActive = false;
  state.listenDragMoved = false;
  specCanvas.style.cursor = "";

  // Mute audio gain seketika dan tangguhkan AudioContext agar buffer terjadwal tidak terus berbunyi!
  if (state.audioGainNode && state.audioCtx) {
    try {
      state.audioGainNode.gain.setValueAtTime(0, state.audioCtx.currentTime);
    } catch (_) {}
  }
  if (state.audioCtx && state.audioCtx.state === "running") {
    try {
      await state.audioCtx.suspend();
    } catch (_) {}
  }
  state.audioNextPlayTime = 0;

  const vu = document.getElementById("vuBar");
  if (vu) vu.style.width = "0%";
  const monVu = document.getElementById("monVuBar");
  if (monVu) monVu.style.width = "0%";
  syncAudioButtons();

  // Sapuan kembali aktif: bersihkan overlay "dijeda" dan gambar ulang segera.
  state.rfState = "SWEEPING";
  scheduleDraw();

  try {
    await api("/api/rf/listen/stop", { method: "POST" });
    toast("Dengar audio dihentikan", "info");
  } catch (err) {
    toast(err.message, "err");
  }
}

function _fmtElapsed(sec) {
  const s = Math.max(0, Math.floor(sec));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function _tickRecordTimer() {
  const el = document.getElementById("monRecordTimer");
  if (!el) return;
  if (!state.isManualRecording) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = _fmtElapsed((Date.now() - state.manualRecordStartedAt) / 1000);
}

async function startManualRecord(freqHz, demod) {
  try {
    const res = await api("/api/rf/record/start", {
      method: "POST",
      body: { freq_hz: freqHz, demod: demod || "FM" },
    });
    if (res.status === "error") {
      toast(res.message || "Gagal memulai rekaman.", "err");
      return;
    }
    state.isManualRecording = true;
    state.manualRecordStartedAt = Date.now();
    state.manualRecordFilename = res.filename || null;

    // Rekaman manual memakai sesi dengar perangkat yang sama.
    state.isListening = true;

    syncAudioButtons();
    _tickRecordTimer();
    if (state.manualRecordTimerId) clearInterval(state.manualRecordTimerId);
    state.manualRecordTimerId = setInterval(_tickRecordTimer, 500);

    toast(`Merekam ${(freqHz / 1e6).toFixed(4)} MHz → ${res.filename || "WAV"}`, "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function stopManualRecord() {
  try {
    const res = await api("/api/rf/record/stop", { method: "POST" });
    state.isManualRecording = false;
    if (state.manualRecordTimerId) {
      clearInterval(state.manualRecordTimerId);
      state.manualRecordTimerId = null;
    }
    _tickRecordTimer();
    syncAudioButtons();

    if (res.status === "ok") {
      toast(`Rekaman tersimpan: ${res.filename} (${res.duration_sec?.toFixed?.(1) ?? res.duration_sec} s)`, "ok");
      refreshRecordings();
    } else if (res.status === "discarded") {
      toast("Tidak ada audio terekam — berkas dibuang.", "warn");
    } else {
      toast("Tidak ada rekaman yang sedang berjalan.", "info");
    }
  } catch (err) {
    toast(err.message, "err");
  }
}

async function toggleOccupancyTracking() {
  const next = !state.occupancyTracking;
  try {
    await api("/api/rf/occupancy/tracking", { method: "POST", body: { enabled: next } });
    state.occupancyTracking = next;
    syncOccupancyButtons();
    toast(`Okupansi ${next ? "dilanjutkan" : "dijeda"}`, next ? "ok" : "warn");
  } catch (err) {
    toast(err.message, "err");
  }
}

function syncOccupancyButtons() {
  const on = state.occupancyTracking;
  const label = on ? "Jeda Okupansi" : "Mulai Okupansi";
  for (const id of ["occToggleBtn", "occToggleViewBtn"]) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    btn.dataset.enabled = String(on);
    const textEl = btn.querySelector("span:not(.occ-dot)");
    if (textEl) textEl.textContent = label;
    const dot = btn.querySelector(".occ-dot");
    if (dot) {
      dot.classList.toggle("is-on", on);
      dot.classList.toggle("is-paused", !on);
    }
  }
  const badge = document.getElementById("occTrackingBadge");
  if (badge) {
    badge.textContent = on ? "Mengukur" : "Dijeda";
    badge.className = `occ-badge ${on ? "is-on" : "is-paused"}`;
  }
}

// --------------------------------------------------------------------------- //
// Handlers Kontrol Panel RF & Aksi Pengguna
// --------------------------------------------------------------------------- //
function initControls() {
  // Pilihan Skala Rentang Y Spektrum (Auto Range atau Span Tetap)
  const scaleSel = document.getElementById("scaleSelect");
  if (scaleSel) {
    scaleSel.addEventListener("change", (e) => {
      state.scale = e.target.value;
      applyYRange();
      drawSpectrum();
    });
  }

  // Mode Trace: Realtime / Max Hold / Average
  const realtimeBtn = document.getElementById("traceRealtimeBtn");
  const maxHoldBtn = document.getElementById("traceMaxHoldBtn");
  const avgBtn = document.getElementById("traceAvgBtn");

  function syncTraceButtons() {
    if (realtimeBtn) {
      realtimeBtn.classList.toggle("is-on", state.traceRealtime);
      realtimeBtn.setAttribute("aria-pressed", String(state.traceRealtime));
    }
    if (maxHoldBtn) {
      maxHoldBtn.classList.toggle("is-on", state.traceMaxHold);
      maxHoldBtn.setAttribute("aria-pressed", String(state.traceMaxHold));
    }
    if (avgBtn) {
      avgBtn.classList.toggle("is-on", state.traceAverage);
      avgBtn.setAttribute("aria-pressed", String(state.traceAverage));
    }
  }

  if (realtimeBtn) {
    realtimeBtn.addEventListener("click", () => {
      state.traceRealtime = !state.traceRealtime;
      syncTraceButtons();
      drawSpectrum();
    });
  }
  if (maxHoldBtn) {
    maxHoldBtn.addEventListener("click", () => {
      state.traceMaxHold = !state.traceMaxHold;
      if (!state.traceMaxHold) state.peakLevels = null;
      syncTraceButtons();
      drawSpectrum();
    });
  }
  if (avgBtn) {
    avgBtn.addEventListener("click", () => {
      state.traceAverage = !state.traceAverage;
      if (!state.traceAverage) state.avgLevels = null;
      syncTraceButtons();
      drawSpectrum();
    });
  }

  document.getElementById("resetTraceBtn")?.addEventListener("click", () => {
    state.peakLevels = null;
    state.avgLevels = null;
    drawSpectrum();
    toast("Memori Max Hold & Average dibersihkan", "info");
  });

  // Marker spektrum: Puncak / Manual / Hapus
  document.getElementById("markerPeakBtn")?.addEventListener("click", () => {
    state.markerMode = state.markerMode === "peak" ? "none" : "peak";
    setMarkerButtons();
    drawSpectrum();
  });

  document.getElementById("markerManualBtn")?.addEventListener("click", () => {
    state.markerMode = state.markerMode === "manual" ? "none" : "manual";
    if (state.markerMode === "manual" && !state.markerManual && state.spectrumFrame) {
      // Taruh di tengah span agar operator punya pegangan awal.
      const f = state.spectrumFrame;
      const idx = Math.floor(f.length / 2);
      state.markerManual = { freqHz: f.startHz + idx * f.binSizeHz, dbm: null, index: idx };
    }
    setMarkerButtons();
    drawSpectrum();
    if (state.markerMode === "manual") {
      toast("Klik / geser pada kanvas untuk menempatkan marker", "info");
    }
  });

  document.getElementById("markerClearBtn")?.addEventListener("click", () => {
    state.markerMode = "none";
    state.markerManual = null;
    setMarkerButtons();
    drawSpectrum();
  });

  // Dengar Frekuensi Marker (1-klik)
  document.getElementById("monListenMarkerBtn")?.addEventListener("click", async (e) => {
    const freqHz = parseFloat(e.currentTarget.dataset.freqHz);
    if (!Number.isFinite(freqHz)) return;
    // Pindahkan frekuensi marker ke kotak input agar operator melihat target
    // yang sedang didengarkan (kalau tidak, kotak tetap menampilkan frekuensi lama).
    const input = document.getElementById("monListenFreq");
    if (input) input.value = (freqHz / 1e6).toFixed(4);
    await startListening(freqHz, getMonitorDemod());
  });

  // Audio Bar Monitor: input frekuensi & volume
  const monVol = document.getElementById("monVolumeRange");
  if (monVol) {
    monVol.addEventListener("input", (e) => {
      const v = parseFloat(e.target.value);
      const side = document.getElementById("volumeRange");
      if (side) side.value = e.target.value;
      if (state.audioGainNode) state.audioGainNode.gain.value = v / 100.0;
    });
  }

  document.getElementById("monListenToggleBtn")?.addEventListener("click", async () => {
    if (state.isListening) {
      await stopListening();
    } else {
      const freq = parseFloat(document.getElementById("monListenFreq").value) * 1e6;
      if (!Number.isFinite(freq) || freq <= 0) {
        toast("Isi frekuensi dengarkan (MHz) atau pakai tombol Frek. Marker.", "err");
        return;
      }
      await startListening(freq, getMonitorDemod());
    }
  });

  document.getElementById("monRecordToggleBtn")?.addEventListener("click", async () => {
    if (state.isManualRecording) {
      await stopManualRecord();
    } else {
      const freq = parseFloat(document.getElementById("monListenFreq").value) * 1e6;
      if (!Number.isFinite(freq) || freq <= 0) {
        toast("Isi frekuensi (MHz) sebelum merekam.", "err");
        return;
      }
      await startManualRecord(freq, getMonitorDemod());
    }
  });

  // Jeda / lanjutkan pengukuran okupansi (Monitor & view Okupansi)
  document.getElementById("occToggleBtn")?.addEventListener("click", () => toggleOccupancyTracking());
  document.getElementById("occToggleViewBtn")?.addEventListener("click", () => toggleOccupancyTracking());

  // Waterfall Palette & Gain
  document.getElementById("paletteSelect").addEventListener("change", (e) => {
    state.paletteName = e.target.value;
    colorLUT = generatePalette(state.paletteName);
  });

  document.getElementById("waterfallGain").addEventListener("input", (e) => {
    state.waterfallGain = parseFloat(e.target.value);
  });

  // Preset Select
  document.getElementById("presetSelect").addEventListener("change", async (e) => {
    const key = e.target.value;
    try {
      await api("/api/rf/preset", { method: "POST", body: { preset_key: key } });
      toast(`Preset diubah ke ${key}`, "ok");
      refreshStatus();
      syncConfigToForm();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Terapkan Sweep Manual
  document.getElementById("applySweepBtn").addEventListener("click", async () => {
    const center = parseFloat(document.getElementById("centerInput").value) * 1e6;
    const span = parseFloat(document.getElementById("spanInput").value) * 1e6;
    const rbw = parseFloat(document.getElementById("rbwInput").value) * 1e3;
    const ref = parseFloat(document.getElementById("refInput").value);
    const atten = parseInt(document.getElementById("attenInput").value, 10);

    if (![center, span, rbw, ref, atten].every(Number.isFinite)) {
      toast("Lengkapi semua kolom parameter sapuan.", "err");
      return;
    }

    try {
      await api("/api/rf/sweep", {
        method: "POST",
        body: { center_hz: center, span_hz: span, rbw_hz: rbw, ref_level_dbm: ref, atten_db: atten },
      });
      toast("Parameter sweep diperbarui", "ok");
      state.refLevelDbm = ref;
      state.rbwHz = rbw;
      state.smoothedNoiseFloor = null;
      applyYRange();
      refreshStatus();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Terapkan opsi okupansi / rekam otomatis
  document.getElementById("applyOccBtn").addEventListener("click", async () => {
    const mode = document.getElementById("thresholdMode").value;
    const offset = parseFloat(document.getElementById("offsetInput").value);
    const dwell = parseFloat(document.getElementById("dwellInput").value);
    const body = { threshold_mode: mode };
    if (Number.isFinite(offset)) body.adaptive_offset_db = offset;
    if (Number.isFinite(dwell)) body.dwell_sec = dwell;
    try {
      await api("/api/rf/occupancy-config", { method: "POST", body });
      toast("Ambang batas & dwell diperbarui", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Dengar Audio Langsung (drawer samping)
  document.getElementById("listenBtn").addEventListener("click", async () => {
    const freq = parseFloat(document.getElementById("listenFreq").value) * 1e6;
    const demod = document.getElementById("listenDemod").value;
    if (!Number.isFinite(freq) || freq <= 0) {
      toast("Isi frekuensi dengarkan (MHz).", "err");
      return;
    }
    await startListening(freq, demod);
  });

  document.getElementById("listenStopBtn").addEventListener("click", () => stopListening());

  document.getElementById("volumeRange").addEventListener("input", (e) => {
    const mon = document.getElementById("monVolumeRange");
    if (mon) mon.value = e.target.value;
    if (state.audioGainNode) {
      state.audioGainNode.gain.value = parseFloat(e.target.value) / 100.0;
    }
  });

  // Auto Record Toggle
  document.getElementById("autoRecordToggle").addEventListener("change", async (e) => {
    try {
      await api("/api/rf/autorecord", { method: "POST", body: { enabled: e.target.checked } });
      toast(`Auto-record ${e.target.checked ? "diaktifkan" : "dinonaktifkan"}`, "info");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Engine Start / Stop
  document.getElementById("engineStartBtn").addEventListener("click", async () => {
    try {
      await api("/api/rf/start", { method: "POST" });
      toast("Capture engine dimulai", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  document.getElementById("engineStopBtn").addEventListener("click", async () => {
    try {
      if (state.isListening) {
        await stopListening();
      }
      await api("/api/rf/stop", { method: "POST" });
      toast("Capture engine dihentikan", "warn");
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Ekspor & Reset Okupansi
  document.getElementById("exportXlsxBtn").addEventListener("click", () => {
    window.location.href = `/api/reports/excel?token=${encodeURIComponent(state.token)}`;
  });
  document.getElementById("exportCsvBtn").addEventListener("click", () => {
    window.location.href = `/api/reports/csv?token=${encodeURIComponent(state.token)}`;
  });
  document.getElementById("resetOccBtn").addEventListener("click", async () => {
    if (!confirm("Hapus seluruh data riwayat okupansi kanal?")) return;
    try {
      await api("/api/occupancy/reset", { method: "POST" });
      toast("Statistik okupansi berhasil direset", "ok");
      state.occSelectedKeys.clear();
      refreshOccupancy();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Hapus selektif okupansi
  document.getElementById("deleteSelectedOccBtn")?.addEventListener("click", async () => {
    const keys = Array.from(state.occSelectedKeys);
    if (!keys.length) return;
    if (!confirm(`Hapus ${keys.length} kanal terpilih dari riwayat okupansi?`)) return;
    try {
      const res = await api("/api/occupancy/delete", { method: "POST", body: { keys } });
      toast(`${res.deleted} kanal berhasil dihapus`, "ok");
      state.occSelectedKeys.clear();
      refreshOccupancy();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Pilih semua okupansi
  document.getElementById("occSelectAll")?.addEventListener("change", (e) => {
    const checked = e.target.checked;
    document.querySelectorAll("#occBody .occ-chk").forEach((chk) => {
      chk.checked = checked;
      const key = chk.dataset.key;
      if (checked && key) state.occSelectedKeys.add(key);
      else if (!checked && key) state.occSelectedKeys.delete(key);
    });
    _refreshOccSelectionUI();
  });

  // Delegasi checkbox baris okupansi
  document.getElementById("occTable")?.addEventListener("change", (e) => {
    if (e.target.classList.contains("occ-chk")) {
      const key = e.target.dataset.key;
      if (!key) return;
      if (e.target.checked) state.occSelectedKeys.add(key);
      else state.occSelectedKeys.delete(key);
      _refreshOccSelectionUI();
    }
  });

  // Rekaman: Pilih semua
  document.getElementById("recSelectAll")?.addEventListener("change", (e) => {
    const checked = e.target.checked;
    document.querySelectorAll("#recList .rec-chk").forEach((chk) => {
      chk.checked = checked;
      const fn = chk.dataset.filename;
      if (checked && fn) state.recSelectedFilenames.add(fn);
      else if (!checked && fn) state.recSelectedFilenames.delete(fn);
    });
    _refreshRecSelectionUI();
  });

  // Delegasi checkbox rekaman per item
  document.getElementById("recList")?.addEventListener("change", (e) => {
    if (e.target.classList.contains("rec-chk")) {
      const fn = e.target.dataset.filename;
      if (!fn) return;
      if (e.target.checked) state.recSelectedFilenames.add(fn);
      else state.recSelectedFilenames.delete(fn);
      _refreshRecSelectionUI();
    }
  });

  // Rekaman: Hapus terpilih
  document.getElementById("recDeleteSelectedBtn")?.addEventListener("click", async () => {
    const filenames = Array.from(state.recSelectedFilenames);
    if (!filenames.length) return;
    if (!confirm(`Hapus ${filenames.length} file rekaman terpilih?`)) return;
    try {
      const res = await api("/api/recordings/bulk-delete", { method: "POST", body: { filenames } });
      toast(`${res.deleted} file rekaman berhasil dihapus`, "ok");
      state.recSelectedFilenames.clear();
      refreshRecordings();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Rekaman: Hapus semua
  document.getElementById("recDeleteAllBtn")?.addEventListener("click", async () => {
    if (!confirm("Hapus SELURUH file rekaman audio? Tindakan ini tidak dapat dibatalkan.")) return;
    try {
      const res = await api("/api/recordings/delete-all", { method: "POST" });
      toast(`${res.deleted} file rekaman berhasil dihapus`, "ok");
      state.recSelectedFilenames.clear();
      refreshRecordings();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Bilah Navigasi Span
  document.getElementById("panLeftBtn")?.addEventListener("click", async () => {
    if (!state.spectrumFrame) return;
    const f = state.spectrumFrame;
    const span = f.length * f.binSizeHz;
    const newCenter = Math.max(100000, (f.startHz + span / 2) - span * 0.25);
    try {
      await api("/api/rf/sweep", { method: "POST", body: { center_hz: newCenter } });
    } catch (err) { toast(err.message, "err"); }
  });

  document.getElementById("panRightBtn")?.addEventListener("click", async () => {
    if (!state.spectrumFrame) return;
    const f = state.spectrumFrame;
    const span = f.length * f.binSizeHz;
    const newCenter = Math.min(12400000000, (f.startHz + span / 2) + span * 0.25);
    try {
      await api("/api/rf/sweep", { method: "POST", body: { center_hz: newCenter } });
    } catch (err) { toast(err.message, "err"); }
  });

  document.getElementById("zoomInBtn")?.addEventListener("click", async () => {
    if (!state.spectrumFrame) return;
    const f = state.spectrumFrame;
    const curSpan = f.length * f.binSizeHz;
    const newSpan = Math.max(100000, curSpan * 0.5);
    const center = f.startHz + curSpan / 2;
    try {
      await api("/api/rf/sweep", { method: "POST", body: { center_hz: center, span_hz: newSpan } });
    } catch (err) { toast(err.message, "err"); }
  });

  document.getElementById("zoomOutBtn")?.addEventListener("click", async () => {
    if (!state.spectrumFrame) return;
    const f = state.spectrumFrame;
    const curSpan = f.length * f.binSizeHz;
    const newSpan = Math.min(12400000000, curSpan * 2.0);
    const center = f.startHz + curSpan / 2;
    try {
      await api("/api/rf/sweep", { method: "POST", body: { center_hz: center, span_hz: newSpan } });
    } catch (err) { toast(err.message, "err"); }
  });

  document.getElementById("resetSpanBtn")?.addEventListener("click", async () => {
    const curPresetKey = document.getElementById("presetSelect")?.value;
    const p = (state.presets || []).find((x) => x.key === curPresetKey);
    if (!p) {
      toast("Preset tidak ditemukan untuk mereset rentang", "warn");
      return;
    }
    try {
      await api("/api/rf/sweep", { method: "POST", body: { center_hz: p.center_hz, span_hz: p.span_hz } });
      toast(`Rentang direset ke bawaan preset: ${(p.span_hz / 1e6).toFixed(3)} MHz span`, "ok");
    } catch (err) { toast(err.message, "err"); }
  });

  // Modal Pemilihan Perangkat
  const devModal = document.getElementById("deviceModal");
  async function openDeviceModal() {
    if (!devModal) return;
    devModal.hidden = false;
    const banner = document.getElementById("devDriverStatus");
    const choiceSel = document.getElementById("modalDevChoice");
    const customWrap = document.getElementById("modalDevCustomSerialWrap");
    const infoBox = document.getElementById("modalDevActiveInfo");
    if (banner) {
      banner.className = "diag-banner";
      banner.textContent = "Memeriksa status driver dan perangkat USB...";
    }
    try {
      const data = await api("/api/rf/devices");
      if (!data.library_loaded) {
        banner.className = "diag-banner err";
        banner.textContent = `Driver library SA124B (${data.library_error || "sa_api.dll"}) tidak ditemukan. Mode simulator aktif. Jika memakai perangkat fisik, pastikan perangkat lunak Spike telah terinstal di Windows.`;
      } else if (!data.serials || !data.serials.length) {
        banner.className = "diag-banner warn";
        banner.textContent = "Driver sa_api.dll siap, namun tidak ada perangkat SA124B yang terdeteksi di port USB. Sambungkan kabel USB perangkat, pastikan perangkat terhubung, lalu tekan 'Pindai Ulang USB'.";
      } else {
        banner.className = "diag-banner ok";
        banner.textContent = `Driver siap. Ditemukan ${data.serials.length} perangkat SA124B di port USB: #${data.serials.join(", #")}.`;
      }

      const cur = data.current || {};
      choiceSel.innerHTML = "";

      const optAuto = document.createElement("option");
      optAuto.value = "hardware_auto";
      optAuto.textContent = "Instrumen Fisik (Deteksi Otomatis USB)";
      choiceSel.appendChild(optAuto);

      (data.serials || []).forEach((sn) => {
        const opt = document.createElement("option");
        opt.value = `hardware_${sn}`;
        opt.textContent = `Instrumen Fisik — Nomor Seri #${sn}`;
        choiceSel.appendChild(opt);
      });

      const optCustom = document.createElement("option");
      optCustom.value = "hardware_custom";
      optCustom.textContent = "Instrumen Fisik (Nomor Seri Manual…)";
      choiceSel.appendChild(optCustom);

      const optSim = document.createElement("option");
      optSim.value = "simulator";
      optSim.textContent = "Simulator SA124B (23276018)";
      choiceSel.appendChild(optSim);

      if (cur.configured_simulate) {
        choiceSel.value = "simulator";
      } else if (cur.configured_serial) {
        if ((data.serials || []).includes(cur.configured_serial)) {
          choiceSel.value = `hardware_${cur.configured_serial}`;
        } else {
          choiceSel.value = "hardware_custom";
          const custIn = document.getElementById("modalDevCustomSerial");
          if (custIn) custIn.value = cur.configured_serial;
        }
      } else {
        choiceSel.value = "hardware_auto";
      }

      if (customWrap) customWrap.hidden = choiceSel.value !== "hardware_custom";
      if (infoBox) {
        const act = cur.active || {};
        infoBox.textContent = `Perangkat aktif: ${act.model || "—"} (#${act.serial || "—"}) ${act.simulated ? "[Simulator]" : "[Fisik]"}`;
      }
      const applyBtn = document.getElementById("modalDevApplyBtn");
      if (applyBtn) {
        const isAdmin = state.user && state.user.role === "admin";
        applyBtn.disabled = !isAdmin;
        applyBtn.title = isAdmin ? "Terapkan konfigurasi perangkat" : "Hanya Administrator yang dapat mengubah konfigurasi perangkat keras.";
      }
    } catch (err) {
      if (banner) {
        banner.className = "diag-banner err";
        banner.textContent = `Gagal memeriksa perangkat: ${err.message}`;
      }
    }
  }

  document.getElementById("deviceChip")?.addEventListener("click", openDeviceModal);
  document.getElementById("modalDevCancelBtn")?.addEventListener("click", () => {
    if (devModal) devModal.hidden = true;
  });
  document.getElementById("modalDevRescanBtn")?.addEventListener("click", openDeviceModal);

  document.getElementById("modalDevChoice")?.addEventListener("change", (e) => {
    const customWrap = document.getElementById("modalDevCustomSerialWrap");
    if (customWrap) customWrap.hidden = e.target.value !== "hardware_custom";
  });

  document.getElementById("modalDevApplyBtn")?.addEventListener("click", async () => {
    const choice = document.getElementById("modalDevChoice")?.value;
    let simulate = false;
    let serial = null;
    if (choice === "simulator") {
      simulate = true;
    } else if (choice === "hardware_auto") {
      simulate = false;
      serial = null;
    } else if (choice === "hardware_custom") {
      simulate = false;
      const val = document.getElementById("modalDevCustomSerial")?.value.trim();
      serial = val ? parseInt(val, 10) : null;
    } else if (choice && choice.startsWith("hardware_")) {
      simulate = false;
      serial = parseInt(choice.split("_")[1], 10);
    }

    const applyBtn = document.getElementById("modalDevApplyBtn");
    applyBtn.disabled = true;
    applyBtn.textContent = "Menyambungkan…";
    try {
      const res = await api("/api/rf/device", { method: "POST", body: { simulate, serial } });
      const d = res.device || {};
      const desc = d.simulated ? "Mode simulator aktif" : `Tersambung ke SA124B (#${d.serial || "—"})`;
      toast(desc, res.warning ? "warn" : "ok");
      if (devModal) devModal.hidden = true;
      refreshStatus();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      applyBtn.disabled = false;
      applyBtn.textContent = "Terapkan & Sambung";
    }
  });

  // Header tabel okupansi: klik untuk mengurutkan
  document.querySelectorAll("#occTable th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.getAttribute("data-sort");
      if (state.occSortCol === col) {
        state.occSortAsc = !state.occSortAsc;
      } else {
        state.occSortCol = col;
        state.occSortAsc = col === "label" || col === "freq_mhz";
      }
      document.querySelectorAll("#occTable th.sortable").forEach((h) => {
        h.classList.remove("sort-asc", "sort-desc");
        h.removeAttribute("data-order");
      });
      th.classList.add(state.occSortAsc ? "sort-asc" : "sort-desc");
      th.setAttribute("data-order", state.occSortAsc ? "▲" : "▼");
      renderOccupancyTable();
    });
  });

  // Filter daftar rekaman berdasarkan frekuensi / nama file
  const recFilter = document.getElementById("recFilter");
  recFilter.addEventListener("input", () => {
    const q = recFilter.value.trim().toLowerCase();
    document.querySelectorAll("#recList .rec-item").forEach((item) => {
      item.hidden = q.length > 0 && !item.textContent.toLowerCase().includes(q);
    });
  });
  document.getElementById("recRefreshBtn").addEventListener("click", refreshRecordings);

  // Form admin: tambah pengguna
  document.getElementById("newUserForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("nuName").value.trim();
    const password = document.getElementById("nuPass").value;
    const role = document.getElementById("nuRole").value;
    try {
      await api("/api/auth/users", { method: "POST", body: { username, password, role } });
      toast(`Pengguna ${username} dibuat sebagai ${role}`, "ok");
      e.target.reset();
      refreshUsers();
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Form admin: ganti kata sandi sendiri
  document.getElementById("pwForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const oldPw = document.getElementById("pwOld").value;
    const newPw = document.getElementById("pwNew").value;
    try {
      await api("/api/auth/change-password", {
        method: "POST",
        body: { old_password: oldPw, new_password: newPw },
      });
      toast("Kata sandi diperbarui — silakan login ulang.", "ok");
      setTimeout(logout, 1500);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  // Form jadwal survei (operator/admin)
  function populateSchedulePresets() {
    const sel = document.getElementById("scPreset");
    if (!sel) return;
    sel.innerHTML = "";
    (state.presets || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = `${p.category}: ${p.name}`;
      sel.appendChild(opt);
    });
  }

  function toggleThresholdInputs() {
    const mode = document.getElementById("scThresholdMode")?.value;
    const offsetWrap = document.getElementById("scOffsetWrap");
    const fixedWrap = document.getElementById("scFixedWrap");
    if (!offsetWrap || !fixedWrap) return;
    offsetWrap.hidden = mode === "fixed";
    fixedWrap.hidden = mode === "adaptive";
  }

  document.getElementById("scThresholdMode")?.addEventListener("change", toggleThresholdInputs);

  // Form mode perangkat (admin)
  document.getElementById("devApplyBtn")?.addEventListener("click", async () => {
    const mode = document.getElementById("devMode").value;
    const serialVal = document.getElementById("devSerial").value.trim();
    const simulate = mode === "simulator";
    const serial = serialVal ? parseInt(serialVal, 10) : null;
    const statusBox = document.getElementById("devStatus");
    const btn = document.getElementById("devApplyBtn");

    btn.disabled = true;
    btn.textContent = "Menyambungkan…";
    if (statusBox) statusBox.textContent = "Mengonfigurasi ulang perangkat...";

    try {
      const res = await api("/api/rf/device", {
        method: "POST",
        body: { simulate, serial },
      });
      const d = res.device || {};
      const desc = d.simulated ? "Mode simulator aktif" : `Tersambung ke SA124B (#${d.serial || "—"})`;
      if (statusBox) {
        statusBox.textContent = res.warning ? `${desc} — perhatian: ${res.warning}` : desc;
        statusBox.style.color = res.warning ? "var(--warn)" : "var(--success)";
      }
      toast(desc, res.warning ? "warn" : "ok");
      refreshStatus();
    } catch (err) {
      if (statusBox) {
        statusBox.textContent = err.message;
        statusBox.style.color = "var(--danger)";
      }
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "Terapkan & Sambung Ulang";
    }
  });

  document.getElementById("newScheduleForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = document.getElementById("scName").value.trim();
    const preset = document.getElementById("scPreset").value;
    const start_at = document.getElementById("scStart").value;
    const duration_min = parseFloat(document.getElementById("scDuration").value);
    const repeat = document.getElementById("scRepeat").value;
    const threshold_mode = document.getElementById("scThresholdMode").value;
    const auto_record = document.getElementById("scAutoRecord").checked;
    const fixed_threshold_dbm = parseFloat(document.getElementById("scFixedThreshold").value);
    const adaptive_offset_db = parseFloat(document.getElementById("scAdaptiveOffset").value);

    if (!name || !start_at || !Number.isFinite(duration_min)) {
      toast("Lengkapi nama, waktu mulai, dan durasi.", "err");
      return;
    }

    try {
      await api("/api/schedules", {
        method: "POST",
        body: { name, preset, start_at, duration_min, repeat, enabled: true, auto_record, threshold_mode, fixed_threshold_dbm, adaptive_offset_db },
      });
      toast(`Jadwal "${name}" dibuat`, "ok");
      e.target.reset();
      refreshSchedules();
      // Pastikan preset dropdown di form terisi saat tampilan jadwal dibuka
      populateSchedulePresets();
      toggleThresholdInputs();
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

// --------------------------------------------------------------------------- //
// Sinkronisasi konfigurasi server -> panel kontrol
// --------------------------------------------------------------------------- //
async function syncConfigToForm() {
  try {
    const cfg = await api("/api/rf/config");
    const sw = cfg.sweep || {};
    const au = cfg.audio || {};
    const occ = cfg.occupancy || {};

    const set = (id, value) => {
      const el = document.getElementById(id);
      if (el && value !== undefined && value !== null) el.value = value;
    };
    set("centerInput", Number.isFinite(sw.center_hz) ? (sw.center_hz / 1e6).toFixed(4) : "");
    set("spanInput", Number.isFinite(sw.span_hz) ? (sw.span_hz / 1e6).toFixed(4) : "");
    set("rbwInput", Number.isFinite(sw.rbw_hz) ? (sw.rbw_hz / 1e3).toFixed(1) : "");
    set("refInput", sw.ref_level_dbm);
    set("attenInput", sw.atten_db);
    // Sinkronkan ref level & RBW ke state render supaya skala Y canvas mengikuti
    // konfigurasi sweep server, bukan konstanta yang terpotong di sisi klien.
    if (Number.isFinite(sw.ref_level_dbm)) state.refLevelDbm = sw.ref_level_dbm;
    if (Number.isFinite(sw.rbw_hz)) state.rbwHz = sw.rbw_hz;
    applyYRange();
    set("thresholdMode", occ.threshold_mode);
    set("offsetInput", occ.adaptive_offset_db);
    set("dwellInput", au.dwell_sec);

    const dev = cfg.device || {};
    const devModeSel = document.getElementById("devMode");
    if (devModeSel && dev.simulate !== undefined) {
      devModeSel.value = dev.simulate ? "simulator" : "hardware";
    }
    if (dev.serial) {
      set("devSerial", dev.serial);
    }

    const demodSel = document.getElementById("listenDemod");
    if (demodSel && au.demod) demodSel.value = au.demod;
    const monDemod = document.getElementById("monListenDemod");
    if (monDemod && au.demod) monDemod.value = au.demod;

    const listenFreq = document.getElementById("listenFreq");
    if (listenFreq && !listenFreq.value && sw.center_hz) {
      listenFreq.value = (sw.center_hz / 1e6).toFixed(4);
    }
    const monFreq = document.getElementById("monListenFreq");
    if (monFreq && sw.center_hz) {
      monFreq.value = (sw.center_hz / 1e6).toFixed(4);
    }

    // Deskripsi preset aktif + metadata kanal untuk legenda ITU-R
    const p = (state.presets || []).find((x) => x.key === sw.preset);
    if (p) {
      state.presetMeta = p;
      state.rbwHz = p.rbw_hz || sw.rbw_hz || state.rbwHz;
    }
    const desc = document.getElementById("presetDesc");
    if (desc && p) {
      desc.textContent = `${p.category} · ${(p.span_hz / 1e6).toFixed(3)} MHz span · ${p.channel_count} kanal · ${p.demod}`;
    }
  } catch {}
}

// --------------------------------------------------------------------------- //
// Fetch Data & Refresh UI Elements
// --------------------------------------------------------------------------- //
async function refreshStatus() {
  if (!state.token) return;
  try {
    const s = await api("/api/rf/status");
    state.device = s.device;
    state.rfState = s.state;

    // Chips
    const devChip = document.getElementById("deviceChip");
    const devText = document.getElementById("deviceText");
    if (s.device && s.device.connected) {
      devChip.className = "chip chip-running";
      devText.textContent = `${s.device.model} (#${s.device.serial})`;
    } else {
      devChip.className = "chip chip-idle";
      devText.textContent = "Terputus";
    }

    const stChip = document.getElementById("stateChip");
    const stText = document.getElementById("stateText");
    stText.textContent = s.state;
    stChip.className = "chip " + (s.state === "SWEEPING" ? "chip-running" : s.state === "DWELL_RECORD" ? "chip-recording" : s.state === "MANUAL_AUDIO" ? "chip-listening" : "chip-idle");

    // Selama dwell/demodulasi manual, sweep berhenti sehingga frame berhenti.
    // Polling status tetap menggambar ulang supaya overlay jeda terlihat.
    if (s.state === "DWELL_RECORD" || s.state === "MANUAL_AUDIO") {
      state.dwellFreqMhz = s.dwell_freq_hz ? s.dwell_freq_hz / 1e6 : state.dwellFreqMhz;
      scheduleDraw();
    }

    const metaText = document.getElementById("metaText");
    if (metaText) {
      // Di luar SWEEPING, counter fps engine menghitung iterasi loop audio
      // (~8/detik) — menampilkannya sebagai "fps" membuat operator mengira
      // sapuan masih berjalan padahal sudah dijeda.
      const fpsTxt = s.state === "SWEEPING" ? `${s.fps} fps` : "sapuan dijeda";
      metaText.textContent = `${fpsTxt} · ${s.temperature_c !== null ? s.temperature_c + " °C" : "—"}`;
    }

    // Sinkronisasi status autorecord
    const autoBox = document.getElementById("autoRecordToggle");
    if (autoBox && document.activeElement !== autoBox) {
      autoBox.checked = s.auto_record;
    }

    // Rekam manual aktif
    if (s.manual_recording !== undefined) {
      state.isManualRecording = !!s.manual_recording;
      syncAudioButtons();
      if (state.isManualRecording) {
        if (!state.manualRecordTimerId) {
          state.manualRecordTimerId = setInterval(_tickRecordTimer, 500);
        }
        _tickRecordTimer();
      } else {
        if (state.manualRecordTimerId) {
          clearInterval(state.manualRecordTimerId);
          state.manualRecordTimerId = null;
        }
        const el = document.getElementById("monRecordTimer");
        if (el) el.hidden = true;
      }
    }

    // Sinkronisasi occupancy tracking
    if (s.occupancy_tracking !== undefined) {
      state.occupancyTracking = !!s.occupancy_tracking;
      syncOccupancyButtons();
    }

    // Sinkronisasi dengar manual (state MANUAL_AUDIO)
    if (s.manual_audio !== undefined) {
      const wasListening = state.isListening;
      state.isListening = !!s.manual_audio;
      // Frekuensi & demod otoritatif datang dari engine, supaya tab lain
      // atau perubahan di luar kanvas ikut tercerminkan di garis pembawa.
      if (s.manual_freq_hz) state.listenFreqHz = Number(s.manual_freq_hz);
      if (s.manual_demod) state.listenDemod = String(s.manual_demod);
      syncAudioButtons();
      if (wasListening !== state.isListening) scheduleDraw();
    }

    // Pelajari threshold terkini dari status perangkat
    const thrInfo = document.getElementById("thrInfo");
    if (thrInfo && s.threshold_dbm !== undefined) {
      thrInfo.textContent = `Threshold: ${Number.isFinite(s.threshold_dbm) ? s.threshold_dbm.toFixed(1) : "—"} dBm`;
    }

    syncConfigToForm();
  } catch (err) {
    // Jangan telan senyap: kegagalan berulang (401/jaringan/500) sebelumnya tidak
    // meninggalkan jejak apa pun — operator hanya melihat angka status membeku.
    // Ditampilkan sekali dengan jeda 15 detik agar tidak membanjiri toast tiap 3 s.
    if (!state.statusErrShown) {
      state.statusErrShown = true;
      toast(`Status RF gagal dimuat: ${err.message}`, "err");
      setTimeout(() => { state.statusErrShown = false; }, 15000);
    }
  }
}

async function refreshPresets() {
  try {
    const res = await api("/api/rf/presets");
    state.presets = res.presets;
    const sel = document.getElementById("presetSelect");
    sel.innerHTML = "";
    const scSel = document.getElementById("scPreset");
    if (scSel) scSel.innerHTML = "";
    res.presets.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = `${p.category}: ${p.name}`;
      sel.appendChild(opt);

      if (scSel) {
        const scOpt = document.createElement("option");
        scOpt.value = p.key;
        scOpt.textContent = `${p.category}: ${p.name}`;
        scSel.appendChild(scOpt);
      }
    });
  } catch {}
}

// Pemformat defensif: satu field yang hilang dari API tidak boleh menggagalkan
// seluruh render tabel (dulu `catch {}` menelan galatnya sehingga tabel kosong
// tanpa jejak apa pun).
function fmtNum(v, digits = 0) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return digits > 0 ? n.toFixed(digits) : n.toLocaleString();
}

async function refreshOccupancy() {
  try {
    const res = await api("/api/occupancy/stats");
    state.occChannels = Array.isArray(res.channels) ? res.channels : [];
    renderOccupancyTable();
    document.getElementById("occSummary").textContent =
      `Total: ${res.total_channels ?? state.occChannels.length} kanal · Aktif: ${res.occupied_channels ?? 0}`;
  } catch (err) {
    // Tampilkan, jangan telan — kegagalan senyap di sini pernah menyembunyikan
    // bug selama beberapa sesi.
    console.error("Gagal memuat statistik okupansi:", err);
    toast(err.message || "Gagal memuat statistik okupansi", "err");
  }
}

function renderOccupancyTable() {
  const tbody = document.getElementById("occBody");
  if (!tbody) return;

  const col = state.occSortCol;
  const asc = state.occSortAsc;
  const sorted = [...state.occChannels].sort((a, b) => {
    let v1 = a[col] ?? 0;
    let v2 = b[col] ?? 0;
    if (typeof v1 === "string") return asc ? v1.localeCompare(v2) : v2.localeCompare(v1);
    return asc ? v1 - v2 : v2 - v1;
  });

  tbody.innerHTML = sorted.map((ch) => {
    const occPct = Number(ch.occupancy_pct);
    const pct = Number.isFinite(occPct) ? occPct : 0;
    let barClass = "low";
    if (pct >= 50) barClass = "extreme";
    else if (pct >= 20) barClass = "high";
    else if (pct >= 5) barClass = "med";

    const freqMhz = Number(ch.freq_mhz);
    const isChecked = state.occSelectedKeys.has(ch.key);
    return `
      <tr>
        <td class="chk-col">
          <label class="chk-wrap" title="Pilih kanal ${escapeHtml(ch.key)}">
            <input type="checkbox" class="occ-chk" data-key="${escapeHtml(ch.key)}" ${isChecked ? "checked" : ""}>
          </label>
        </td>
        <td><b>${Number.isFinite(freqMhz) ? freqMhz.toFixed(4) : "—"} MHz</b></td>
        <td>${escapeHtml(ch.label) || "—"}</td>
        <td class="num occupancy-cell">
          ${pct.toFixed(2)}%
          <div class="occ-bar"><div class="occ-bar-fill ${barClass}" style="width: ${Math.min(pct, 100)}%"></div></div>
        </td>
        <td class="num">${fmtNum(ch.n_above)}</td>
        <td class="num">${fmtNum(ch.n_total)}</td>
        <td class="num">${fmtNum(ch.peak_dbm, 1)} dBm</td>
        <td class="num">${fmtNum(ch.mean_dbm, 1)} dBm</td>
        <td class="num">${fmtNum(ch.events)}</td>
        <td>
          <button class="btn btn-ghost btn-sm" data-action="tuneAndListen" data-freq="${ch.freq_hz}">Dengar</button>
        </td>
      </tr>
    `;
  }).join("");

  // Sinkronkan status "pilih semua" & jumlah terpilih
  _refreshOccSelectionUI();
}

function _refreshOccSelectionUI() {
  const count = state.occSelectedKeys.size;
  const btn = document.getElementById("deleteSelectedOccBtn");
  const label = document.getElementById("occSelectedCount");
  const selectAll = document.getElementById("occSelectAll");
  if (btn) btn.disabled = count === 0;
  if (label) {
    label.hidden = count === 0;
    label.textContent = `${count} dipilih`;
  }
  if (selectAll) {
    const total = document.querySelectorAll("#occBody .occ-chk").length;
    selectAll.checked = total > 0 && count >= total;
    selectAll.indeterminate = count > 0 && count < total;
  }
}

window.tuneAndListen = (freqHz) => {
  _syncListenInputs(freqHz);
  switchView("monitor");
  document.getElementById("listenBtn")?.click();
};

// URL bermedia (tag <audio> / <a download>) tidak dapat mengirim header
// Authorization, jadi token disisipkan sebagai query string — persis seperti
// tautan ekspor laporan. Server menerimanya lewat _extract_token().
function authedUrl(path) {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}token=${encodeURIComponent(state.token || "")}`;
}

async function refreshRecordings() {
  if (!state.token) return;
  try {
    const res = await api("/api/recordings?limit=500");
    state.recordings = res.recordings || [];
    const container = document.getElementById("recList");
    if (!state.recordings.length) {
      container.innerHTML = '<p class="muted-sm" style="padding:12px;">Belum ada rekaman audio.</p>';
      _refreshRecSelectionUI();
      return;
    }
    container.innerHTML = state.recordings.map((r) => {
      const isChecked = state.recSelectedFilenames.has(r.filename);
      return `
        <div class="rec-item">
          <label class="chk-wrap" title="Pilih rekaman ${escapeHtml(r.filename)}">
            <input type="checkbox" class="rec-chk" data-filename="${escapeHtml(r.filename)}" ${isChecked ? "checked" : ""}>
          </label>
          <div class="rec-freq">${Number.isFinite(r.freq_mhz) ? r.freq_mhz.toFixed(4) : "—"} MHz</div>
          <div class="rec-info">
            <div><b>${escapeHtml(r.filename)}</b></div>
            <div class="muted-sm">${escapeHtml(r.modified)} · Durasi: ${Number.isFinite(r.duration_sec) ? r.duration_sec : "—"}s · ${Number.isFinite(r.size_bytes) ? (r.size_bytes / 1024).toFixed(1) : "—"} KB</div>
          </div>
          <div class="rec-actions">
            <audio controls preload="none" src="${authedUrl(`/api/recordings/${encodeURIComponent(r.filename)}/stream`)}" style="height:32px; vertical-align:middle;"></audio>
            <a class="btn btn-ghost btn-sm" href="${authedUrl(`/api/recordings/${encodeURIComponent(r.filename)}/download`)}" download>Unduh</a>
            ${state.user.role !== "viewer" ? `<button class="btn btn-danger btn-sm" data-action="deleteRec" data-filename="${escapeHtml(r.filename)}">Hapus</button>` : ""}
          </div>
        </div>
      `;
    }).join("");
    _refreshRecSelectionUI();
  } catch {}
}

function _refreshRecSelectionUI() {
  const count = state.recSelectedFilenames.size;
  const btn = document.getElementById("recDeleteSelectedBtn");
  const label = document.getElementById("recSelectedCount");
  const selectAll = document.getElementById("recSelectAll");
  if (btn) btn.disabled = count === 0;
  if (label) {
    label.hidden = count === 0;
    label.textContent = `${count} dipilih`;
  }
  if (selectAll) {
    const total = (state.recordings || []).length;
    selectAll.checked = total > 0 && count >= total;
    selectAll.indeterminate = count > 0 && count < total;
  }
}

window.deleteRec = async (filename) => {
  if (!confirm(`Hapus file rekaman ${filename}?`)) return;
  try {
    await api(`/api/recordings/${encodeURIComponent(filename)}`, { method: "DELETE" });
    toast("Rekaman dihapus", "ok");
    refreshRecordings();
  } catch (err) {
    toast(err.message, "err");
  }
};

// --------------------------------------------------------------------------- //
// Helper Keamanan & Sanitasi String untuk Menghindari XSS
// --------------------------------------------------------------------------- //
function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function refreshSchedules() {
  try {
    const res = await api("/api/schedules");
    const activeEl = document.getElementById("schedActive");
    if (res.active) {
      activeEl.hidden = false;
      const run = res.active;
      activeEl.innerHTML = `
        <div class="sched-name">● Sedang Berjalan: ${escapeHtml(run.name)} (${escapeHtml(run.preset)})</div>
        <div>Mulai: ${escapeHtml(run.started_at)} · Sisa: ${res.remaining_sec !== null ? Math.round(res.remaining_sec / 60) + " menit" : "—"}</div>
        <button class="btn btn-danger btn-sm" style="margin-top:6px;" data-action="cancelSurvey">Hentikan Survei</button>
      `;
    } else {
      activeEl.hidden = true;
    }

    const listEl = document.getElementById("schedList");
    listEl.innerHTML = res.schedules.map((s) => `
      <div class="sched-item">
        <div>
          <div class="sched-name">${escapeHtml(s.name)} (${escapeHtml(s.preset_name)})</div>
          <div class="sched-meta">Waktu: ${escapeHtml(s.start_at)} (${s.duration_min} mnt) · Ulang: ${escapeHtml(s.repeat)}</div>
          <div class="sched-meta muted-sm">Threshold: ${escapeHtml(s.threshold_mode)} (${s.threshold_mode === "adaptive" ? "+" + s.adaptive_offset_db + " dB" : s.fixed_threshold_dbm + " dBm"}) · Auto-record: ${s.auto_record ? "Ya" : "Tidak"}</div>
        </div>
        <div class="sched-ctrl">
          ${state.user.role !== "viewer" ? `<button class="btn btn-primary btn-sm" data-action="startScheduleNow" data-id="${escapeHtml(s.id)}">Mulai Sekarang</button>` : ""}
          ${state.user.role === "admin" ? `<button class="btn btn-danger btn-sm" data-action="deleteSchedule" data-id="${escapeHtml(s.id)}">Hapus</button>` : ""}
        </div>
      </div>
    `).join("");

    const histEl = document.getElementById("runHistory");
    histEl.innerHTML = res.recent_runs.map((r) => `
      <div class="run-item">
        <span class="run-status ${escapeHtml(r.status)}">[${escapeHtml(r.status.toUpperCase())}]</span>
        <div style="flex:1;">
          <b>${escapeHtml(r.name)}</b> (${escapeHtml(r.preset)}) · ${escapeHtml(r.started_at)}
          <div class="muted-sm">Okupansi rata-rata: ${r.occupancy_pct}% · Puncak: ${r.max_channel_power_dbm} dBm · ${r.recordings} rekaman</div>
        </div>
      </div>
    `).join("");
  } catch {}
}

window.startScheduleNow = async (id) => {
  try {
    await api(`/api/schedules/${id}/start-now`, { method: "POST" });
    toast("Kampanye survei dimulai", "ok");
    refreshSchedules();
  } catch (err) {
    toast(err.message, "err");
  }
};

window.cancelSurvey = async () => {
  if (!confirm("Hentikan survei aktif?")) return;
  try {
    await api("/api/schedules/cancel-active", { method: "POST" });
    toast("Survei dihentikan", "warn");
    refreshSchedules();
  } catch (err) {
    toast(err.message, "err");
  }
};

window.deleteSchedule = async (id) => {
  if (!confirm("Hapus jadwal ini?")) return;
  try {
    await api(`/api/schedules/${id}`, { method: "DELETE" });
    toast("Jadwal dihapus", "ok");
    refreshSchedules();
  } catch (err) {
    toast(err.message, "err");
  }
};

async function refreshUsers() {
  try {
    const list = await api("/api/auth/users");
    const tbody = document.getElementById("userBody");
    tbody.innerHTML = list.map((u) => `
      <tr>
        <td><b>${escapeHtml(u.username)}</b></td>
        <td><span class="role-tag">${escapeHtml(u.role)}</span></td>
        <td>${u.disabled ? '<span style="color:var(--danger)">Nonaktif</span>' : '<span style="color:var(--success)">Aktif</span>'}</td>
        <td style="text-align:right;">
          ${u.username !== state.user.username ? `<button class="btn btn-danger btn-sm" data-action="deleteUser" data-username="${escapeHtml(u.username)}">Hapus</button>` : ""}
        </td>
      </tr>
    `).join("");
  } catch {}
}

window.deleteUser = async (uname) => {
  if (!confirm(`Hapus pengguna ${uname}?`)) return;
  try {
    await api(`/api/auth/users/${uname}`, { method: "DELETE" });
    toast("Pengguna dihapus", "ok");
    refreshUsers();
  } catch (err) {
    toast(err.message, "err");
  }
};

// --------------------------------------------------------------------------- //
// Delegasi Event Klik untuk Tombol Aksi (Bebas dari Pelanggaran CSP Inline Event)
// --------------------------------------------------------------------------- //
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "tuneAndListen") {
    const freq = parseFloat(btn.dataset.freq);
    if (!isNaN(freq)) window.tuneAndListen(freq);
  } else if (action === "deleteRec") {
    const fn = btn.dataset.filename;
    if (fn) window.deleteRec(fn);
  } else if (action === "cancelSurvey") {
    window.cancelSurvey();
  } else if (action === "startScheduleNow") {
    const id = btn.dataset.id;
    if (id) window.startScheduleNow(id);
  } else if (action === "deleteSchedule") {
    const id = btn.dataset.id;
    if (id) window.deleteSchedule(id);
  } else if (action === "deleteUser") {
    const u = btn.dataset.username;
    if (u) window.deleteUser(u);
  }
});

// --------------------------------------------------------------------------- //
// Event Handlers & Notifikasi Toast
// --------------------------------------------------------------------------- //
function handleSystemEvent(evt) {
  if (evt.type === "recording_started") {
    toast(`Merekam audio ${evt.freq_mhz} MHz → ${evt.filename}`, "warn");
  } else if (evt.type === "recording_finished") {
    const m = evt.meta || {};
    const dur = m.duration_sec !== undefined ? `${m.duration_sec}s` : "—";
    toast(`Rekaman selesai: ${m.filename || "—"} (${dur})`, "ok");
    refreshRecordings();
  } else if (evt.type === "survey_started") {
    toast(`Kampanye survei dimulai: ${evt.name || ""}`, "info");
    refreshSchedules();
  } else if (evt.type === "survey_finished") {
    toast(`Kampanye survei selesai: ${evt.name || ""}`, "ok");
    refreshSchedules();
    refreshOccupancy();
  }
}

function toast(msg, type = "info") {
  const host = document.getElementById("toastHost");
  if (!host) return;
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  host.appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    setTimeout(() => t.remove(), 300);
  }, 3500);
}

// --------------------------------------------------------------------------- //
// Startup
// --------------------------------------------------------------------------- //
window.addEventListener("DOMContentLoaded", checkAuth);
