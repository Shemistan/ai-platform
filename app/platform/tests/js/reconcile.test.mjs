// reconcile/captureFocus/restoreFocus из ui.js: перерисовка по опросу не сбрасывает набранное (FE-001, FE-012).
// Свой крошечный DOM: только то, чем пользуются эти функции.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

class El {
  constructor(tag, { id = "", type = "", value = "" } = {}) {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.type = type;
    this.value = value;
    this.defaultValue = value;
    this.children = [];
    this.parent = null;
    this.dataset = {};
    this.attrs = {};
  }
  append(...nodes) {
    for (const n of nodes) {
      n.parent?.children.splice(n.parent.children.indexOf(n), 1);
      n.parent = this;
      this.children.push(n);
    }
    return this;
  }
  insertBefore(node, ref) {
    node.parent?.children.splice(node.parent.children.indexOf(node), 1);
    node.parent = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node);
    else this.children.splice(i, 0, node);
  }
  remove() {
    this.parent?.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  get isConnected() {
    let n = this;
    while (n.parent) n = n.parent;
    return n === doc.body;
  }
  contains(el) {
    for (let n = el; n; n = n.parent) if (n === this) return true;
    return false;
  }
  *walk() {
    for (const c of this.children) {
      yield c;
      yield* c.walk();
    }
  }
  querySelectorAll(selector) {
    const tags = selector.split(",").map((s) => s.trim().replace(/\[.*\]$/, "").toUpperCase());
    return [...this.walk()].filter((n) => tags.includes(n.tagName) && (!selector.includes("[src]") || n.attrs.src));
  }
  getAttribute(k) {
    return this.attrs[k] ?? null;
  }
  focus() {
    doc.activeElement = this;
  }
  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }
}

const doc = {
  body: new El("body"),
  activeElement: null,
  getElementById(id) {
    return [...this.body.walk()].find((n) => n.id === id) || null;
  },
};
globalThis.document = doc;

const { reconcile, captureFocus, restoreFocus } = await loadStatic("ui.js");

function card(id, value = "") {
  return new El("article").append(new El("input", { id: `views-${id}`, type: "number", value }));
}

function setup() {
  doc.body.children = [];
  doc.activeElement = null;
  const root = new El("div");
  doc.body.append(root);
  return root;
}

test("та же подпись — узел остаётся, новая — пересобирается, порядок как в items", () => {
  const root = setup();
  reconcile(root, [
    { key: "a", sig: "1", build: () => card("a") },
    { key: "b", sig: "1", build: () => card("b") },
  ]);
  const [a, b] = root.children;
  reconcile(root, [
    { key: "b", sig: "2", build: () => card("b") },
    { key: "a", sig: "1", build: () => card("a") },
  ]);
  assert.equal(root.children[1], a);
  assert.notEqual(root.children[0], b);
  assert.equal(root.children[0].dataset.key, "b");
  reconcile(root, [{ key: "a", sig: "1", build: () => card("a") }]);
  assert.deepEqual(root.children, [a]);
});

test("набранное в поле с id переносится в пересобранный узел, фокус и курсор возвращаются", () => {
  const root = setup();
  reconcile(root, [{ key: "p1", sig: "1", build: () => card("p1") }], "active");
  const input = root.children[0].children[0];
  input.value = "1234";
  input.focus();
  input.selectionStart = 2;
  input.selectionEnd = 3;
  const saved = captureFocus(doc.body);
  reconcile(root, [{ key: "p1", sig: "2", build: () => card("p1") }], "active");
  restoreFocus(saved);
  const fresh = root.children[0].children[0];
  assert.notEqual(fresh, input);
  assert.equal(fresh.value, "1234");
  assert.equal(doc.activeElement, fresh);
  assert.equal(fresh.selectionStart, 2);
  assert.equal(fresh.selectionEnd, 3);
});

test("другая область (scope) — правки не переносятся", () => {
  const root = setup();
  reconcile(root, [{ key: "p1", sig: "1", build: () => card("p1") }], "active");
  root.children[0].children[0].value = "7";
  reconcile(root, [{ key: "p1", sig: "2", build: () => card("p1") }], "archived");
  assert.equal(root.children[0].children[0].value, "");
});

test("фокус в поле без id — блок ждёт и не пересобирается", () => {
  const root = setup();
  const build = () => new El("article").append(new El("input", { type: "text" }));
  reconcile(root, [{ key: "x", sig: "1", build }]);
  const node = root.children[0];
  node.children[0].focus();
  reconcile(root, [{ key: "x", sig: "2", build }]);
  assert.equal(root.children[0], node);
});
