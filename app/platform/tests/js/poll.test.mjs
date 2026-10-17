// poller и pollDelay из ui.js: расписание на фейковых таймерах и фейковой видимости вкладки.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { poller, pollDelay } = await loadStatic("ui.js");

// Фейковые таймеры: время двигаем руками, колбэки зовём по порядку.
function fakeTimers() {
  let now = 0;
  let seq = 0;
  const queue = new Map();
  return {
    setTimeout(fn, ms) {
      const id = ++seq;
      queue.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout(id) {
      queue.delete(id);
    },
    get pending() {
      return [...queue.values()].map((t) => t.at - now);
    },
    // Продвинуть время на ms, выполняя созревшие таймеры (и микрозадачи между ними).
    async advance(ms) {
      const end = now + ms;
      while (true) {
        await flush();
        const due = [...queue.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        queue.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = end;
      await flush();
    },
  };
}

const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

function fakeDoc() {
  const listeners = new Set();
  return {
    hidden: false,
    addEventListener: (_type, fn) => listeners.add(fn),
    removeEventListener: (_type, fn) => listeners.delete(fn),
    set(hidden) {
      this.hidden = hidden;
      for (const fn of listeners) fn();
    },
  };
}

test("pollDelay: быстрый, медленный, backoff до 30 с, скрытая вкладка не чаще раза в минуту", () => {
  const base = { fast: 1000, slow: 5000 };
  assert.equal(pollDelay({ ...base, active: true }), 1000);
  assert.equal(pollDelay({ ...base, active: false }), 5000);
  assert.equal(pollDelay({ ...base, active: true, failures: 1 }), 2000);
  assert.equal(pollDelay({ ...base, active: true, failures: 3 }), 8000);
  assert.equal(pollDelay({ ...base, active: true, failures: 10 }), 30000);
  assert.equal(pollDelay({ ...base, active: false, hidden: true }), 60000);
  assert.equal(pollDelay({ fast: 10000, slow: 120000, active: false, hidden: true }), 1200000);
  assert.equal(pollDelay({ ...base, active: false, hidden: true, hiddenMs: Infinity }), Infinity);
});

test("опрос: быстрый пока активно, медленный в простое", async () => {
  const timers = fakeTimers();
  let active = true;
  let calls = 0;
  const p = poller({ run: async () => calls++, fast: 1000, slow: 5000, isActive: () => active, timers, doc: fakeDoc() }).start();
  await timers.advance(0);
  assert.equal(calls, 1);
  await timers.advance(3000);
  assert.equal(calls, 4);
  active = false;
  await timers.advance(1000); // тик, который уже стоял в очереди
  await timers.advance(4999);
  assert.equal(calls, 5);
  await timers.advance(1);
  assert.equal(calls, 6);
  p.stop();
  await timers.advance(60000);
  assert.equal(calls, 6);
});

test("сбои — интервал растёт, после успеха — снова обычный; onError получает счётчик", async () => {
  const timers = fakeTimers();
  let fail = true;
  const seen = [];
  poller({
    run: async () => {
      if (fail) throw new Error("нет связи");
    },
    fast: 1000,
    slow: 1000,
    onError: (_err, n) => seen.push(n),
    timers,
    doc: fakeDoc(),
  }).start();
  await timers.advance(0);
  assert.deepEqual(timers.pending, [2000]);
  await timers.advance(2000);
  assert.deepEqual(timers.pending, [4000]);
  fail = false;
  await timers.advance(4000);
  assert.deepEqual(timers.pending, [1000]);
  assert.deepEqual(seen, [1, 2]);
});

test("скрытая вкладка — редкий опрос, при возврате — сразу", async () => {
  const timers = fakeTimers();
  const doc = fakeDoc();
  let calls = 0;
  poller({ run: async () => calls++, fast: 1000, slow: 3000, timers, doc }).start();
  await timers.advance(0);
  doc.set(true);
  assert.deepEqual(timers.pending, [60000]);
  await timers.advance(59999);
  assert.equal(calls, 1);
  doc.set(false);
  await timers.advance(0);
  assert.equal(calls, 2);
  assert.deepEqual(timers.pending, [3000]);
});

test("stop() отменяет запрос в полёте; now() во время запроса — повтор сразу после", async () => {
  const timers = fakeTimers();
  const signals = [];
  let release;
  const p = poller({
    run: (signal) => {
      signals.push(signal);
      return new Promise((r) => (release = r));
    },
    fast: 1000,
    slow: 1000,
    timers,
    doc: fakeDoc(),
  }).start();
  await timers.advance(0);
  p.now();
  release();
  await timers.advance(0);
  assert.equal(signals.length, 2);
  p.stop();
  assert.equal(signals[1].aborted, true);
});
