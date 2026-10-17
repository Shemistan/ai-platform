// exifOrientation из photo/exif.js: тег поворота JPEG при любом порядке байт, без тега — 1 (FE-011).
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { exifOrientation } = await loadStatic("photo/exif.js");

// JPEG: SOI, (APP0 JFIF), APP1 с EXIF и IFD0 из entries [[тег, значение]], затем SOS.
function jpeg({ little = false, entries = [[0x0112, 6]], jfif = true } = {}) {
  const u16 = (v) => (little ? [v & 0xff, v >> 8] : [v >> 8, v & 0xff]);
  const u32 = (v) => (little ? [v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, v >>> 24] : [v >>> 24, (v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff]);
  const tiff = [...(little ? [0x49, 0x49] : [0x4d, 0x4d]), ...u16(42), ...u32(8), ...u16(entries.length)];
  for (const [tag, value] of entries) tiff.push(...u16(tag), ...u16(3), ...u32(1), ...u16(value), 0, 0);
  tiff.push(0, 0, 0, 0);
  const app1 = [0x45, 0x78, 0x69, 0x66, 0, 0, ...tiff];
  const be16 = (v) => [v >> 8, v & 0xff];
  const app0 = jfif ? [0xff, 0xe0, ...be16(16), 0x4a, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0] : [];
  const bytes = [0xff, 0xd8, ...app0, 0xff, 0xe1, ...be16(app1.length + 2), ...app1, 0xff, 0xda, 0, 2];
  return new Uint8Array(bytes).buffer;
}

test("поворот из EXIF: big-endian (iPhone) и little-endian", () => {
  assert.equal(exifOrientation(jpeg()), 6);
  assert.equal(exifOrientation(jpeg({ little: true, entries: [[0x0110, 1], [0x0112, 8]] })), 8);
  assert.equal(exifOrientation(jpeg({ jfif: false, entries: [[0x0112, 3]] })), 3);
});

test("нет тега, мусор, PNG, обрезанный файл — 1", () => {
  assert.equal(exifOrientation(jpeg({ entries: [[0x010f, 2]] })), 1);
  assert.equal(exifOrientation(jpeg({ entries: [[0x0112, 42]] })), 1);
  assert.equal(exifOrientation(new Uint8Array([0x89, 0x50, 0x4e, 0x47]).buffer), 1);
  assert.equal(exifOrientation(new ArrayBuffer(0)), 1);
  assert.equal(exifOrientation(jpeg().slice(0, 30)), 1);
});
