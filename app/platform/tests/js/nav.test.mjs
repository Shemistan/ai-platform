// Шапка по группам (UX-028): меню групп, активная вкладка и её группа.
import assert from "node:assert/strict";
import { test } from "node:test";
import { loadStatic } from "./load.mjs";

const { currentTab, groupTabs } = await loadStatic("core/nav-groups.js");

const TABS = [
  { id: "photo", label: "Фото", href: "/photo/", group: "media", group_label: "Медиа" },
  { id: "montage", label: "Монтаж", href: "/montage/", group: "media", group_label: "Медиа" },
  { id: "board", label: "Выпуски", href: "/board/", group: "board", group_label: "Выпуски", badge: "attention" },
  { id: "digest", label: "Дайджест", href: "/studio/?scenario=digest", group: "studio", group_label: "Студия" },
  { id: "article", label: "Статья", href: "/studio/?scenario=article", group: "studio", group_label: "Студия", default: true },
];
const at = (url) => new URL(url, "http://127.0.0.1:8780");

test("активная вкладка — по пути и параметрам, без параметра — default", () => {
  assert.equal(currentTab(TABS, at("/montage/?id=abc")), "montage");
  assert.equal(currentTab(TABS, at("/studio/?scenario=digest")), "digest");
  assert.equal(currentTab(TABS, at("/studio/")), "article");
  assert.equal(currentTab(TABS, at("/nope/")), undefined);
});

test("группы по порядку, одиночная — ссылка, активная знает текущую вкладку", () => {
  const groups = groupTabs(TABS, "montage");
  assert.deepEqual(groups.map((g) => [g.id, g.label, g.tabs.length]), [["media", "Медиа", 2], ["board", "Выпуски", 1], ["studio", "Студия", 2]]);
  assert.equal(groups[0].active, true);
  assert.equal(groups[0].current.label, "Монтаж");
  assert.equal(groups[1].badge, "attention");
  assert.ok(groups.slice(1).every((g) => !g.active && g.current === null));
});

test("группа-иконка (UX-037): icon из group_icon вкладки, у остальных — null", () => {
  const tabs = [...TABS, { id: "settings", label: "Настройки", href: "/settings/", group: "service", group_label: "Сервис", group_icon: "settings", badge: "advice" }];
  const groups = groupTabs(tabs, "settings");
  assert.equal(groups.at(-1).icon, "settings");
  assert.equal(groups.at(-1).badge, "advice");
  assert.ok(groups.slice(0, -1).every((g) => g.icon === null));
});
