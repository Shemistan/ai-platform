// Альбом пространства (PRD-033): сетка готового, фильтр, просмотр элемента и возврат на то же место.
// Пространство даёт источник и виды: album({ root, filters, load, card, view }).
//   filters — [{ id, label }], первый — «всё»; load() → [{ id, kind, … }] (новые сверху);
//   card(item) → узел плитки; view(item) → узел просмотра (можно Promise).
// Состояние — в адресе (core/album-state.js): «назад» браузера, Esc и кнопка «← Альбом» возвращают в сетку,
// прокрутка сетки восстанавливается.
import { filterItems, hashOf, parseHash } from "/core/album-state.js";
import { fill, h, icon } from "/ui.js";

export function album({ root, filters, load, card, view, empty = "Пока пусто." }) {
  const ids = filters.map((f) => f.id);
  const state = { items: [], scroll: 0, shown: null, viewing: false };
  const bar = h("div", { class: "segmented album-filter", role: "group", "aria-label": "Что показать" });
  const grid = h("div", { class: "album-grid" });
  const viewer = h("section", { class: "album-viewer", hidden: true, "aria-live": "polite" });
  fill(root, bar, grid, viewer);

  const current = () => parseHash(location.hash, ids);
  const go = (next) => {
    const hash = hashOf(next, ids);
    if (hash !== location.hash) location.hash = hash || "#";
  };

  function back() {
    // В просмотр пришли из сетки — шаг назад в истории; открыли ссылкой — просто убрать item из адреса.
    if (state.cameFromGrid) history.back();
    else go({ ...current(), item: null });
  }

  function renderBar(filter) {
    fill(bar, ...filters.map((f) =>
      h("button", { type: "button", "aria-pressed": String(f.id === filter), onclick: () => go({ filter: f.id, item: null }) }, f.label)));
  }

  function renderGrid(filter) {
    const items = filterItems(state.items, filter, ids);
    fill(grid, items.length
      ? items.map((item) => h("button", { type: "button", class: "album-tile", onclick: () => open(item) }, card(item)))
      : h("p", { class: "muted" }, empty));
  }

  function open(item) {
    state.scroll = window.scrollY;
    state.cameFromGrid = true;
    go({ ...current(), item: item.id });
  }

  async function renderViewer(id) {
    const item = state.items.find((x) => x.id === id);
    if (!item) return fill(viewer, h("p", { class: "muted" }, "Не нашёл — возможно, удалено."), backButton());
    if (state.shown === id) return;
    state.shown = id;
    fill(viewer, backButton(), h("p", { class: "muted" }, "Загружаю…"));
    const node = await view(item);
    if (state.shown === id) fill(viewer, backButton(), node);
  }

  function backButton() {
    return h("button", { type: "button", class: "btn btn--ghost btn--sm album-back", onclick: back }, icon("prev"), "Альбом");
  }

  function apply() {
    const { filter, item } = current();
    renderBar(filter);
    if (item) {
      state.viewing = true;
      grid.hidden = bar.hidden = true;
      viewer.hidden = false;
      renderViewer(item);
      viewer.scrollIntoView({ block: "start" });  // просмотр — на экран, входы пространства остаются выше
      return;
    }
    const wasViewing = state.viewing;
    state.viewing = false;
    state.shown = null;
    state.cameFromGrid = false;
    viewer.hidden = true;
    grid.hidden = bar.hidden = false;
    renderGrid(filter);
    if (wasViewing) window.scrollTo(0, state.scroll);
  }

  window.addEventListener("hashchange", apply);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.viewing && !document.querySelector("dialog[open]")) back();
  });

  return {
    // Обновить данные (опрос пространства): сетку перерисовать, открытый просмотр не трогать.
    async refresh() {
      state.items = await load();
      if (!state.viewing) renderGrid(current().filter);
      else if (!state.shown) apply();
      return state.items;
    },
    start() {
      return this.refresh().then(apply);
    },
  };
}
