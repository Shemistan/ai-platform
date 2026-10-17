// toast() из ui.js: стек в углу, обычный тост гаснет сам, ошибка висит до закрытия,
// текст уходит в общую область объявлений #sr-status (DS-009).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

// Минимальный DOM: столько, сколько нужно h(), toast() и announce().
class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attrs = new Map();
    this.children = [];
    this.listeners = new Map();
    this.parent = null;
    this.text = "";
  }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
    if (k === "id") registry.set(String(v), this);
  }
  getAttribute(k) {
    return this.attrs.get(k) ?? null;
  }
  addEventListener(type, fn) {
    this.listeners.set(type, fn);
  }
  append(...nodes) {
    for (const n of nodes) {
      n.parent = this;
      this.children.push(n);
    }
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((n) => n !== this);
    this.parent = null;
  }
  click() {
    this.listeners.get("click")?.();
  }
  set textContent(v) {
    this.text = v;
    this.children = [];
  }
  get textContent() {
    return this.text + this.children.map((c) => c.textContent).join("");
  }
}
class FakeText extends FakeNode {
  constructor(text) {
    super("#text");
    this.text = text;
  }
}
const registry = new Map();
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (t) => new FakeText(t),
  getElementById: (id) => {
    const n = registry.get(id);
    return n && (n.parent || n === document.body) ? n : null;
  },
  body: new FakeNode("body"),
};

const { toast, TOAST_MS, icon, iconButton } = await loadStatic("ui.js");
const stack = () => document.getElementById("toasts");

test("тост — в стеке, с тоном и в области объявлений", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const item = toast("Сохранено");
  assert.equal(stack().children.includes(item), true);
  assert.match(item.getAttribute("class"), /\btoast\b/);
  assert.match(item.getAttribute("class"), /tone-success/);
  t.mock.timers.tick(60);
  assert.equal(document.getElementById("sr-status").textContent, "Сохранено");
  t.mock.timers.tick(TOAST_MS);
  assert.equal(stack().children.includes(item), false);
});

test("ошибка висит до закрытия крестиком", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const item = toast("Не вышло", "danger");
  t.mock.timers.tick(TOAST_MS * 5);
  assert.equal(stack().children.includes(item), true);
  item.children.find((c) => c.tag === "button").click();
  assert.equal(stack().children.includes(item), false);
});

test("несколько тостов — стопкой в одном контейнере", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const a = toast("Раз", "info");
  const b = toast("Два", "info");
  assert.deepEqual(stack().children.slice(-2), [a, b]);
  assert.equal(TOAST_MS, 4000);
});

test("пустой текст — ничего", () => {
  assert.equal(toast(""), null);
});

test("крестик тоста — кнопка-иконка с именем, SVG скрыт от скринридера (DS-014)", () => {
  const item = toast("Ещё", "info");
  const close = item.children.find((c) => c.tag === "button");
  assert.equal(close.getAttribute("class"), "btn-icon");
  assert.equal(close.getAttribute("aria-label"), "Скрыть");
  assert.equal(close.textContent, "");
  const svg = close.children[0];
  assert.equal(svg.tag, "svg");
  assert.equal(svg.getAttribute("aria-hidden"), "true");
});

test("icon и iconButton: размер, свой класс и подсказка", () => {
  assert.equal(icon("star", { size: 20 }).getAttribute("width"), "20");
  const b = iconButton("prev", "Кадр 2 раньше", { class: "btn-icon--outline", title: "Раньше", disabled: true });
  assert.equal(b.getAttribute("class"), "btn-icon btn-icon--outline");
  assert.equal(b.getAttribute("aria-label"), "Кадр 2 раньше");
  assert.equal(b.getAttribute("title"), "Раньше");
  assert.equal(b.getAttribute("size"), null);
});
