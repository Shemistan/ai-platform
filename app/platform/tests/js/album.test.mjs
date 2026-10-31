// Альбом пространства (PRD-033): адрес #filter=&item= ↔ состояние, фильтр по виду.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { filterItems, hashOf, parseHash } = await loadStatic("core/album-state.js");
const F = ["all", "photo", "video"];

test("адрес → состояние: пусто, фильтр, просмотр, чужой фильтр", () => {
  assert.deepEqual(parseHash("", F), { filter: "all", item: null });
  assert.deepEqual(parseHash("#filter=video&item=j42", F), { filter: "video", item: "j42" });
  assert.deepEqual(parseHash("#filter=nope", F), { filter: "all", item: null });
});

test("состояние → адрес: «всё» не пишется, туда-обратно без потерь", () => {
  assert.equal(hashOf({ filter: "all", item: null }, F), "");
  assert.equal(hashOf({ filter: "photo", item: "a b" }, F), "#filter=photo&item=a+b");
  assert.deepEqual(parseHash(hashOf({ filter: "photo", item: "a b" }, F), F), { filter: "photo", item: "a b" });
});

test("фильтр по виду", () => {
  const items = [{ id: 1, kind: "photo" }, { id: 2, kind: "video" }, { id: 3, kind: "photo" }];
  assert.equal(filterItems(items, "all", F).length, 3);
  assert.deepEqual(filterItems(items, "photo", F).map((i) => i.id), [1, 3]);
});
