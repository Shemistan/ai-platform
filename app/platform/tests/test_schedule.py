"""Дайджест по расписанию, уведомления о выпусках и разделы настроек."""

import json
from datetime import datetime, time, timedelta
from unittest import mock

from tests.fixtures import ARTICLE, NEEDS_FFMPEG, TODAY, png_bytes
from tests.helpers import StudioCase


@NEEDS_FFMPEG
class ScheduleTest(StudioCase):
    extra_env = {"AIP_RENDER_SCALE": "0.25", "AIP_SCHEDULE_TICK_S": "0"}  # часы двигает тест

    def setUp(self):
        super().setUp()
        # Ежедневная уборка включена по умолчанию (REL-013); здесь тики проверяют расписание без неё.
        self.section("cleanup", {"auto": False})

    def section(self, name, body):
        return self.json("POST", f"/api/settings/section/{name}", body)

    @property
    def studio(self):
        return self.app.modules["studio"]

    def test_section_validation(self):
        status, body = self.section("schedule", {"enabled": True, "time": "06:30", "day": "today"})
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body["schedule"],
            {"enabled": True, "time": "06:30", "day": "today", "auto_publish": False},
        )
        bad_values = (
            {"time": "25:00"},
            {"day": "завтра"},
            {"enabled": "да"},
            {"nope": 1},
            {},
            {"poster_mode": "local"},  # умолчания выпуска — в digest_defaults (PRD-034)
        )
        for bad in bad_values:
            status, body = self.section("schedule", bad)
            self.assertEqual(status, 400, (bad, body))
        self.assertEqual(self.json("GET", "/api/settings/state")[1]["schedule"]["time"], "06:30")
        status, _ = self.json("POST", "/api/settings/section/other", {"a": 1})
        self.assertEqual(status, 404)

    def test_digest_runs_once_a_day_after_its_time(self):
        self.section("stats", {"auto": False})  # ежедневный отклик — отдельный тест
        self.section("schedule", {"enabled": True, "time": "07:00"})
        today = TODAY
        yesterday = (today - timedelta(days=1)).isoformat()
        self.assertEqual(self.studio.scheduler.tick(datetime.combine(today, time(6, 59))), [])
        done = self.studio.scheduler.tick(datetime.combine(today, time(9, 30)))  # платформу включили позже
        self.assertEqual(len(done), 1)
        self.assertIn(f"дайджест за {yesterday}: запущен", done[0])
        projects = self.json("GET", "/api/studio/projects?scenario=digest")[1]["projects"]
        self.assertEqual(len(projects), 1)
        self.settle(projects[0]["id"])
        self.assertEqual(self.studio.scheduler.tick(datetime.combine(today, time(10, 0))), [])
        run = self.json("GET", "/api/settings/state")[1]["last_runs"]["digest"]
        self.assertEqual((run["day"], run["result"], run["attempts"]), (today.isoformat(), "ok", 1))

        # Вручную уже собранный день не пересоздаётся.
        self.studio.scheduler._mark("digest", "")
        again = self.studio.scheduler.tick(datetime.combine(today, time(10, 0)))
        self.assertEqual(again, [f"дайджест за {yesterday}: уже есть «{projects[0]['title']}»"])
        log = (self.folder("x").parents[2] / "notifications.log").read_text(encoding="utf-8")
        self.assertIn("Дайджест запущен по расписанию", log)

    def test_digest_runs_with_chosen_options(self):
        """«Настройки дайджеста» и автопубликация расписания становятся настройками выпуска (PRD-030, PRD-034)."""
        self.section("stats", {"auto": False})
        options = {
            "poster_mode": "local",
            "draw_images": False,
            "shorts": True,
            "review_text": True,
            "ai_badge": False,
            "branding": False,
            "slide_motion": False,
            "tempo": 1.5,
            "frame": "square",
        }
        status, body = self.section("digest_defaults", options)
        self.assertEqual(status, 200, body)
        status, body = self.section("schedule", {"enabled": True, "time": "07:00", "auto_publish": True})
        self.assertEqual(status, 200, body)
        create = self.studio.create
        calls = []

        def spy(body):
            calls.append(body)
            return create(body)

        with mock.patch.object(self.studio, "create", spy):
            done = self.studio.scheduler.tick(datetime.combine(TODAY, time(8, 0)))
        self.assertIn("запущен", done[0])
        self.assertEqual(calls[0]["settings"], {"auto_publish": True})
        project = self.json("GET", "/api/studio/projects?scenario=digest")[1]["projects"][0]
        self.settle(project["id"])
        settings = self.studio.studio.get(project["id"])["settings"]
        self.assertEqual({k: settings[k] for k in options}, options)
        self.assertTrue(settings["auto_publish"])
        self.assertTrue(settings["disclaimers"])  # остальное — по умолчанию сценария

    def test_digest_defaults_without_file_are_as_before(self):
        """Без настроек дайджест прежний: своя обложка, оформление канала, ×1,25, кадр по постеру."""
        base = self.studio.defaults() | self.studio.scenario_defaults("digest")
        want = {"poster_mode": "upload", "branding": True, "tempo": 1.25, "frame": "poster", "slide_s": 8,
                "draw_images": True, "clips": False, "shorts": False, "review_text": False, "ai_badge": True,
                "target_minutes": 10}  # fmt: skip
        self.assertEqual({k: base[k] for k in want}, want)
        self.assertEqual(base["image_model"], self.studio.default_image_model())
        self.assertEqual(base["engine"], self.studio.defaults()["engine"])

    def test_old_settings_file_moves_to_digest_defaults(self):
        """Схема 1: выбранное человеком в schedule.digest переезжает в умолчания и не теряется (PRD-034)."""
        store = self.app.ctx.config.settings
        store.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "schedule": {
                        "digest": {
                            "enabled": True,
                            "time": "06:00",
                            "poster_mode": "local",
                            "draw_images": False,
                            "clips": True,
                            "auto_publish": True,
                        }
                    },
                    "digest_defaults": {"poster_deadline": "08:30"},
                }
            ),  # fmt: skip
            encoding="utf-8",
        )
        data = store.load()
        plan = {"enabled": True, "time": "06:00", "day": "yesterday", "auto_publish": True}
        self.assertEqual(data["schedule"]["digest"], plan)
        got = data["digest_defaults"]
        self.assertEqual((got["poster_mode"], got["draw_images"], got["clips"]), ("local", False, True))
        self.assertEqual(got["poster_deadline"], "08:30")
        self.assertTrue(got["ai_badge"])
        self.section("stats", {"auto": False})  # запись — уже в новой схеме, значения те же
        saved = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema"], 2)
        self.assertNotIn("draw_images", saved["schedule"]["digest"])
        self.assertFalse(saved["digest_defaults"]["draw_images"])

    def test_digest_defaults_choices_match_studio(self):
        from aiplatform.core import settings as core
        from aiplatform.studio import service, slides, steps

        self.assertEqual(core.DIGEST_CHOICES["poster_mode"], service.POSTER_MODES)
        self.assertEqual(core.DIGEST_CHOICES["images_mode"], service.IMAGES_MODES)
        self.assertEqual(core.DIGEST_CHOICES["clips_mode"], service.CLIPS_MODES)
        self.assertEqual(core.DIGEST_CHOICES["voice_mode"], service.VOICE_MODES)
        self.assertEqual(core.DIGEST_CHOICES["upscale_images"], service.UPSCALE_CHOICES)
        self.assertEqual(core.DIGEST_CHOICES["tempo"], steps.TEMPOS)
        self.assertEqual(core.DIGEST_CHOICES["slide_s"], slides.SLIDE_SECONDS)
        self.assertEqual(set(core.DIGEST_CHOICES["reading"]), set(slides.READING))
        self.assertEqual(set(core.DIGEST_CHOICES["frame"]), set(steps.FRAMES))
        self.assertLessEqual(set(core.DIGEST_DEFAULTS), set(service.SETTING_STEPS))

    def test_digest_defaults_validation(self):
        bad_values = (
            {"poster_mode": "ai"},
            {"clips": "да"},
            {"tempo": True},
            {"tempo": 3},
            {"slide_s": 5},
            {"frame": "circle"},
            {"poster_deadline": "25:00"},
            {"image_model": "нет-такой"},
            {"video_model": "нет-такой"},
            {"engine": "нет-такого"},
            {"voice": "нет-такого"},
            {"auto_publish": True},  # только у расписания
            {"nope": 1},
        )
        for bad in bad_values:
            status, body = self.section("digest_defaults", bad)
            self.assertEqual(status, 400, (bad, body))
        self.assertEqual(self.json("GET", "/api/settings/state")[1]["digest_defaults"]["poster_mode"], "upload")

    def test_manual_digest_uses_settings_defaults(self):
        """Умолчания дайджеста одни — страница меняет и выпуск по расписанию, и форму студии (UX-034, PRD-034)."""
        self.section("digest_defaults", {"draw_images": False, "clips": False, "ai_badge": False, "shorts": True})
        self.section("schedule", {"auto_publish": True})
        form = self.json("GET", "/api/studio/config")[1]["scenario_defaults"]["digest"]
        self.assertEqual((form["draw_images"], form["ai_badge"], form["shorts"]), (False, False, True))
        self.assertNotIn("auto_publish", form)  # публиковать сам — только выпуск по расписанию
        # «День» в «Новостях» создаёт выпуск без настроек — он тоже берёт умолчания.
        status, project = self.json("POST", "/api/studio/projects", {"scenario": "digest", "day": "2026-10-01"})
        self.assertIn(status, (200, 201), project)
        settings = project["settings"]
        self.assertEqual((settings["draw_images"], settings["ai_badge"], settings["shorts"]), (False, False, True))
        self.assertFalse(settings["auto_publish"])

    def test_disabled_model_in_defaults_does_not_break_digest(self):
        """Модель из умолчаний потом выключили — выпуск берёт общее умолчание, а не падает с 400."""
        self.section("stats", {"auto": False})
        chosen = self.studio.image_models()[0]["id"]
        self.assertEqual(self.section("digest_defaults", {"image_model": chosen})[0], 200)
        self.json("POST", "/api/settings/toggle", {"kind": "photo", "id": chosen, "enabled": False})
        self.section("schedule", {"enabled": True, "time": "07:00"})
        done = self.studio.scheduler.tick(datetime.combine(TODAY, time(8, 0)))
        self.assertIn("запущен", done[0])
        project = self.json("GET", "/api/studio/projects?scenario=digest")[1]["projects"][0]
        self.settle(project["id"])
        self.assertNotEqual(self.studio.studio.get(project["id"])["settings"]["image_model"], chosen)

    def test_digest_settings_page_is_served_and_in_menu(self):
        tabs = self.json("GET", "/api/config")[1]["tabs"]
        tab = next(t for t in tabs if t["id"] == "digest-settings")
        self.assertEqual((tab["href"], tab["group"]), ("/settings/digest/", "newsroom"))

    def test_digest_failure_is_reported_and_retried(self):
        self.section("stats", {"auto": False})
        self.section("schedule", {"enabled": True, "time": "07:00"})
        today = TODAY
        create = self.studio.create
        calls = []

        def flaky(body):
            calls.append(body)
            if len(calls) == 1:
                raise OSError("No space left on device")
            return create(body)

        with mock.patch.object(self.studio, "create", flaky), self.assertLogs("aiplatform.schedule", "ERROR"):
            done = self.studio.scheduler.tick(datetime.combine(today, time(9, 0)))
            self.assertIn("OSError: No space left on device", done[0])
            run = self.studio.scheduler.digest_run()
            self.assertEqual((run["result"], run["attempts"]), ("failed", 1))
            self.assertIn("No space left", run["error"])
            note = self.json("GET", "/api/settings/state")[1]["notifications"][0]
            self.assertEqual(note["title"], "Дайджест по расписанию не запустился")
            self.assertIn("Попытка 1 из 3", note["text"])

            # До паузы не повторяем, после — повтор в тот же день удаётся.
            self.assertEqual(self.studio.scheduler.tick(datetime.combine(today, time(9, 10))), [])
            done = self.studio.scheduler.tick(datetime.combine(today, time(9, 15)))
        self.assertIn("запущен", done[0])
        run = self.studio.scheduler.digest_run()
        self.assertEqual((run["result"], run["attempts"], run["error"]), ("ok", 2, None))
        self.settle(self.json("GET", "/api/studio/projects?scenario=digest")[1]["projects"][0]["id"])

    def test_digest_gives_up_after_max_attempts(self):
        self.section("stats", {"auto": False})
        self.section("schedule", {"enabled": True, "time": "07:00"})
        today = TODAY
        boom = mock.Mock(side_effect=KeyError("schedule"))
        with mock.patch.object(self.studio, "create", boom), self.assertLogs("aiplatform.schedule", "ERROR"):
            for minute in (0, 15, 30, 45):
                self.studio.scheduler.tick(datetime.combine(today, time(9, minute)))
        self.assertEqual(boom.call_count, 3)
        self.assertEqual(self.studio.scheduler.digest_run()["attempts"], 3)
        note = self.json("GET", "/api/settings/state")[1]["notifications"][0]
        self.assertIn("больше сегодня не пробую", note["text"])

    def test_old_string_mark_counts_as_done(self):
        self.section("stats", {"auto": False})
        self.section("schedule", {"enabled": True, "time": "07:00"})
        self.studio.scheduler._mark("digest", TODAY.isoformat())
        self.assertEqual(self.studio.scheduler.tick(datetime.combine(TODAY, time(9, 0))), [])

    def test_daily_job_failure_is_visible(self):
        self.section("stats", {"auto": False})

        def broken():
            raise RuntimeError("диск отвалился")

        self.studio.scheduler.daily["boom"] = broken
        with self.assertLogs("aiplatform.schedule", "ERROR") as logs:
            done = self.studio.scheduler.tick(datetime.combine(TODAY, time(8, 0)))
        self.assertIn("boom: сбой — RuntimeError: диск отвалился", done)
        self.assertIn("Traceback", "\n".join(logs.output))
        note = self.json("GET", "/api/settings/state")[1]["notifications"][0]
        self.assertEqual(note["title"], "Ежедневная работа «boom» упала")

    def test_disabled_schedule_does_nothing(self):
        self.section("stats", {"auto": False})
        self.assertEqual(self.studio.scheduler.tick(datetime.combine(TODAY, time(23, 0))), [])

    def test_daily_response_refresh_once_a_day(self):
        today = datetime.combine(TODAY, time(8, 0))
        self.assertEqual(self.studio.scheduler.tick(today), ["отклик: обновлено выпусков 0"])
        self.assertEqual(self.studio.scheduler.tick(today), [])
        self.assertTrue(self.json("GET", "/api/studio/board")[1]["stats"]["at"])

    def test_notifications_on_waiting_and_published(self):
        self.section("notify", {"enabled": True})
        project = self.create(
            scenario="article",
            text=ARTICLE,
            settings={"poster_mode": "upload", "channels": ["download-16x9"]},
            run=True,
        )
        self.settle(project["id"])
        notes = self.json("GET", "/api/settings/state")[1]["notifications"]
        self.assertEqual(notes[0]["title"], "Выпуск ждёт тебя", notes)
        self.assertIn("Постер — первый кадр", notes[0]["text"])

        # Тот же шаг ждёт и после повторного запуска — повторно не беспокоим.
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        self.settle(project["id"])
        self.assertEqual(len(self.json("GET", "/api/settings/state")[1]["notifications"]), 1)

        # Постер загружен → выпуск доходит до публикации и ждёт её; выгрузили пакет — «Пакет готов»:
        # пакет для ручной загрузки ещё не публикация (AN-003).
        # Конвейер после загрузки стартует не мгновенно: settle() мог бы вернуться до старта — ждём уведомление.
        self.call("POST", f"/api/studio/projects/{project['id']}/upload/poster", png_bytes())
        self.poll("/api/settings/state", lambda st: "Публикация" in st["notifications"][0]["text"])
        self.json("POST", f"/api/studio/projects/{project['id']}/publish", {"channels": ["download-16x9"]})
        self.poll("/api/settings/state", lambda st: st["notifications"][0]["title"] == "Пакет готов — залей руками")
        self.settle(project["id"])

        self.json("POST", "/api/settings/notify-test")
        self.assertEqual(
            self.json("GET", "/api/settings/state")[1]["notifications"][0]["title"], "Проверка уведомлений"
        )
