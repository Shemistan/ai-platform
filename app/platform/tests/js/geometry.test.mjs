// geometry() из photo/geometry.js: размер под сетку модели, пределы стороны и поля outpaint (FE-009).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { geometry } = await loadStatic("photo/geometry.js");

const base = { grid: 64, maxSide: 2048, workSide: 1024, outpaint: false, pads: { left: 25, top: 0, right: 25, bottom: 0 } };
const run = (over) => geometry({ ...base, ...over });

test("без outpaint поля нулевые, размер — по сетке", () => {
  assert.deepEqual(run({ source: { width: 1024, height: 768 } }), {
    sw: 1024,
    sh: 768,
    pad: { left: 0, right: 0, top: 0, bottom: 0 },
    cw: 1024,
    ch: 768,
  });
});

test("outpaint: холст с полями укладывается в рабочую сторону, поля кратны сетке", () => {
  const g = run({ source: { width: 1024, height: 768 }, outpaint: true });
  assert.equal(g.sw, 704); // 1024 · (1024 / 1536) = 682.7 → 704 по сетке 64
  assert.equal(g.sh, 512);
  assert.deepEqual(g.pad, { left: 192, right: 192, top: 0, bottom: 0 });
  assert.equal(g.cw, 704 + 384);
  assert.equal(g.ch, 512);
  for (const v of [g.sw, g.sh, ...Object.values(g.pad)]) assert.equal(v % 64, 0);
});

test("маленькое поле — не меньше одного шага сетки", () => {
  const g = run({ source: { width: 1024, height: 1024 }, outpaint: true, pads: { left: 1, right: 0, top: 0, bottom: 0 } });
  assert.equal(g.pad.left, 64);
  assert.equal(g.pad.right, 0);
  assert.equal(g.cw, g.sw + 64);
});

test("крохотное фото тянется до 256, огромное — режется до maxSide", () => {
  const small = run({ source: { width: 100, height: 60 } });
  assert.equal(small.sw, 256);
  assert.equal(small.sh, 256);
  const huge = run({ source: { width: 8000, height: 4000 }, workSide: 4096 });
  assert.equal(huge.sw, 2048);
  assert.equal(huge.sh, 2048);
});

test("рабочая сторона больше фото — не увеличиваем", () => {
  const g = run({ source: { width: 640, height: 512 }, workSide: 2048 });
  assert.equal(g.sw, 640);
  assert.equal(g.sh, 512);
});
