// Группы шапки (UX-028): вкладки из /api/config → пункты верхнего уровня. Группа из одной вкладки —
// ссылка, из нескольких — меню. Порядок групп — по первой вкладке группы (порядок модулей на сервере).

// Активна вкладка с тем же путём и теми же параметрами адреса; на пути без своего параметра
// (студия без ?scenario) — вкладка с default.
export function currentTab(tabs, here) {
  const params = new URLSearchParams(here.search);
  const same = tabs.filter((t) => new URL(t.href, here.href).pathname === here.pathname);
  const exact = same.find((t) => [...new URL(t.href, here.href).searchParams].every(([k, v]) => params.get(k) === v));
  return (exact || same.find((t) => t.default) || same[0])?.id;
}

// [{ id, label, tabs, active, current, badge }]: label — подпись группы, current — активная вкладка
// группы (её название показывается рядом с подписью), badge — счётчик первой вкладки со счётчиком.
export function groupTabs(tabs, currentId) {
  const groups = [];
  for (const tab of tabs) {
    const id = tab.group || tab.id;
    let group = groups.find((g) => g.id === id);
    if (!group) {
      group = { id, label: tab.group_label || tab.label, tabs: [], active: false, current: null, badge: null };
      groups.push(group);
    }
    group.tabs.push(tab);
    if (tab.id === currentId) Object.assign(group, { active: true, current: tab });
    if (tab.badge && !group.badge) group.badge = tab.badge;
  }
  return groups;
}
