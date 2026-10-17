// explainError из ui.js: известные поломки — человеческим текстом, сырой вывод — в details.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { explainError, ApiError } = await loadStatic("ui.js");

const ARGPARSE = "Движок упал: usage: narrate [-h] {say,library} ... narrate: error: argument command: invalid choice: 'bridge' (choose from 'say', 'library')";

test("сырой argparse — «движок недоступен», текст только в details", () => {
  const out = explainError(new ApiError(ARGPARSE, { kind: "http", status: 400 }));
  assert.equal(out.engine, true);
  assert.doesNotMatch(out.reason, /invalid choice/);
  assert.equal(out.details, ARGPARSE);
});

test("503 с kind=unavailable — движок недоступен", () => {
  const err = new ApiError("Команда «narrate» не отвечает.", { kind: "http", status: 503, data: { kind: "unavailable" } });
  assert.equal(explainError(err).engine, true);
});

test("сеть — «не отвечает» и что делать", () => {
  const out = explainError(new ApiError("Сервис не отвечает.", { kind: "network" }));
  assert.match(out.reason, /не отвечает/);
  assert.match(out.help, /Повторить/);
});

test("500 с request_id — код для поиска в логе", () => {
  const err = new ApiError("Внутренняя ошибка. Код для поиска в логе: abc123", { kind: "http", status: 500, data: { request_id: "abc123" } });
  assert.match(explainError(err).help, /abc123/);
});

test("короткая ошибка сервера — как есть, без подробностей", () => {
  const out = explainError(new ApiError("Нет такой статьи.", { kind: "http", status: 404 }));
  assert.equal(out.reason, "Нет такой статьи.");
  assert.equal(out.details, null);
});

test("строка с токеном Telegram — про токен", () => {
  assert.match(explainError("Telegram: 401 Unauthorized").reason, /токен/);
});

test("длинный неизвестный текст — общий текст и details", () => {
  const raw = "Traceback (most recent call last):\n  File x\nValueError: boom";
  const out = explainError(raw);
  assert.equal(out.reason, "Что-то пошло не так.");
  assert.equal(out.details, raw);
});
