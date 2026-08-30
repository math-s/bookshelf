/* Barcode scanning.
 *
 * Book barcodes are EAN-13, and an EAN-13 starting 978/979 is exactly an ISBN-13,
 * so a decoded barcode needs no translation — only validation.
 *
 * Detection uses the browser's native BarcodeDetector where it exists (Chrome,
 * Android) and falls back to ZXing from a CDN elsewhere (notably iOS Safari).
 * Camera access requires a secure context: HTTPS, or localhost. */

import { Config, api } from "./config.js";

const ZXING_CDN = "https://cdnjs.cloudflare.com/ajax/libs/zxing-library/0.19.1/umd/index.min.js";

const $ = (sel) => document.querySelector(sel);
const queue = new Map();      // isbn13 -> { seen, alreadyOwned }
let stream = null;
let running = false;
let detector = null;
let zxingReader = null;

/* --- ISBN validation (mirrors the server's rules) -------------------------- */

function checkDigit13(body) {
  const sum = [...body.slice(0, 12)].reduce(
    (acc, ch, i) => acc + (i % 2 === 0 ? 1 : 3) * Number(ch), 0);
  return String((10 - (sum % 10)) % 10);
}

function isValidIsbn13(value) {
  const digits = (value || "").replace(/[^0-9]/g, "");
  if (digits.length !== 13) return false;
  if (!digits.startsWith("978") && !digits.startsWith("979")) return false;
  return digits[12] === checkDigit13(digits);
}

function checkDigit10(body) {
  const sum = [...body.slice(0, 9)].reduce((acc, ch, i) => acc + (10 - i) * Number(ch), 0);
  const rem = (11 - (sum % 11)) % 11;
  return rem === 10 ? "X" : String(rem);
}

/** Accept a typed ISBN-10 too, converting it up so the queue holds one key type. */
function toIsbn13(value) {
  const raw = (value || "").replace(/[^0-9Xx]/g, "").toUpperCase();
  if (isValidIsbn13(raw)) return raw;
  if (raw.length === 10 && raw[9] === checkDigit10(raw)) {
    const body = "978" + raw.slice(0, 9);
    return body + checkDigit13(body);
  }
  return null;
}

/* --- UI ------------------------------------------------------------------- */

function setStatus(text) { $("#status").textContent = text; }

function notice(text) {
  const box = $("#notice");
  box.textContent = text;
  box.hidden = !text;
}

function renderQueue() {
  const list = $("#queue");
  list.replaceChildren(...[...queue.entries()].map(([isbn, meta]) => {
    const remove = document.createElement("button");
    remove.textContent = "✕";
    remove.title = "Remove";
    remove.addEventListener("click", () => { queue.delete(isbn); renderQueue(); });

    const li = document.createElement("li");
    li.append(isbn);
    if (meta.alreadyOwned) {
      const tag = document.createElement("span");
      tag.className = "dup";
      tag.textContent = "already in library";
      li.append(tag);
    }
    const spacer = document.createElement("div");
    spacer.className = "spacer";
    spacer.style.flex = "1";
    li.append(spacer, remove);
    return li;
  }));

  $("#queue-empty").hidden = queue.size > 0;
  const button = $("#import");
  button.hidden = queue.size === 0;
  button.textContent = `Add ${queue.size} book${queue.size === 1 ? "" : "s"}`;
}

/** Check the library so an already-shelved book is flagged before importing. */
async function markIfOwned(isbn13) {
  try {
    await api(`/api/books/${isbn13}`);
    return true;
  } catch {
    return false;   // 404 (not owned) or unreachable API — either way, let it queue
  }
}

async function enqueue(isbn13, { beep = false } = {}) {
  if (queue.has(isbn13)) return false;
  queue.set(isbn13, { seen: Date.now(), alreadyOwned: false });
  renderQueue();
  if (beep) navigator.vibrate?.(40);
  const owned = await markIfOwned(isbn13);
  if (owned && queue.has(isbn13)) {
    queue.get(isbn13).alreadyOwned = true;
    renderQueue();
  }
  return true;
}

/* --- detection ------------------------------------------------------------ */

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = src;
    tag.onload = resolve;
    tag.onerror = () => reject(new Error("could not load the barcode library"));
    document.head.append(tag);
  });
}

async function setUpDetector() {
  if ("BarcodeDetector" in window) {
    try {
      const formats = await window.BarcodeDetector.getSupportedFormats();
      if (formats.includes("ean_13")) {
        detector = new window.BarcodeDetector({ formats: ["ean_13"] });
        return "native";
      }
    } catch { /* fall through to ZXing */ }
  }
  await loadScript(ZXING_CDN);
  if (!window.ZXing) throw new Error("barcode library did not load");
  zxingReader = new window.ZXing.BrowserMultiFormatReader();
  return "zxing";
}

async function scanLoop() {
  const video = $("#video");
  let lastHit = 0;

  while (running) {
    try {
      let value = null;

      if (detector) {
        const results = await detector.detect(video);
        if (results.length) value = results[0].rawValue;
      } else if (zxingReader) {
        try {
          const result = await zxingReader.decodeOnceFromVideoElement(video);
          value = result?.getText?.() ?? null;
        } catch { /* no barcode in this frame */ }
      }

      if (value && Date.now() - lastHit > 900) {
        const isbn13 = toIsbn13(value);
        if (isbn13) {
          lastHit = Date.now();
          const added = await enqueue(isbn13, { beep: true });
          setStatus(added ? `Scanned ${isbn13}` : `${isbn13} is already in the queue`);
        } else {
          setStatus(`Ignored ${value} — not a book barcode`);
        }
      }
    } catch { /* transient decode error; keep scanning */ }

    await new Promise((r) => setTimeout(r, detector ? 220 : 60));
  }
}

async function start() {
  if (!window.isSecureContext) {
    notice("Camera access needs HTTPS (or localhost). Open this page over HTTPS, " +
           "or type ISBNs by hand below.");
    setStatus("Camera unavailable");
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    notice("This browser doesn't expose a camera API. Type ISBNs by hand below.");
    setStatus("Camera unavailable");
    return;
  }

  try {
    setStatus("Loading barcode reader…");
    const mode = await setUpDetector();
    setStatus("Requesting camera…");
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 } },
      audio: false,
    });
    const video = $("#video");
    video.srcObject = stream;
    await video.play();

    running = true;
    $("#start").hidden = true;
    $("#stop").hidden = false;
    setStatus(`Point at a barcode (${mode === "native" ? "native reader" : "ZXing"})`);
    scanLoop();
  } catch (err) {
    const denied = err?.name === "NotAllowedError";
    notice(denied
      ? "Camera permission was denied. Allow it in your browser settings, or type ISBNs by hand."
      : `Could not start the camera: ${err.message}`);
    setStatus("Camera unavailable");
    stop();
  }
}

function stop() {
  running = false;
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
  try { zxingReader?.reset(); } catch { /* already torn down */ }
  $("#start").hidden = false;
  $("#stop").hidden = true;
}

/* --- import --------------------------------------------------------------- */

async function importQueue() {
  if (!queue.size) return;
  const button = $("#import");
  button.disabled = true;
  button.textContent = "Importing…";
  const report = $("#report");
  report.replaceChildren();

  try {
    const result = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ isbns: [...queue.keys()] }),
    });
    const summary = document.createElement("div");
    summary.textContent =
      `${result.added.length} added, ${result.skipped.length} already present, ` +
      `${result.failed_count} failed.`;
    report.append(summary);
    if (result.failures.length) {
      const ul = document.createElement("ul");
      for (const f of result.failures) {
        const li = document.createElement("li");
        li.textContent = `${f.input} — ${f.reason}`;
        ul.append(li);
      }
      report.append(ul);
    }
    // Keep only what failed, so a retry doesn't re-send books already stored.
    const failed = new Set(result.failures.map((f) => f.input));
    for (const isbn of [...queue.keys()]) if (!failed.has(isbn)) queue.delete(isbn);
    renderQueue();
  } catch (err) {
    const error = document.createElement("div");
    error.style.color = "var(--abandoned)";
    error.textContent = err.message;
    report.append(error);
  } finally {
    button.disabled = false;
    renderQueue();
  }
}

/* --- wiring --------------------------------------------------------------- */

$("#start").addEventListener("click", start);
$("#stop").addEventListener("click", () => { stop(); setStatus("Stopped"); });
$("#import").addEventListener("click", importQueue);

$("#manual-add").addEventListener("click", addManual);
$("#manual-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addManual(); });

async function addManual() {
  const input = $("#manual-input");
  const isbn13 = toIsbn13(input.value);
  if (!isbn13) {
    notice(`"${input.value}" is not a valid ISBN.`);
    return;
  }
  notice("");
  await enqueue(isbn13);
  input.value = "";
}

if (!Config.apiBase && location.protocol === "file:") {
  notice("Open this page through the server (or set an API URL in the library's settings).");
}

renderQueue();
window.addEventListener("pagehide", stop);
