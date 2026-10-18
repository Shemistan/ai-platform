// Форма «Диктора» (PRD-017): персонаж с фото — только с согласием, задача — только с речью.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { characterPayload, faceConsent, jobPayload } = await loadStatic("avatar/form.js");

const CONSENT = { confirmed: true, owner: "  Шамистан  ", form: "self" };

test("фото человека без согласия не уходит, рисованный — уходит", () => {
  assert.match(characterPayload({ uploadId: "u", name: "Я", kind: "photo" }).error, /согласие/);
  assert.deepEqual(characterPayload({ uploadId: "u", name: " Кот  Учёный ", kind: "drawn" }), {
    payload: { upload_id: "u", name: "Кот Учёный", kind: "drawn" },
  });
  assert.deepEqual(characterPayload({ uploadId: "u", name: "Я", kind: "photo", consent: CONSENT }).payload.consent, {
    confirmed: true, owner: "Шамистан", form: "self",
  });
});

test("персонаж без картинки, имени или вида — ошибка", () => {
  assert.ok(characterPayload({ name: "Я", kind: "drawn" }).error);
  assert.ok(characterPayload({ uploadId: "u", name: "  ", kind: "drawn" }).error);
  assert.ok(characterPayload({ uploadId: "u", name: "я".repeat(81), kind: "drawn" }).error);
  assert.ok(characterPayload({ uploadId: "u", name: "Я", kind: "robot" }).error);
});

test("согласие на лицо", () => {
  assert.ok(faceConsent().error);
  assert.ok(faceConsent({ confirmed: "true", owner: "А", form: "self" }).error);
  assert.match(faceConsent({ confirmed: true, owner: " ", form: "self" }).error, /чьё/);
  assert.ok(faceConsent({ confirmed: true, owner: "А", form: "устно" }).error);
});

test("задача из текста: голос, скорость и настройки видео", () => {
  const { payload } = jobPayload({
    character: "c1", source: "text", text: "  Привет  ", engine: "higgs", voice: "Shem-v2", speed: "1.1",
    model: "sadtalker", size: "512", frame: "full", enhance: 1,
  });
  assert.deepEqual(payload, {
    character: "c1", source: "text", model: "sadtalker", size: 512, frame: "full", enhance: true,
    text: "Привет", engine: "higgs", voice: "Shem-v2", speed: 1.1,
  });
});

test("задача без персонажа, текста или озвучки — ошибка", () => {
  assert.match(jobPayload({ source: "text", text: "a" }).error, /персонажа/);
  assert.match(jobPayload({ character: "c", source: "text", text: "  " }).error, /текст/);
  assert.match(jobPayload({ character: "c", source: "text", text: "abc", maxText: 2 }).error, /длиннее/);
  assert.match(jobPayload({ character: "c", source: "audio" }).error, /озвучку/);
  assert.deepEqual(jobPayload({ character: "c", source: "audio", uploadId: "u", size: 256, frame: "crop" }).payload.upload_id, "u");
  assert.ok(jobPayload({ character: "c", source: "video" }).error);
});
