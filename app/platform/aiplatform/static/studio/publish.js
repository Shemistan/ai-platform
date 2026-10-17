// Студия: шаг «Публикация» — выбор каналов, подтверждение и журнал отправок.
import { busyClick, confirmPublish, h } from "/ui.js";
import { API, state } from "./state.js";
import { channelAdder } from "./settings.js";
import { editor } from "./media.js";
import { act, alertBox } from "./app.js";

// Выбор каналов и «повторно» у кнопки «Опубликовать» живут между перерисовками страницы.
export function publishPick(project) {
  // Каналы не выбраны — сразу отмечены каналы по умолчанию сценария (PRD-005).
  const initial = project.settings.channels.length ? project.settings.channels : project.default_channels || [];
  if (!state.publish[project.id]) state.publish[project.id] = { channels: new Set(initial), force: false, ignoreVerdict: false, noDisclaimers: "" };
  return state.publish[project.id];
}

// Тело шага «Публикация»: подпись, каналы, флаги повторной публикации и журнал отправок.
export function publishBody() {
  const p = state.project;
  const a = p.artifacts;
  const pick = publishPick(p);
  const sentTo = new Set((p.published || []).filter((r) => r.sent).map((r) => r.channel));
  return [
    p.scenario === "post" ? null : editor("post", a.post, 5),
    h(
      "div",
      { class: "publish-box" },
      // Домены ссылок поста; не из исходника — красным: автопубликация их не пропустит (SEC-013).
      p.links?.domains?.length
        ? h(
            "p",
            { class: "post-links" },
            "Ссылки ведут на: ",
            ...p.links.domains.map((d) => h("span", { class: `chip ${p.links.foreign.includes(d) ? "tone-danger" : ""}`, title: p.links.foreign.includes(d) ? "Этого домена нет в исходном материале" : "" }, d)),
          )
        : null,
      h("strong", {}, "Куда опубликовать"),
      !p.settings.channels.length && !(p.default_channels || []).length
        ? h("p", { class: "hint" }, "Чтобы не выбирать каналы каждый раз, задай каналы по умолчанию: ", h("a", { href: "/settings/#defaults-panel" }, "Настройки → Выпуски по умолчанию"), ".")
        : null,
      h(
        "div",
        { class: "publish-channels" },
        ...state.config.channels.map((c) =>
          h(
            "label",
            { class: "channel" },
            h("input", { type: "checkbox", checked: pick.channels.has(c.id), onchange: (e) => (e.target.checked ? pick.channels.add(c.id) : pick.channels.delete(c.id)) }),
            c.label,
            h("span", { class: "muted" }, c.format === "vertical" ? "9:16" : "16:9"),
            sentTo.has(c.id) ? h("span", { class: "chip tone-success" }, "уже опубликовано") : null,
            c.problem ? h("span", { class: "chip tone-danger" }, c.problem) : null,
          ),
        ),
      ),
      h(
        "label",
        { class: "check" },
        h("input", { type: "checkbox", checked: pick.force, onchange: (e) => (pick.force = e.target.checked) }),
        h("span", {}, "Опубликовать повторно", h("span", { class: "hint" }, "Отправить и туда, куда выпуск уже ушёл — как FORCE=1 у make news-publish.")),
      ),
      p.review?.verdict === "не готово"
        ? h(
            "label",
            { class: "check" },
            h("input", { type: "checkbox", checked: pick.ignoreVerdict, onchange: (e) => (pick.ignoreVerdict = e.target.checked) }),
            h("span", {}, "Опубликовать несмотря на вердикт", h("span", { class: "hint" }, "Судья сказал «не готово» — как --force у make news-publish.")),
          )
        : null,
      // Пометки выключены, а в тексте есть упоминания из реестра — публикация только с причиной.
      p.settings.disclaimers === false && p.disclaimers?.length
        ? h(
            "label",
            { class: "check" },
            h("span", {}, `Без пометок о: ${p.disclaimers.map((d) => d.name).join(", ")}`, h("span", { class: "hint" }, "Пометки выключены. Причина обязательна — она сохранится в записи публикации.")),
            h("input", { type: "text", value: pick.noDisclaimers, placeholder: "Причина публикации без пометок", oninput: (e) => (pick.noDisclaimers = e.target.value) }),
          )
        : null,
      channelAdder("step-publish"),
      h("button", { type: "button", class: "btn btn--primary", disabled: p.running, onclick: busyClick(publishNow) }, "Опубликовать…"),
    ),
    p.published
      ? h(
          "ul",
          { class: "results" },
          ...p.published.map((r) => {
            // Журнал публикации (BE-001): ответа Telegram не было — решает человек, проверив канал.
            const unclear = r.parts ? Object.values(r.parts).some((x) => ["sending", "unknown"].includes(x.state)) : ["sending", "unknown"].includes(r.state) && !r.message_id;
            const mark = unclear ? "?" : r.skipped ? "↷" : r.state === "failed" ? "✗" : "✓";
            return h(
              "li",
              {},
              `${mark} ${r.label}${r.republish ? " (повторно)" : ""}: ${r.message} `,
              ...(r.files || []).filter((x) => x.endsWith(".mp4")).map((x) => h("a", { class: "btn--link", href: `${API}/projects/${p.id}/files/${x}?download=1` }, "скачать")),
              ...(unclear && !p.running
                ? [
                    h("button", { type: "button", onclick: busyClick(() => act("publish/resolve", { channel: r.channel, posted: true })) }, "Пост есть"),
                    h("button", { type: "button", onclick: busyClick(() => act("publish/resolve", { channel: r.channel, posted: false })) }, "Поста нет"),
                  ]
                : []),
            );
          }),
        )
      : null,
  ];
}

// Публикация необратима: сначала подтверждение — каналы по названиям, начало подписи, вердикт судьи.
export async function publishNow() {
  const project = state.project;
  const pick = publishPick(project);
  const chosen = state.config.channels.filter((c) => pick.channels.has(c.id));
  if (!chosen.length) return alertBox("Отметь хотя бы один канал в шаге «Публикация».");
  const unsaved = state.drafts.post !== undefined && state.drafts.post !== project.artifacts.post;
  const ok = await confirmPublish({
    channels: chosen,
    caption: project.artifacts.post || "",
    verdict: project.review?.verdict,
    review: project.review?.text,
    note: unsaved ? "Правка подписи не сохранена — уйдёт сохранённая подпись." : null,
  });
  if (!ok) return;
  await act("publish", { channels: [...pick.channels], force: pick.force, ignore_verdict: pick.ignoreVerdict, ignore_disclaimers: pick.noDisclaimers });
  pick.force = false;
}
