// Форма записи образца голоса не уходит без согласия владельца (CMP-010).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { voiceConsent } = await loadStatic("core/consent.js");

test("без галочки — ошибка, форма не отправляется", () => {
  assert.ok(voiceConsent({ owner: "Аня", form: "self" }).error);
  assert.ok(voiceConsent({ confirmed: "true", owner: "Аня", form: "self" }).error);
  assert.ok(voiceConsent().error);
});

test("без имени владельца или с чужой формой — ошибка", () => {
  assert.match(voiceConsent({ confirmed: true, owner: "   ", form: "self" }).error, /чей/);
  assert.ok(voiceConsent({ confirmed: true, owner: "Аня", form: "устно" }).error);
  assert.ok(voiceConsent({ confirmed: true, owner: "а".repeat(201), form: "self" }).error);
});

test("с согласием — тело для сервера", () => {
  assert.deepEqual(voiceConsent({ confirmed: true, owner: "  Аня   Петрова ", form: "written" }), {
    consent: { confirmed: true, owner: "Аня Петрова", form: "written" },
  });
});
