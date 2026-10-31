// Строка шага студии (UX-032): когда «Перезапустить шаг» тихая.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

globalThis.location ??= { search: "?scenario=article", href: "http://127.0.0.1/studio/?scenario=article" };
const { quietRerun } = await loadStatic("studio/state.js");

test("готовый и пропущенный свёрнутый шаг — тихая кнопка", () => {
  assert.equal(quietRerun({ status: "done" }, true), true);
  assert.equal(quietRerun({ status: "skipped" }, true), true);
});

test("раскрытый, упавший и ждущий — кнопка видна всегда", () => {
  assert.equal(quietRerun({ status: "done" }, false), false);
  assert.equal(quietRerun({ status: "failed" }, true), false);
  assert.equal(quietRerun({ status: "waiting" }, true), false);
});
