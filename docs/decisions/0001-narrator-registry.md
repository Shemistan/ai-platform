---
тип: adr
статус: принято
обновлено: 2026-10-17
---

# 0001. Реестр запрещённых организаций в narrator — файлом по пути из env

## Контекст

Упоминание организации из реестра (экстремистские, террористические,
нежелательные) требует пометки в посте и в кадре (КоАП ст. 13.15). Реестр и
`with_footnotes` жили только в платформе (`app/platform/aiplatform/updates/registry.py`),
а основной канал — дайджест и озвучки narrator (`make news-publish`,
`./narrate bot send`, skill `telegram-send`) — уходил без пометок (CMP-002).

narrator — отдельный пакет со своим uv-окружением и не импортирует платформу:
его запускают и без неё, из терминала и агентами.

## Решение

- narrator получает реестр **файлом**, не кодом: путь — `$NARRATOR_REGISTRY`,
  формат тот же, что у платформы (`{"checked", "entries": [{id, name, status,
  aliases, disclaimer, …}]}`).
- Платформа передаёт свой `data/registry.json` (его правят на вкладке
  «Обновления») во все запуски narrator: `aiplatform.core.narrator.load`
  кладёт `NARRATOR_REGISTRY` в окружение движка. Своё значение
  `NARRATOR_REGISTRY` в окружении платформы главнее.
- Без переменной, без файла или с битым файлом narrator берёт копию списка по
  умолчанию — `app/narrator/src/narrator/registry.json`. Что копия совпадает с
  `app/platform/aiplatform/updates/registry.json`, проверяет тест narrator
  `test_default_copy_matches_platform`: правят обе вместе.
- Поиск упоминаний и сноски — небольшая копия логики платформы в
  `narrator/registry.py` (`alias_pattern`, `notes`, `with_footnotes`,
  `fit_caption`): те же правила псевдонимов и тот же неудаляемый хвост подписи
  (CMP-001).

## Последствия

- Подписи `describe`, `digest_caption`, `send_folder`, `send_digest_video`,
  подпись аудио дайджеста и кадр видео дайджеста/папки получают пометки;
  `news check` не пропускает `post.md` с упоминанием без сноски.
- Логика поиска продублирована в двух пакетах: правка правил псевдонимов —
  в обоих местах (тесты на «Meta» есть с обеих сторон).
- Из терминала без платформы действует список по умолчанию; свежий реестр —
  `NARRATOR_REGISTRY=<data платформы>/registry.json` в окружении.
