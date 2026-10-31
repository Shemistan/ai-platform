// Альбом пространства (PRD-033): состояние в адресе — #filter=<f>&item=<id>. Без DOM — тесты в node.
// Так работает «назад» браузера, а просмотр можно открыть ссылкой.

export function parseHash(hash, filters = ["all"]) {
  const params = new URLSearchParams(String(hash || "").replace(/^#/, ""));
  const filter = params.get("filter");
  return { filter: filters.includes(filter) ? filter : filters[0], item: params.get("item") || null };
}

export function hashOf({ filter, item = null }, filters = ["all"]) {
  const params = new URLSearchParams();
  if (filter && filter !== filters[0]) params.set("filter", filter);
  if (item) params.set("item", item);
  const text = params.toString();
  return text ? `#${text}` : "";
}

// Фильтр «all» (первый) — всё; иначе — элементы своего вида (item.kind).
export function filterItems(items, filter, filters = ["all"]) {
  return filter === filters[0] ? items : items.filter((item) => item.kind === filter);
}
