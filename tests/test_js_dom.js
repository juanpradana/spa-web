/**
 * Tes runtime frontend (Node, tanpa dependensi eksternal).
 *
 * Memuat static/app.js ke dalam VM context dengan DOM tiruan, lalu MEMANGGIL
 * fungsi-fungsi yang sebelumnya rusak untuk membuktikan tidak ada lagi
 * ReferenceError. Ini menangkap kelas bug yang lolos dari `node --check`
 * (yang hanya memeriksa sintaks, bukan resolusi identifier saat runtime).
 *
 * Jalankan:  node tests/test_js_dom.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP_JS = path.join(__dirname, "..", "static", "app.js");
const code = fs.readFileSync(APP_JS, "utf8");

// --------------------------------------------------------------------------
// DOM tiruan minimal
// --------------------------------------------------------------------------
const elements = new Map();
const toasts = [];   // pesan toast apa pun yang muncul selama tes

function makeElement(id) {
  const el = {
    id,
    value: "",
    hidden: false,
    disabled: false,
    checked: false,
    indeterminate: false,
    textContent: "",
    innerHTML: "",
    className: "",
    title: "",
    style: {},
    dataset: {},
    children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c)) : (on ? this._s.add(c) : this._s.delete(c)); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    appendChild(child) { this.children.push(child); return child; },
    remove() {},
    focus() {},
    click() { this._clicked = true; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getContext() { return makeCtx(); },
    getBoundingClientRect() { return { width: 800, height: 300, left: 0, top: 0 }; },
  };
  return el;
}

function makeCtx() {
  const noop = () => {};
  return {
    save: noop, restore: noop, scale: noop, clearRect: noop, fillRect: noop,
    beginPath: noop, moveTo: noop, lineTo: noop, stroke: noop, fill: noop,
    closePath: noop, arc: noop, rect: noop, roundRect: noop, setLineDash: noop,
    fillText: noop, drawImage: noop, putImageData: noop,
    createImageData: () => ({ data: new Uint8ClampedArray(4) }),
    createLinearGradient: () => ({ addColorStop: noop }),
    measureText: () => ({ width: 42 }),
    canvas: { width: 800, height: 300 },
  };
}

function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}

const documentMock = {
  getElementById: (id) => getEl(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => makeElement(`<${tag}>`),
  addEventListener: () => {},
  documentElement: makeElement("html"),
};

class FakeAudioContext {
  constructor() {
    this.state = "running";
    this.currentTime = 0;
    this.destination = {};
    this.sampleRate = 32000;
  }
  createGain() {
    return { connect() {}, gain: { value: 0.7, setValueAtTime() {} } };
  }
  createBuffer(ch, len) {
    return { length: len, duration: len / this.sampleRate, copyToChannel() {} };
  }
  createBufferSource() {
    return { buffer: null, connect() {}, start() {} };
  }
  async resume() {}
  async suspend() {}
}

// --------------------------------------------------------------------------
// Sandbox
// --------------------------------------------------------------------------
const sandbox = {
  console,
  Math,
  JSON,
  Date,
  Number,
  String,
  Boolean,
  Array,
  Object,
  Map,
  Set,
  Promise,
  Error,
  TypeError,
  ReferenceError,
  Uint8ClampedArray,
  Float32Array,
  isNaN,
  parseFloat,
  parseInt,
  encodeURIComponent,
  decodeURIComponent,
  document: documentMock,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  location: { protocol: "http:", host: "localhost:3487" },
  performance: { now: () => 0 },
  requestAnimationFrame: () => 0,
  // Timer tidak dijalankan agar proses Node tidak menggantung.
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  confirm: () => true,
  alert: () => {},
  FormData: class FormData {},
  fetch: async () => ({
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => ({ access_token: "tok", user: { username: "admin", role: "admin" } }),
  }),
  AudioContext: FakeAudioContext,
  WebSocket: class { constructor() {} close() {} },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.AudioContext = FakeAudioContext;
sandbox.window.addEventListener = () => {};

// Rekam isi toast agar galat yang tertelan try/catch tetap terdeteksi.
const toastHost = getEl("toastHost");
toastHost.appendChild = (child) => {
  toasts.push(String(child.textContent || ""));
  return child;
};

vm.createContext(sandbox);

// --------------------------------------------------------------------------
// Harness
// --------------------------------------------------------------------------
let pass = 0;
let fail = 0;

function check(label, cond, detail) {
  if (cond) {
    pass++;
    console.log(`  [LULUS] ${label}`);
  } else {
    fail++;
    console.log(`  [GAGAL] ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

async function main() {
  console.log("== Muat static/app.js ke DOM tiruan ==");
  try {
    vm.runInContext(code, sandbox, { filename: "app.js" });
    check("app.js dievaluasi tanpa galat", true);
  } catch (err) {
    check("app.js dievaluasi tanpa galat", false, err.message);
    console.log("\nHASIL: gagal memuat skrip, tes dihentikan.");
    process.exit(1);
  }

  console.log("\n== 1. _syncListenInputs dapat dipanggil dari scope modul ==");
  check("_syncListenInputs terdefinisi sebagai fungsi global",
    typeof sandbox._syncListenInputs === "function");
  if (typeof sandbox._syncListenInputs === "function") {
    getEl("monListenFreq").value = "";
    getEl("listenFreq").value = "";
    sandbox._syncListenInputs(101_400_000);
    check("bilah monitor diperbarui", getEl("monListenFreq").value === "101.4000",
      `nilai=${getEl("monListenFreq").value}`);
    check("drawer samping diperbarui", getEl("listenFreq").value === "101.4000",
      `nilai=${getEl("listenFreq").value}`);
  }

  console.log("\n== 2. _demodBandwidthHz mengembalikan lebar pita yang benar ==");
  if (typeof sandbox._demodBandwidthHz === "function") {
    check("FM = 150 kHz", sandbox._demodBandwidthHz("FM") === 150_000);
    check("AM = 8 kHz", sandbox._demodBandwidthHz("AM") === 8_000);
    check("USB = 3 kHz", sandbox._demodBandwidthHz("USB") === 3_000);
    check("LSB = 3 kHz", sandbox._demodBandwidthHz("LSB") === 3_000);
    check("CW  = 1 kHz", sandbox._demodBandwidthHz("CW") === 1_000);
    check("mode tak dikenal = 15 kHz", sandbox._demodBandwidthHz("XYZ") === 15_000);
    check("case-insensitive ('fm')", sandbox._demodBandwidthHz("fm") === 150_000);
  }

  console.log("\n== 3. startListening() tidak lagi melempar ReferenceError ==");
  toasts.length = 0;
  getEl("monListenFreq").value = "";
  getEl("listenFreq").value = "";
  let threw = null;
  try {
    await sandbox.startListening(101_400_000, "FM");
  } catch (err) {
    threw = err;
  }
  check("startListening tidak melempar keluar", threw === null, threw && threw.message);
  const scopeErr = toasts.find((t) => /is not defined/i.test(t));
  check("tidak ada toast 'is not defined'", !scopeErr, scopeErr);
  check("kedua input tersinkron setelah startListening",
    getEl("monListenFreq").value === "101.4000" && getEl("listenFreq").value === "101.4000",
    `mon=${getEl("monListenFreq").value} side=${getEl("listenFreq").value}`);

  console.log("\n== 4. window.tuneAndListen menyinkronkan kedua input ==");
  getEl("monListenFreq").value = "";
  getEl("listenFreq").value = "";
  sandbox.window.tuneAndListen(88_500_000);
  check("bilah monitor tersinkron dari tabel okupansi",
    getEl("monListenFreq").value === "88.5000", `nilai=${getEl("monListenFreq").value}`);
  check("drawer samping tersinkron",
    getEl("listenFreq").value === "88.5000", `nilai=${getEl("listenFreq").value}`);

  console.log("\n== 5. Tidak ada variabel mati drawScheduled ==");
  check("drawScheduled tidak diekspos", sandbox.drawScheduled === undefined);

  console.log("\n============================================================");
  console.log(`HASIL: ${pass} lulus, ${fail} gagal`);
  process.exit(fail === 0 ? 0 : 1);
}

main();
