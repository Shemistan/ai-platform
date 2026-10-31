// «Сегодня» (UX-031): что делать с введённым, какие выпуски показать, приветствие.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { greeting, guessIntent, pickAttention, POST_LIMIT } = await loadStatic("core/today.js");

test("поле: пусто, ссылка, короткий и длинный текст, видео и не видео", () => {
  assert.equal(guessIntent("   "), null);
  assert.equal(guessIntent(" https://habr.com/ru/articles/1/ ").kind, "url");
  assert.equal(guessIntent("https://a.b/ и ещё слова").kind, "post"); // ссылка внутри текста — это текст
  assert.equal(guessIntent("Коротко о главном").kind, "post");
  assert.equal(guessIntent("а".repeat(POST_LIMIT + 1)).kind, "article");
  assert.equal(guessIntent({ name: "запись.MOV", type: "" }).kind, "montage");
  assert.equal(guessIntent({ name: "кадр.png", type: "image/png" }).action, null);
});

test("«Ждут тебя»: только attention, свежие первыми, остаток — счётчиком", () => {
  const projects = [
    { id: "a", group: "attention", updated_at: 1 },
    { id: "b", group: "published", updated_at: 9 },
    { id: "c", group: "attention", updated_at: 5 },
    { id: "d", group: "attention", updated_at: 3 },
    { id: "e", group: "attention", updated_at: 4 },
  ];
  const { shown, more } = pickAttention(projects, 3);
  assert.deepEqual(shown.map((p) => p.id), ["c", "e", "d"]);
  assert.equal(more, 1);
  assert.deepEqual(pickAttention(undefined), { shown: [], more: 0 });
});

test("приветствие по часу", () => {
  assert.equal(greeting(7), "Доброе утро");
  assert.equal(greeting(13), "Добрый день");
  assert.equal(greeting(20), "Добрый вечер");
  assert.equal(greeting(2), "Доброй ночи");
});
