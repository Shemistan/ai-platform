// Поля настроек выпуска: их показывают форма студии и «Настройки дайджеста» (PRD-034).
// cfg — ответ /api/studio/config: модели, движки, каналы.
import { h } from "/ui.js";

export function fieldsOf(values, prefix, cfg) {
  const id = (name) => `${prefix}-${name}`;
  const radios = (name, options) =>
    h(
      "div",
      { class: "radio-row" },
      ...options.map(([value, label, checked]) => h("label", {}, h("input", { type: "radio", name: id(name), value, checked }), label)),
    );
  const check = (name, label, hint, checked) =>
    h(
      "label",
      { class: "check" },
      h("input", { type: "checkbox", id: id(name), checked }),
      h("span", {}, label, hint ? h("span", { class: "hint" }, hint) : null),
    );
  const select = (name, hint, options) =>
    h(
      "label",
      { class: "field" },
      h("span", { class: "hint" }, hint),
      h("select", { id: id(name) }, ...options.map(([value, label, selected]) => h("option", { value, selected }, label))),
    );
  const models = (list, current) => list.map((m) => [m.id, m.label + (m.installed ? "" : " — не установлена"), m.id === current]);
  const modes = (name, local, upload) =>
    radios(name, [
      ["local", local, values[name] !== "upload"],
      ["upload", upload, values[name] === "upload"],
    ]);
  return {
    poster_mode: () => modes("poster_mode", "Сгенерировать локальной моделью", "Загружу сам по промпту"),
    draw_images: () => check("draw_images", "Картинки на слайдах", "Иначе фон слайдов — градиент.", values.draw_images),
    images_mode: () => modes("images_mode", "Сгенерировать локальной моделью", "Сделаю сам по промптам во внешней модели"),
    image_model: () => select("image_model", "Модель для постера и картинок", models(cfg.image_models, values.image_model)),
    upscale_images: () =>
      select(
        "upscale_images",
        "Увеличить сгенерированные картинки и постер (SeedVR2: 2K ~20 с, 4K ~1,5 мин на картинку)",
        [["none", "Не увеличивать"], ["2k", "До 2K"], ["4k", "До 4K"]].map(([v, label]) => [v, label, v === values.upscale_images]),
      ),
    clips: () =>
      check(
        "clips",
        "Добавить ролики в ключевые моменты",
        "LLM выберет до 3 самых динамичных слайдов — там вместо картинки будет ролик (крутится по кругу без своего звука). Остальные слайды — картинка с наездом при появлении.",
        values.clips,
      ),
    clips_mode: () => modes("clips_mode", "Сгенерировать локальной моделью", "Сделаю сам по промптам во внешней модели"),
    video_model: () => select("video_model", "Модель видео (ролик 5 с, 704×448)", models(cfg.video_models, values.video_model)),
    voice_mode: () =>
      radios("voice_mode", [
        ["tts", "Озвучить локальной моделью", values.voice_mode !== "upload"],
        ["upload", "Загружу свой mp3", values.voice_mode === "upload"],
      ]),
    engine: () => {
      const engine = cfg.engines.find((e) => e.id === values.engine) || cfg.engines[0];
      const voiceOptions = (e, current) => [
        h("option", { value: "" }, `по умолчанию (${e.default_voice})`),
        ...e.voices.map((v) => h("option", { value: v, selected: v === current }, v)),
      ];
      const voiceSelect = h("select", { id: id("voice") }, ...voiceOptions(engine, values.voice));
      const engineSelect = h(
        "select",
        { id: id("engine"), onchange: (event) => voiceSelect.replaceChildren(...voiceOptions(cfg.engines.find((x) => x.id === event.target.value), "")) },
        ...cfg.engines.map((e) => h("option", { value: e.id, selected: e.id === engine.id }, e.label)),
      );
      return h(
        "div",
        { class: "settings" },
        h("label", { class: "field" }, h("span", { class: "hint" }, "Движок"), engineSelect),
        h("label", { class: "field" }, h("span", { class: "hint" }, "Голос"), voiceSelect),
      );
    },
    shorts: () => check("shorts", "Сделать и вертикальное видео 9:16", "Короткий сценарий 45–60 с для YouTube Shorts, TikTok, Reels.", values.shorts),
    frame: () =>
      select(
        "frame",
        "Кадр видео (шортс всегда 9:16)",
        [
          ["poster", "Как постер — пропорции картинки обложки"],
          ["square", "Квадрат 1:1"],
          ["landscape", "Широкий 16:9"],
        ].map(([v, label]) => [v, label, v === (values.frame || "landscape")]),
      ),
    branding: () =>
      check(
        "branding",
        "Плашка «подпишись» и кнопка «плей»",
        "Первый кадр — обложка: постер притемнён, по центру плей, внизу крупная плашка; дальше плашка плавно уезжает в угол и остаётся до конца. Этот кадр — обложка видео в Telegram.",
        values.branding,
      ),
    ai_badge: () =>
      check(
        "ai_badge",
        "Плашка «ИИ» в углу видео",
        "Мелкая метка в правом нижнем углу: озвучка и картинки сделаны нейросетью. Строка об ИИ в посте и метка в файле видео ставятся всегда.",
        values.ai_badge,
      ),
    slide_motion: () =>
      check(
        "slide_motion",
        "Анимация появления слайдов",
        "Картинка плавно приближается в первые полторы секунды, пункты и схемы выезжают по очереди, дальше слайд стоит. Выключи — ничего не движется, элементы только проявляются.",
        values.slide_motion !== false,
      ),
    disclaimers: () =>
      check(
        "disclaimers",
        "Помечать запрещённые в РФ организации",
        h("span", {}, "Мелкий текст на слайдах, где упомянута, например, Meta, и строка в конце поста. Список — на вкладке ", h("a", { class: "btn--link", href: "/updates/" }, "«Обновления»"), "."),
        values.disclaimers,
      ),
    tempo: () =>
      select(
        "tempo",
        "Скорость в видео — ускоряет озвучку без смены высоты голоса, слайды подстраиваются под неё",
        [0.75, 1, 1.25, 1.5, 1.75, 2].map((t) => [t, `×${String(t).replace(".", ",")}`, t === Number(values.tempo || 1)]),
      ),
    slide_s: () =>
      select(
        "slide_s",
        "Сколько держится слайд — ориентир для плана: чем дольше, тем меньше слайдов и больше времени прочитать каждый",
        [4, 6, 8, 10, 12].map((t) => [t, `~${t} с`, t === Number(values.slide_s || 6)]),
      ),
    reading: () =>
      select(
        "reading",
        "Время на чтение — слайд с длинным текстом подержится дольше, но не больше секунды после начала следующей фразы: картинка не отстаёт от речи",
        [
          ["normal", "Обычно — 3 слова в секунду"],
          ["slow", "Медленно — 2 слова в секунду"],
          ["off", "Не ждать — слайд меняется вместе с фразой"],
        ].map(([v, label]) => [v, label, v === (values.reading || "normal")]),
      ),
    channels: () =>
      cfg.channels.length
        ? h(
            "div",
            {},
            ...cfg.channels.map((c) =>
              h(
                "label",
                { class: "channel" },
                h("input", { type: "checkbox", name: id("channels"), value: c.id, checked: values.channels.includes(c.id) }),
                c.label,
                h("span", { class: "muted" }, c.format === "vertical" ? "9:16" : "16:9"),
                c.problem ? h("span", { class: "chip tone-danger" }, c.problem) : null,
              ),
            ),
            // Удаление канала затрагивает все сценарии — только на вкладке «Каналы», с подтверждением.
            h("p", { class: "hint" }, "Убрать канал из списка — на вкладке ", h("a", { class: "btn--link", href: "/channels/" }, "«Каналы»"), "."),
          )
        : h("p", { class: "hint" }, "Каналов пока нет — добавь их на вкладке ", h("a", { class: "btn--link", href: "/channels/" }, "«Каналы»"), "."),
    // Необратимое: выпуск уйдёт в каналы без просмотра — тумблер предупреждающего цвета, а не обычный флажок.
    auto_publish: () =>
      h(
        "label",
        { class: "check auto-publish" },
        h("input", { type: "checkbox", role: "switch", id: id("auto_publish"), checked: values.auto_publish }),
        h("span", {}, "Публиковать сразу, без моей проверки", h("span", { class: "hint warn" }, "Опубликуется без просмотра: выпуск уйдёт в каналы, как только соберётся.")),
      ),
    post_format: () =>
      h(
        "div",
        { class: "field" },
        h("span", { class: "hint" }, "Вид поста в Telegram: видео одним постом или картинка с подписью и следом аудио. Пакеты для ручной загрузки — всегда видео."),
        radios("post_format", [
          ["video", "Видео", values.post_format !== "photo"],
          ["photo", "Картинка, затем аудио", values.post_format === "photo"],
        ]),
      ),
    // Дайджест: до скольких ждать свою обложку, потом нарисовать локально (PRD-002).
    poster_deadline: () =>
      h("label", { class: "field" }, h("span", { class: "hint" }, "Своя обложка не пришла к этому времени — нарисовать локально (пусто — ждать)"), h("input", { type: "time", id: id("poster_deadline"), value: values.poster_deadline || "" })),
    // «Видео по статье»: без источника выпуск не опубликуется — строка в конце поста и на последнем слайде.
    source_url: () =>
      h("label", { class: "field" }, h("span", { class: "hint" }, "Ссылка на оригинал статьи"), h("input", { type: "url", id: id("source_url"), value: values.source_url || "", placeholder: "https://…" })),
    source_author: () =>
      h("label", { class: "field" }, h("span", { class: "hint" }, "Автор оригинала"), h("input", { type: "text", id: id("source_author"), value: values.source_author || "", placeholder: "Имя автора или издание" })),
    // Право на переработку (CMP-021): без него серия частями — полный пересказ — в канал не уходит.
    source_license: () =>
      select(
        "source_license",
        "Право на переработку — без него серия частями не публикуется",
        [["none", "Нет права"], ["own", "Своя статья"], ["cc-by", "CC BY"], ["permission", "Разрешение автора"]].map(([v, label]) => [v, label, v === (values.source_license || "none")]),
      ),
    // Рекламная интеграция (CMP-019): без erid публикация не уйдёт; «Реклама. erid: …» — в конце подписи.
    ad: () => check("ad", "Реклама", "Выпуск — рекламная интеграция: в подпись добавится «Реклама. erid: …» (38-ФЗ ст. 18.1).", values.ad),
    erid: () =>
      h("label", { class: "field" }, h("span", { class: "hint" }, "erid — токен от оператора рекламных данных (ОРД)"), h("input", { type: "text", id: id("erid"), value: values.erid || "", placeholder: "2Vtzqx…" })),
    review_text: () => check("review_text", "Пауза после текста — проверю его перед генерацией", null, values.review_text),
    evening: () =>
      check(
        "evening",
        "Собирать с вечера",
        "Выпуск за сегодня: новости с момента сбора вчерашнего выпуска — охват те же сутки. В первый раз — с полуночи.",
        values.evening,
      ),
    // Статья через LLM по правилам озвучки: код — на экран, а не в озвучку (PRD-029).
    adapt_text: () =>
      check(
        "adapt_text",
        "Готовить текст к озвучке",
        "LLM перескажет статью по правилам озвучки: числа и латиница словами, без ссылок, код покажется на экране, а не прочитается.",
        values.adapt_text !== false,
      ),
    draft: () =>
      check(
        "draft",
        "Черновик: без картинок и роликов, кадр вполовину",
        "Быстро посмотреть структуру и ритм. «Сделать чистовик» дорисует картинки и соберёт видео в полном кадре.",
        values.draft,
      ),
  };
}

// Значения полей с этим префиксом; names — какие именно (engine читается вместе с голосом).
export function readFields(prefix, names) {
  const get = (name) => document.getElementById(`${prefix}-${name}`);
  const radio = (name) => document.querySelector(`input[name="${prefix}-${name}"]:checked`)?.value;
  const special = {
    poster_mode: () => radio("poster_mode"),
    images_mode: () => radio("images_mode"),
    clips_mode: () => radio("clips_mode"),
    voice_mode: () => radio("voice_mode"),
    post_format: () => radio("post_format"),
    engine: () => ({ engine: get("engine").value, voice: get("voice").value }),
    tempo: () => ({ tempo: Number(get("tempo").value) }),
    slide_s: () => ({ slide_s: Number(get("slide_s").value) }),
    channels: () => ({ channels: [...document.querySelectorAll(`input[name="${prefix}-channels"]:checked`)].map((i) => i.value) }),
  };
  const out = {};
  for (const name of names) {
    const read = special[name];
    if (read) {
      const value = read();
      Object.assign(out, typeof value === "object" ? value : { [name]: value });
    } else {
      const input = get(name);
      out[name] = input.type === "checkbox" ? input.checked : input.value;
    }
  }
  return out;
}
