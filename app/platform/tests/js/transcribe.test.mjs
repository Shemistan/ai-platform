// «Расшифровка» (PRD-032): подписи времени абзацев и размера файла.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { clock, sizeLabel } = await loadStatic("transcribe/view.js");

test("таймкод абзаца — как в text.md", () => {
  assert.equal(clock(0), "0:00");
  assert.equal(clock(65.9), "1:05");
  assert.equal(clock(3725), "1:02:05");
  assert.equal(clock(-3), "0:00");
  assert.equal(clock(undefined), "0:00");
});

test("размер файла для людей", () => {
  assert.equal(sizeLabel(512), "512 Б");
  assert.equal(sizeLabel(820 * 1024), "820 КБ");
  assert.equal(sizeLabel(14.5 * 2 ** 20), "14,5 МБ");
  assert.equal(sizeLabel(1.2 * 2 ** 30), "1,2 ГБ");
});
