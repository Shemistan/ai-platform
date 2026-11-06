// «Настройки дайджеста» (PRD-034): страница показывает действующие значения и шлёт на сервер только
// изменённое — каждое в свой раздел настроек; нетронутые модель и голос не закрепляются.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

// Минимальный DOM: столько, сколько нужно h(), fill(), toast() и чтению полей формы.
const ids = new Map();
class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attrs = new Map();
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.hidden = false;
  }
  setAttribute(k, v) {
    this.attrs.set(k, String(v));
    if (k === "id") ids.set(String(v), this);
  }
  getAttribute(k) {
    return this.attrs.get(k) ?? null;
  }
  addEventListener(type, fn) {
    this.listeners.set(type, fn);
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  remove() {}
  get classList() {
    return { add() {}, remove() {}, toggle() {} };
  }
  get type() {
    return this.getAttribute("type");
  }
  get name() {
    return this.getAttribute("name");
  }
  get checked() {
    return this.isChecked ?? this.attrs.has("checked");
  }
  set checked(on) {
    this.isChecked = on;
  }
  get value() {
    if (this.tag !== "select") return this.ownValue;
    if (this.ownValue !== undefined) return this.ownValue;
    const options = this.children.filter((c) => c.tag === "option");
    return String((options.find((o) => o.attrs.has("selected")) || options[0])?.value ?? "");
  }
  set value(v) {
    this.ownValue = v;
  }
}
const walk = (node, seen = []) => {
  seen.push(node);
  for (const c of node.children || []) walk(c, seen);
  return seen;
};
const root = new FakeNode("body");
const inputs = (selector) => {
  const [, name] = selector.match(/name="([^"]+)"/);
  return walk(root).filter((n) => n.tag === "input" && n.name === name && n.checked);
};
for (const id of ["error", "form"]) {
  const node = new FakeNode("div");
  node.setAttribute("id", id);
  root.children.push(node);
}
globalThis.Node = FakeNode;
globalThis.location = { href: "http://127.0.0.1:8765/settings/digest/" };
globalThis.document = {
  body: root,
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_, tag) => new FakeNode(tag),
  createTextNode: (text) => Object.assign(new FakeNode("#text"), { textContent: text }),
  getElementById: (id) => ids.get(id) ?? null,
  querySelector: (selector) => inputs(selector)[0] ?? null,
  querySelectorAll: inputs,
};

const CONFIG = {
  defaults: { poster_mode: "local", images_mode: "local", clips_mode: "local", voice_mode: "tts", voice: "", channels: [], image_model: "z", video_model: "ltx", engine: "espeech", tempo: 1 },
  scenario_defaults: { digest: { poster_mode: "upload", poster_deadline: "", draw_images: true, clips: false, upscale_images: "none", shorts: false, slide_s: 8, reading: "normal", slide_motion: true, frame: "poster", branding: true, ai_badge: true, disclaimers: true, review_text: false, tempo: 1.25, channels: ["news"] } },
  image_models: [{ id: "z", label: "Z", installed: true }],
  video_models: [{ id: "ltx", label: "LTX", installed: true }],
  engines: [
    { id: "espeech", label: "ESpeech", default_voice: "A", voices: ["A"] },
    { id: "silero", label: "Silero", default_voice: "B", voices: ["B"] },
  ],
  channels: [
    { id: "news", label: "Новости", format: "landscape" },
    { id: "other", label: "Другой", format: "landscape" },
  ],
};
const posts = [];
let failNext = null;
let reads = 0;
globalThis.fetch = async (path, init) => {
  const answer = (status, body) => ({ ok: status < 400, status, text: async () => JSON.stringify(body), headers: new Map() });
  if (init.method === "GET") {
    reads += 1;
    return answer(200, path === "/api/studio/config" ? CONFIG : { schedule: { auto_publish: false } });
  }
  posts.push([path, JSON.parse(init.body)]);
  if (failNext) {
    const message = failNext;
    failNext = null;
    return answer(400, { error: message });
  }
  return answer(200, {});
};
const settle = () => new Promise((done) => setTimeout(done, 20));
const field = (name) => ids.get(`digest-${name}`);
const changed = async () => {
  walk(root).find((n) => n.tag === "form").listeners.get("change")();
  await settle();
};

await loadStatic("settings/digest/app.js");
await settle();

test("в форме — действующие значения дайджеста, сохранять нечего", async () => {
  assert.equal(field("ai_badge").checked, true);
  assert.equal(field("clips").checked, false);
  assert.equal(field("tempo").value, "1.25");
  assert.equal(inputs('input[name="digest-poster_mode"]:checked')[0].value, "upload");
  assert.deepEqual(inputs('input[name="digest-channels"]:checked').map((i) => i.value), ["news"]);
  await changed();
  assert.deepEqual(posts.splice(0), []);
});

test("снятые галочки уходят в digest_defaults — только они", async () => {
  field("draw_images").checked = false;
  field("ai_badge").checked = false;
  await changed();
  assert.deepEqual(posts.splice(0), [["/api/settings/section/digest_defaults", { draw_images: false, ai_badge: false }]]);
  await changed(); // сохранённое второй раз не шлётся
  assert.deepEqual(posts.splice(0), []);
});

test("смена движка уходит вместе с голосом", async () => {
  field("voice").value = "A";
  await changed();
  posts.splice(0);
  field("engine").value = "silero";
  field("voice").value = "";
  await changed();
  assert.deepEqual(posts.splice(0), [["/api/settings/section/digest_defaults", { engine: "silero", voice: "" }]]);
});

test("каналы и автопубликация — в свои разделы", async () => {
  const other = walk(root).find((n) => n.tag === "input" && n.name === "digest-channels" && n.value === "other");
  other.checked = true;
  field("auto_publish").checked = true;
  await changed();
  assert.deepEqual(posts.splice(0), [
    ["/api/settings/section/default_channels", { digest: ["news", "other"] }],
    ["/api/settings/section/schedule", { auto_publish: true }],
  ]);
});

test("отказ сервера: ошибка на странице и форма читается заново", async () => {
  const before = reads;
  failNext = "Неверное значение: frame.";
  field("frame").value = "square";
  await changed();
  assert.equal(ids.get("error").textContent, "Неверное значение: frame.");
  assert.equal(ids.get("error").hidden, false);
  assert.equal(reads, before + 2);
  posts.splice(0);
});
