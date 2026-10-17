// followTask из ui.js: конец по 4xx, повтор при сбое сети, предел сбоев, один опрос на задачу.
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import { loadStatic } from "./load.mjs";

const { followTask } = await loadStatic("ui.js");
const realFetch = globalThis.fetch;

beforeEach(() => {
  // announce() пишет в #sr-status — хватает заглушки.
  globalThis.document = { getElementById: () => ({ textContent: "" }) };
});

afterEach(() => {
  globalThis.fetch = realFetch;
  delete globalThis.document;
});

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

// fetch по сценарию: функции по очереди, последняя повторяется.
function script(...steps) {
  let i = 0;
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    const step = steps[Math.min(i++, steps.length - 1)];
    return step();
  };
  return calls;
}

test("удалённая задача (404) — отказ, а не вечный опрос", async () => {
  const calls = script(() => json({ error: "Нет такой задачи." }, 404));
  const err = await followTask("/api/t/1", () => {}, { interval: 1 }).catch((e) => e);
  assert.equal(err.status, 404);
  assert.equal(calls.length, 1);
});

test("сбой сети — повтор, потом итог", async () => {
  const seen = [];
  script(
    () => Promise.reject(new TypeError("Failed to fetch")),
    () => json({ status: "running", title: "t" }),
    () => json({ status: "done", title: "t" }),
  );
  const task = await followTask("/api/t/2", (t) => seen.push(t.status), { interval: 1 });
  assert.equal(task.status, "done");
  assert.deepEqual(seen, ["running", "done"]);
});

test("сбои подряд до maxErrors — отказ с kind network", async () => {
  const calls = script(() => Promise.reject(new TypeError("Failed to fetch")));
  const err = await followTask("/api/t/3", () => {}, { interval: 1, maxErrors: 3 }).catch((e) => e);
  assert.equal(err.kind, "network");
  assert.equal(calls.length, 3);
});

test("повторный вызов на ту же задачу — тот же промис и последний onUpdate", async () => {
  let release;
  const gate = new Promise((r) => (release = r));
  const calls = script(
    () => gate.then(() => json({ status: "running" })),
    () => json({ status: "done" }),
  );
  const first = [];
  const second = [];
  const a = followTask("/api/t/4", (t) => first.push(t.status), { interval: 1 });
  const b = followTask("/api/t/4", (t) => second.push(t.status), { interval: 1 });
  assert.equal(a, b);
  release();
  await a;
  assert.deepEqual(first, []);
  assert.deepEqual(second, ["running", "done"]);
  assert.equal(calls.length, 2);
});

test("signal — опрос останавливается", async () => {
  script(() => json({ status: "running" }));
  const ctrl = new AbortController();
  const pending = followTask("/api/t/5", () => ctrl.abort(), { interval: 5, signal: ctrl.signal }).catch((e) => e);
  assert.equal((await pending).kind, "abort");
});
