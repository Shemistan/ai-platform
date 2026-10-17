// busy() из ui.js: двойной клик — один запрос, кнопка помечена занятой до ответа.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { busy, isBusy } = await loadStatic("ui.js");

function fakeButton() {
  const attrs = new Map();
  return {
    setAttribute: (k, v) => attrs.set(k, v),
    removeAttribute: (k) => attrs.delete(k),
    attr: (k) => attrs.get(k),
  };
}

test("второй вызов по той же кнопке до ответа — тот же промис, работа одна", async () => {
  const btn = fakeButton();
  let calls = 0;
  let release;
  const work = () => {
    calls += 1;
    return new Promise((r) => (release = r));
  };
  const a = busy(btn, work);
  const b = busy(btn, work);
  assert.equal(a, b);
  assert.equal(btn.attr("aria-busy"), "true");
  release("ok");
  assert.equal(await a, "ok");
  assert.equal(calls, 1);
  assert.equal(btn.attr("aria-busy"), undefined);
  await busy(btn, async () => calls++);
  assert.equal(calls, 2);
});

test("ключ держит запрос, даже если перерисовка подменила кнопку", async () => {
  let calls = 0;
  let release;
  const work = () => {
    calls += 1;
    return new Promise((r) => (release = r));
  };
  const a = busy(fakeButton(), work, "act:1:run");
  assert.equal(isBusy("act:1:run"), true);
  busy(fakeButton(), work, "act:1:run");
  release();
  await a;
  assert.equal(calls, 1);
  assert.equal(isBusy("act:1:run"), false);
});

test("ошибка работы снимает занятость и уходит вызывающему", async () => {
  const btn = fakeButton();
  await assert.rejects(busy(btn, async () => Promise.reject(new Error("нет"))), /нет/);
  assert.equal(btn.attr("aria-busy"), undefined);
});
