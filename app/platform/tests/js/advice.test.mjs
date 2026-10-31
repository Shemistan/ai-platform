// Советы по настройке (UX-030): команда для кнопки «Скопировать» — после последнего « — ».
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { adviceCommand } = await loadStatic("core/advice.js");

test("команда после тире, без точки в конце", () => {
  const text = "Каталог данных /Volumes/SSD/data доступен другим пользователям машины — chmod 700 '/Volumes/SSD/data'.";
  assert.equal(adviceCommand(text), "chmod 700 '/Volumes/SSD/data'");
});

test("без тире — команды нет", () => {
  assert.equal(adviceCommand("Просто совет."), null);
});
