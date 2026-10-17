// uploadFile из ui.js (FE-003): лимит до запроса, прогресс, ошибки как у api(), отмена, beforeunload.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

// Подменный XMLHttpRequest: тест сам решает, чем кончится запрос.
const sent = [];
class FakeXHR {
  constructor() {
    this.headers = {};
    this.upload = {};
    sent.push(this);
  }
  open(method, url) {
    Object.assign(this, { method, url });
  }
  setRequestHeader(k, v) {
    this.headers[k] = v;
  }
  send(body) {
    this.body = body;
  }
  abort() {
    this.onabort?.();
  }
  progress(loaded, total) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total });
  }
  respond(status, payload) {
    this.status = status;
    this.responseText = typeof payload === "string" ? payload : JSON.stringify(payload);
    this.onload?.();
  }
}
globalThis.XMLHttpRequest = FakeXHR;
const listeners = new Map();
globalThis.addEventListener = (type, fn) => listeners.set(type, fn);
globalThis.removeEventListener = (type, fn) => listeners.get(type) === fn && listeners.delete(type);

const { uploadFile, uploadsInFlight } = await loadStatic("ui.js");
const file = (size, name = "clip.mp4") => ({ size, name });
const last = () => sent[sent.length - 1];

test("файл больше лимита — отказ с размером лимита, запроса нет", async () => {
  const before = sent.length;
  await assert.rejects(uploadFile("/api/x/uploads", file(30 * 2 ** 20), { limit: 25 * 2 ** 20 }), (err) => {
    assert.equal(err.kind, "too_big");
    assert.equal(err.message, "Файл больше 25 МБ.");
    return true;
  });
  assert.equal(sent.length, before);
});

test("прогресс, заголовок X-AIP, JSON ответа; beforeunload только пока идёт", async () => {
  const seen = [];
  const run = uploadFile("/api/x/uploads", file(100), { limit: 1000, onProgress: (p) => seen.push(p.percent) });
  const xhr = last();
  assert.equal(xhr.method, "POST");
  assert.equal(xhr.headers["X-AIP"], "1");
  assert.equal(uploadsInFlight(), 1);
  assert.ok(listeners.has("beforeunload"));
  const event = { preventDefault() {
    this.prevented = true;
  } };
  listeners.get("beforeunload")(event);
  assert.ok(event.prevented);
  xhr.progress(25, 100);
  xhr.progress(100, 100);
  xhr.respond(201, { id: "abc" });
  assert.deepEqual(await run, { id: "abc" });
  assert.deepEqual(seen, [25, 100]);
  assert.equal(uploadsInFlight(), 0);
  assert.ok(!listeners.has("beforeunload"));
});

test("ошибка сервера — текст error и код для лога; без JSON — код ответа", async () => {
  let run = uploadFile("/u", file(1));
  last().respond(400, { error: "Не картинка.", request_id: "r1" });
  await assert.rejects(run, (err) => err.kind === "http" && err.status === 400 && err.message === "Не картинка. Код для поиска в логе: r1");
  run = uploadFile("/u", file(1));
  last().respond(502, "<h1>Bad Gateway</h1>");
  await assert.rejects(run, /Ошибка 502: Bad Gateway/);
  run = uploadFile("/u", file(1));
  last().onerror();
  await assert.rejects(run, (err) => err.kind === "network");
});

test("отмена через signal — kind abort, лоток и счётчик чистые", async () => {
  const ctrl = new AbortController();
  const run = uploadFile("/u", file(1), { signal: ctrl.signal });
  ctrl.abort();
  await assert.rejects(run, (err) => err.kind === "abort");
  assert.equal(uploadsInFlight(), 0);
  await assert.rejects(uploadFile("/u", file(1), { signal: ctrl.signal }), (err) => err.kind === "abort");
});
