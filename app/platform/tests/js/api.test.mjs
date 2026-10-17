// api() из ui.js: виды ошибок (network, timeout, http, abort) на подменённом fetch.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { loadStatic } from "./load.mjs";

const { api, ApiError } = await loadStatic("ui.js");
const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});

// fetch, который ждёт отмены и не отвечает сам.
function hangingFetch() {
  return (_url, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
    });
}

test("сбой сети — kind network и «Сервис не отвечает»", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  const err = await api("GET", "/api/x").catch((e) => e);
  assert.ok(err instanceof ApiError);
  assert.equal(err.kind, "network");
  assert.match(err.message, /^Сервис не отвечает/);
});

test("зависший запрос — kind timeout через заданное время", async () => {
  globalThis.fetch = hangingFetch();
  const started = Date.now();
  const err = await api("GET", "/api/x", undefined, { timeout: 50 }).catch((e) => e);
  assert.equal(err.kind, "timeout");
  assert.ok(Date.now() - started < 1000);
});

test("отмена своим signal — kind abort", async () => {
  globalThis.fetch = hangingFetch();
  const ctrl = new AbortController();
  const pending = api("GET", "/api/x", undefined, { signal: ctrl.signal }).catch((e) => e);
  ctrl.abort();
  assert.equal((await pending).kind, "abort");
});

test("ответ-ошибка с JSON — текст из поля error", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "Нет такой статьи." }), { status: 404 });
  const err = await api("GET", "/api/x").catch((e) => e);
  assert.equal(err.kind, "http");
  assert.equal(err.status, 404);
  assert.equal(err.message, "Нет такой статьи.");
});

test("ответ-ошибка без JSON — человеческий текст, код и первая строка в скобках", async () => {
  globalThis.fetch = async () => new Response("\n<h1>Bad Gateway</h1>\nnginx", { status: 502 });
  const err = await api("GET", "/api/x").catch((e) => e);
  assert.equal(err.kind, "http");
  assert.equal(err.message, "Сервис ответил ошибкой (502: Bad Gateway). Обнови страницу; если повторится — посмотри лог сервиса.");
});

test("ответ-ошибка с code — err.code; без JSON — null (BE-011)", async () => {
  const body = { error: "Движок недоступен.", code: "engine_unavailable", kind: "unavailable" };
  globalThis.fetch = async () => new Response(JSON.stringify(body), { status: 503 });
  const err = await api("GET", "/api/x").catch((e) => e);
  assert.equal(err.code, "engine_unavailable");
  assert.equal(err.data.kind, "unavailable");
  assert.equal(err.message, "Движок недоступен.");
  globalThis.fetch = async () => new Response("oops", { status: 502 });
  assert.equal((await api("GET", "/api/x").catch((e) => e)).code, null);
});

test("413 — «Файл больше лимита»", async () => {
  globalThis.fetch = async () => new Response("Request Entity Too Large", { status: 413 });
  const err = await api("POST", "/api/upload", new Blob(["x"])).catch((e) => e);
  assert.equal(err.message, "Файл больше лимита.");
});

test("успех — JSON; тело JSON уходит с Content-Type", async () => {
  let seen = null;
  globalThis.fetch = async (url, init) => {
    seen = init;
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  assert.deepEqual(await api("POST", "/api/x", { a: 1 }), { ok: true });
  assert.equal(seen.headers["Content-Type"], "application/json");
  assert.equal(seen.body, '{"a":1}');
});

test("у загрузки (Blob) по умолчанию нет таймаута", async () => {
  let signal = null;
  globalThis.fetch = async (_url, init) => {
    signal = init.signal;
    await new Promise((r) => setTimeout(r, 30));
    return new Response("{}", { status: 200 });
  };
  await api("POST", "/api/upload", new Blob(["x"]), {});
  assert.equal(signal.aborted, false);
});
