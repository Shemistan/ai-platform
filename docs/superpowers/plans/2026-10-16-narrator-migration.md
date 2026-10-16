---
тип: plan
статус: в работе
обновлено: 2026-10-16
спека: 2026-10-16-narrator-migration-design.md
---

# План: перенос narrator в платформу

Каждая подзадача — своя ветка от `staging`, `./check` зелёный, `./finish`.

1. **Движок** (`worktree-narrator-migration`): `app/narrator/` — код без
   старого UI, Remotion и Slidev; `library_root()` и пути токена/каналов;
   `CLAUDE.md` с регламентом агентов; `tools/import-old.sh`; `make check`;
   платформа по умолчанию зовёт `app/narrator`.
2. **Мост** (`narrator-migration/bridge`): `narrate bridge …` с JSON,
   `say --check-json`; фейковый движок платформы отвечает на те же команды;
   `core/narrator.py` в платформе — вызов моста.
3. **Аудио** (`narrator-migration/audio`): произношение, шаги, проверка,
   из библиотеки, из файлов, «текст в редактор», образцы голосов.
4. **Шапка и Каналы** (`narrator-migration/channels`): новые группы шапки,
   вкладка «Каналы»: каналы, бот, подписчики, приём подписок.
5. **Статьи** (`narrator-migration/library`): вкладка, задачи библиотеки.
6. **Новости** (`narrator-migration/news`): источники, главное за день,
   добор, обзоры; вердикт судьи в дайджесте.
7. **Пост в канал** (`narrator-migration/post`): сценарий `post`, вид поста
   «картинка и аудио».
8. **Обновления** (`narrator-migration/models`): модели озвучки.
9. **Сцены** (`narrator-migration/scenes`): глава, код, итог, полоса главы.
10. Финал: README платформы и корня, статус спеки и плана.
