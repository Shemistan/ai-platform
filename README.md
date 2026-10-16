# ai-platform

Локальная AI-платформа: вкладки «Фото», «Видео», «Аудио», «Выпуски», «Статьи»,
«Новости», «Каналы» и сценарии «Дайджест новостей», «Видео по статье» и
«Пост в канал» в одном сервисе.

Сервис — `app/platform/`, запуск и настройка — в [его README](app/platform/README.md).
Движок озвучки, статей, новостей и Telegram-бота, который он вызывает, —
`app/narrator/` ([README](app/narrator/README.md)).

```sh
cd app/platform && make demo   # http://127.0.0.1:8780
```

Дизайн — `docs/superpowers/specs/2026-10-15-ai-platform-design.md`.
