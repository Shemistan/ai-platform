// h() из ui.js: текст детей — текстовые узлы (не разметка), обработчики on*, пропуск пустых
// атрибутов и опасных ссылок (FE-009, QA-025).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

// Минимальный DOM: столько, сколько нужно h().
class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attrs = new Map();
    this.children = [];
    this.listeners = new Map();
  }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
  }
  getAttribute(k) {
    return this.attrs.get(k) ?? null;
  }
  hasAttribute(k) {
    return this.attrs.has(k);
  }
  addEventListener(type, fn) {
    this.listeners.set(type, fn);
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
}
class FakeText extends FakeNode {
  constructor(text) {
    super("#text");
    this.data = text;
  }
}
globalThis.Node = FakeNode;
globalThis.location = { href: "http://127.0.0.1:8765/studio/" };
globalThis.document = { createElement: (tag) => new FakeNode(tag), createTextNode: (t) => new FakeText(t) };

const { h } = await loadStatic("ui.js");

test("строка-ребёнок — текстовый узел как есть, разметка не разбирается", () => {
  const p = h("p", {}, "<img src=x onerror=alert(1)>", 42);
  assert.equal(p.children.length, 2);
  assert.ok(p.children.every((c) => c instanceof FakeText));
  assert.equal(p.children[0].data, "<img src=x onerror=alert(1)>");
  assert.equal(p.children[1].data, "42");
});

test("null, undefined и false среди детей пропускаются, массив детей разворачивается", () => {
  const child = h("b");
  const p = h("p", {}, null, undefined, false, ["a", child], 0);
  assert.deepEqual(
    p.children.map((c) => (c instanceof FakeText ? c.data : c.tag)),
    ["a", "b", "0"],
  );
});

test("on* — обработчик события, а не атрибут", () => {
  let clicked = 0;
  const btn = h("button", { onclick: () => clicked++, onkeydown: () => {} });
  assert.equal(btn.hasAttribute("onclick"), false);
  btn.listeners.get("click")();
  assert.equal(clicked, 1);
  assert.equal(typeof btn.listeners.get("keydown"), "function");
});

test("атрибуты: пустые пропускаются, true — пустой атрибут, value — свойство", () => {
  const input = h("input", { id: "x", disabled: true, hidden: false, title: null, placeholder: undefined, value: "текст", tabindex: 0 });
  assert.equal(input.getAttribute("id"), "x");
  assert.equal(input.getAttribute("disabled"), "");
  assert.equal(input.hasAttribute("hidden"), false);
  assert.equal(input.hasAttribute("title"), false);
  assert.equal(input.hasAttribute("placeholder"), false);
  assert.equal(input.hasAttribute("value"), false);
  assert.equal(input.value, "текст");
  assert.equal(input.getAttribute("tabindex"), "0");
});

test("ссылки: javascript: и data:text/html не ставятся, http, относительные и data:image — да", () => {
  assert.equal(h("a", { href: "javascript:alert(1)" }).hasAttribute("href"), false);
  assert.equal(h("a", { href: " JaVaScRiPt:alert(1)" }).hasAttribute("href"), false);
  assert.equal(h("img", { src: "data:text/html,<b>x</b>" }).hasAttribute("src"), false);
  assert.equal(h("a", { href: "data:image/png;base64,AAAA" }).hasAttribute("href"), false);
  assert.equal(h("a", { href: "https://example.org/a" }).getAttribute("href"), "https://example.org/a");
  assert.equal(h("a", { href: "/board/" }).getAttribute("href"), "/board/");
  assert.equal(h("img", { src: "data:image/png;base64,AAAA" }).getAttribute("src"), "data:image/png;base64,AAAA");
});
