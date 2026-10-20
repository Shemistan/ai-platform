# Схемы данных «Монтажа» (MASTER_SPEC §7)

JSON Schema (подмножество: type, required, properties, items, enum, min/max,
pattern) для файлов конвейера `montage/projects/<id>/`. Проверяет
`aiplatform/montage/schemas.py` (`validate`, `check`), тесты —
`app/platform/tests/test_montage_schemas.py`. У каждой схемы есть
`examples[0]` — он же пример формата для UI и фейков.

| Схема | Файл проекта |
|---|---|
| `project` | `project.json` |
| `source_media`, `source_document` | `source_manifest.json`, `documents.json` |
| `transcript_word` | `words.json` → `words[]` |
| `edit_decision` | `edl.json` → `decisions[]` |
| `time_map_segment` | `timemap.json` → `segments[]` |
| `knowledge_claim` | `knowledge.json` → `claims[]` |
| `narrative_beat` | `beats.json` → `beats[]` |
| `style_bible` | `style_bible.json`, `styles/<rev>.json` |
| `storyboard_scene`, `motion_spec` | `storyboard.json` → `scenes[]` |
| `timeline` | `timeline.json` |
| `qa_item` | `qa_report.json` → `items[]` |
| `export_preset` | `exports/<n>/provenance.json` → `preset` |
| `capability` | `GET /api/montage/capabilities` → `items[]` |
| `revision` | `revisions/index.json` |
| `review_policy`, `review_run`, `review_round` | `review/runs/<id>/run.json` → `policy`, сам запуск, `rounds/<n>/round.json` |
| `reviewer_report`, `review_issue` | `rounds/<n>/reports/<role>.json`, `rounds/<n>/issues.json` → `issues[]` |
| `repair_plan`, `repair_action` | `rounds/<n>/repair_plan.json` (→ `actions[]`) |
| `review_comparison`, `review_version` | `rounds/<n>/comparison.json`, `review/versions/V<n>/version.json` |
| `run_event` | `events.jsonl` (события конвейера, курсор `seq`) |

Шаблоны сцен: `templates/<id>.schema.json` — props каждого шаблона
(`montage/templates.py`).
