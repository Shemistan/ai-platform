---
тип: plan
статус: в работе
обновлено: 2026-10-19
спека: 2026-10-19-montage-master-spec-design.md
---

# План: «Монтаж» по MASTER_SPEC (PRD-027)

**Цель:** сквозной конвейер запись → EDL → знания → биты → стиль → раскадровка →
сцены → таймлайн → финал → QA → экспорт, с ревизиями и реестром возможностей,
внутри `montage/`, на фейках в тестах. **Спека:** `../specs/2026-10-19-montage-master-spec-design.md`,
проекция ТЗ — ADR 0011.

**Ограничения:** stdlib Python 3.11+, без зависимостей; ванильный JS; файлы
(ADR 0007); LLM — `claude -p` (`### task: montage_*` в первой строке, фейк
отвечает по задаче); HyperFrames — единственный рендер сцен; `./check`
зелёный на каждой подзадаче; карта `docs/context.md` правится в том же
коммите, где появляется модуль.

Подзадачи — ветки от `staging`, каждая через `./check` → коммит → `./finish`.

## 1. Основание: схемы, TimeMap, журнал решений, возможности, режимы (ветка `worktree-master-spec`)

1. `contracts/montage/*.schema.json` (16 схем из спеки, п. 2) и
   `montage/schemas.py`: `validate(name: str, value) -> list[str]` (type,
   required, properties, items, enum, minimum, maximum, additionalProperties),
   `check(name, value)` → `ValueError` с первыми ошибками. Тест
   `tests/test_montage_schemas.py`: каждый файл читается, пример из схемы
   (`examples[0]`) валиден, пропущенное обязательное поле и неверный enum —
   ошибки.
2. `montage/timemap.py`: `TimeMap.from_edl(edl, source_id, fps=30)`,
   `.segments`, `.to_edit_frame(s)`, `.to_source(frame)`,
   `.words_in_edit(words)`, `.check() -> list[str]`, `.as_dict()`,
   `load(data)`. Целые кадры: `edit_frame_end` следующего = `edit_frame_start`
   предыдущего + `round(duration*fps)`; накопления float нет (кадры целые).
   Тест: обратимость, инварианты, слова после вырезки, границы. `docs/TIMEBASE.md`.
3. `montage/decisions.py`: `record(folder, role, action, *, model, prompt,
   output, confidence=None, rationale="", ids=())` → строка `decisions.jsonl`
   (`prompt_version` = sha256 промпта[:12], хэши входа/выхода), `read(folder)`.
   `source_manifest.py` — внутри `projects.py` (`_source_manifest`): sha256 +
   probe каждой записи. Тест: запись/чтение, хэши стабильны.
4. `montage/capabilities.py`: `Capability` (id, kind, label, status, version,
   detail, probed_at, hint), `registry(config) -> list[dict]`,
   `probe(config) -> list[dict]` (реальные `--version`/`-h` через subprocess с
   таймаутом 20 с), `route(config, task) -> tuple[str, str]` (движок,
   причина). Ручки `GET capabilities`, `POST capabilities/probe`. Тест: на
   фейке всё `available`, с `AIP_LLM_CMD=/nonexistent` — `unavailable` и
   `route("scene_render")` → `blocked`.
5. Режимы и ворота в `projects.py`: опция `mode` (`clean_options`; `auto`
   → `autonomous` при чтении старых проектов, миграция схемы 1 → 2),
   `approvals`, `approve(pid, gate)`, статусы `waiting_approval`/`blocked`/
   `interrupted` (`_load`), `render` требует `approvals.edl` (в `reviewed`),
   `manual` не зовёт LLM в `prepare`. Ручка `POST projects/{pid}/approve
   {gate}`. Тест: `reviewed` → render без approve → 409; `autonomous` →
   approvals с `auto: true`; `manual` → план пустой, без вызова LLM.
6. Документация: ADR 0011, `docs/ENVIRONMENT_AUDIT.md`, `docs/TIMEBASE.md`,
   карта, api-routes.

## 2. Знания, биты, стиль, раскадровка, шаблоны, сцены (ветка `master-spec/story`)

1. `montage/documents.py`: `save(folder, name, data) -> dict` (sniff: md/txt
   по тексту, docx — zip с `word/document.xml`, pdf — `%PDF`), `text_of(path,
   config)` (docx stdlib, pdf через `pdftotext` или `unavailable`),
   `chunk(text, size=800) -> list[{n, page?, heading?, text}]`. Ручки
   `POST projects/{pid}/documents` (тело — файл, имя в `X-File-Name`),
   `POST …/documents/{id}/delete`, `GET …/documents`. Тест: md и docx
   (собранный zipfile в тесте), pdf без pdftotext → status `unavailable`.
2. `montage/knowledge.py`: `prompt(packed_words, chunks)`, `clean(data,
   n_words, chunks) -> dict` (claims с citations в границах), задача
   `montage_knowledge` в `fake_llm` (claim на каждую 3-ю фразу, цитата из
   первого фрагмента). Тест: чистка, фейк.
3. `montage/narrative.py`: `prompt(packed_words, claims)`, `clean(data,
   n_words, claims)`, `with_frames(beats, timemap, out_words)`, `pacing(beats)`;
   фейк `montage_beats`. Тест: биты по порядку, без пересечений, кадры из TimeMap.
4. `montage/style.py`: `DEFAULT`, `prompt(topic)`, `clean(data)`,
   `revision(folder, style) -> str` (`style_001`), `css_vars(style)`; фейк
   `montage_style`. Тест: кривая палитра → дефолт, версия растёт.
5. `montage/templates.py`: `CATALOG` (12 + 2 алиаса): `Template(id, label,
   schema, limits, html(props, style), anim(props, d), min_font)`,
   `validate_props(tid, props) -> dict` (чистка по схеме и лимитам),
   `composition(tid, props, style, duration_frames, fps) -> str`,
   `example(tid) -> dict`. `motion.py` переходит на `templates` (старые 5 —
   через алиасы, `motion.composition` остаётся совместимой). Тест
   `test_montage_templates.py`: все 12 собираются из `example`, в HTML нет
   запрещённого (регэкспы `motion.agents.FORBIDDEN`), фейк-рендер даёт MOV
   длительности `frames/fps`, первый кадр извлекается ffmpeg (thumbnail).
6. `montage/storyboard.py`: `prompt(beats, claims, style, templates)`,
   `clean(data, beats, timemap) -> list[scene]` (score из necessity, порог
   `MIN_SCORE=12` из 25, `MIN_GAP_S=20`, factual_risk ≥ 4 без claim —
   отбрасывается, props через `templates.validate_props`), `scene_hash(scene,
   style_rev)`, `edit_scene(scene, patch)`, `invalidate(storyboard, reason)`;
   фейк `montage_storyboard` (по кругу три разных шаблона на биты).
   Тест: порог, интервал, factual_risk, хэш меняется от props и не меняется
   от rationale.
7. `montage/scenes.py`: `render_all(config, folder, storyboard, style,
   cancel, note, progress) -> list[scene]` — для каждой включённой сцены
   `scenes/<id>/<hash>.mov` (пропуск, если есть), `thumb.png` (ffmpeg
   `-frames:v 1`), проверка (`media.duration`, `ffprobe pix_fmt`, размер),
   статусы `rendered|blocked|failed`. Тест на фейке: второй прогон не
   перерисовывает (mtime), правка одной сцены — новый файл только у неё.
8. Действие `story` в `projects.py` (`_story`): timemap → chunks → knowledge
   → beats → style → storyboard → `waiting_approval(storyboard)`; этапы
   `timemap`, `knowledge`, `beats`, `style`, `storyboard` в `stages.py`;
   ручки `POST projects/{pid}/storyboard` (запуск), `GET …/storyboard`,
   `POST …/storyboard {scenes: [{id, enabled?, edit_start_frame?,
   duration_frames?, template_id?, props?}]}`, `POST …/storyboard/approve`,
   `POST …/scenes/{sid}/redo {text}`, `GET …/scenes/{sid}/thumb.png`,
   `GET …/scenes/{sid}/index.html`, `GET …/beats`, `GET …/knowledge`,
   `GET …/style`, `POST …/style` (правка → новая ревизия → инвалидация).
   Тест API на фейках: story → ≥3 сцены разных шаблонов → правка → approve.

## 3. Таймлайн, QA, экспорт, ревизии (ветка `master-spec/qa-export`)

1. `montage/timeline.py`: `build(edl, timemap, storyboard, captions,
   has_screen) -> dict`, `overlays(timeline, scenes_dir) -> list[render.Overlay]`,
   `check(timeline) -> list[str]` (пересечения в одной дорожке, клип вне
   результата). `_render` пишет `timeline.json` до финала, `_final` берёт
   наложения из него. `GET projects/{pid}/timeline`. VTT: `render.vtt(items)`.
2. `montage/qa.py`: `mechanical(config, folder, project, timeline, cancel)
   -> list[QAItem]` (blackdetect, silencedetect, astats peak, ffprobe
   размер/fps/дорожки, длительность, сцены, субтитры > 42, шрифт),
   `semantic(...)` оборачивает существующую проверку; `report(items,
   attempts, semantic_mode) -> dict`, `gates(report)`. `qa_report.json` после
   каждой сборки; `GET projects/{pid}/qa`. Тест: искусственный чёрный ролик
   → `black_frames` error; ролик с клиппингом (`sine` на +6 дБ) → warning;
   без LLM → `semantic: human_review`.
3. `montage/exports.py`: `PRESETS`, `argv(preset, src, out, ffmpeg)`,
   `provenance(project, folder, preset, override) -> dict`, `archive(folder,
   out)` (zip манифестов). Действие `export` в `projects.py` (`_export`);
   ручки `POST projects/{pid}/export {preset, override_reason?}` (409 при
   `mechanical: fail` без причины), `GET …/exports`, `GET …/exports/{n}/file`.
   Тест: экспорт на фейках → mp4 читается ffprobe, provenance с хэшами, 409 и
   override с причиной в `decisions.jsonl`.
4. `montage/revisions.py`: `snapshot(folder, label) -> int`, `list(folder)`,
   `restore(folder, n)`; вызов после каждого действия и ручных правок;
   ручки `GET …/revisions`, `POST …/revisions/{n}/restore`. Тест: правка →
   снимок → restore возвращает план; рестарт сервера (новый `MontageModule`
   на том же каталоге) — проект, таймлайн и ревизии на месте.
5. Инвалидация в `save_plan`/`save_style`: сброс зависимых (спека, п. 17).
   Тест: правка плана сбрасывает биты и раскадровку, кэш сцен цел.

## 4. UI (ветка `master-spec/ui`)

1. `static/montage/app.js`: режим и материалы в `setup`, кнопка
   «Раскадровка» в `review`, блоки `narrative(p)`, `storyboard(p)`,
   `timeline(p)`, `qa(p)`, `exports(p)`, `revisions(p)`; панель
   «Возможности» модалкой из шапки. Новые модули: `storyboard.js` (чистые
   функции: карточка, патч сцены), `timeline.js` (полосы по кадрам),
   `qa.js` (группировка по severity). Тесты `tests/js/montage-storyboard.test.mjs`,
   `montage-timeline.test.mjs`.
2. `style.css`: сетка карточек, дорожки, QA.

## 5. Приёмка (ветка `master-spec/acceptance`)

1. `tests/test_montage_e2e.py`: полный путь на фейках (спека, п. 20) и путь
   без LLM/HyperFrames; рестарт.
2. `docs/ACCEPTANCE.md` — матрица требований ТЗ; `docs/user-guide.md`,
   `docs/api.md`, `docs/configuration.md`, `docs/context.md`, этот план →
   `завершено`, спека → `реализовано`.
