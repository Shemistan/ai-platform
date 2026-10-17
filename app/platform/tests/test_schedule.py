"""Дайджест по расписанию, уведомления о выпусках и разделы настроек."""

from datetime import date, datetime, time, timedelta
from unittest import mock

from tests.test_studio_flow import ARTICLE, NEEDS_FFMPEG, StudioCase, png_bytes


@NEEDS_FFMPEG
class ScheduleTest(StudioCase):
    extra_env = {"AIP_RENDER_SCALE": "0.25", "AIP_SCHEDULE_TICK_S": "0"}  # часы двигает тест

    def section(self, name, body):
        return self.json("POST", f"/api/settings/section/{name}", body)

    @property
    def studio(self):
        return self.app.modules["studio"]

    def test_section_validation(self):
        status, body = self.section("schedule", {"enabled": True, "time": "06:30", "day": "today"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["schedule"], {"enabled": True, "time": "06:30", "day": "today"})
        for bad in ({"time": "25:00"}, {"day": "завтра"}, {"enabled": "да"}, {"nope": 1}, {}):
            status, body = self.section("schedule", bad)
            self.assertEqual(status, 400, (bad, body))
        self.assertEqual(self.json("GET", "/api/settings/state")[1]["schedule"]["time"], "06:30")
        status, _ = self.json("POST", "/api/settings/section/other", {"a": 1})
        self.assertEqual(status, 404)

    def test_digest_runs_once_a_day_after_its_time(self):
        self.section("stats", {"auto": False})  # ежедневный отклик — отдельный тест
        self.section("schedule", {"enabled": True, "time": "07:00"})
        today = date.today()
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

    def test_digest_failure_is_reported_and_retried(self):
        self.section("stats", {"auto": False})
        self.section("schedule", {"enabled": True, "time": "07:00"})
        today = date.today()
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
        today = date.today()
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
        self.studio.scheduler._mark("digest", date.today().isoformat())
        self.assertEqual(self.studio.scheduler.tick(datetime.combine(date.today(), time(9, 0))), [])

    def test_daily_job_failure_is_visible(self):
        self.section("stats", {"auto": False})

        def broken():
            raise RuntimeError("диск отвалился")

        self.studio.scheduler.daily["boom"] = broken
        with self.assertLogs("aiplatform.schedule", "ERROR") as logs:
            done = self.studio.scheduler.tick(datetime.combine(date.today(), time(8, 0)))
        self.assertIn("boom: сбой — RuntimeError: диск отвалился", done)
        self.assertIn("Traceback", "\n".join(logs.output))
        note = self.json("GET", "/api/settings/state")[1]["notifications"][0]
        self.assertEqual(note["title"], "Ежедневная работа «boom» упала")

    def test_disabled_schedule_does_nothing(self):
        self.section("stats", {"auto": False})
        self.assertEqual(self.studio.scheduler.tick(datetime.combine(date.today(), time(23, 0))), [])

    def test_daily_response_refresh_once_a_day(self):
        today = datetime.combine(date.today(), time(8, 0))
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
        self.call("POST", f"/api/studio/projects/{project['id']}/upload/poster", png_bytes())
        self.settle(project["id"])
        notes = self.json("GET", "/api/settings/state")[1]["notifications"]
        self.assertIn("Публикация", notes[0]["text"], notes)
        self.json("POST", f"/api/studio/projects/{project['id']}/publish", {"channels": ["download-16x9"]})
        self.settle(project["id"])
        notes = self.json("GET", "/api/settings/state")[1]["notifications"]
        self.assertEqual(notes[0]["title"], "Пакет готов — залей руками", notes)

        self.json("POST", "/api/settings/notify-test")
        self.assertEqual(
            self.json("GET", "/api/settings/state")[1]["notifications"][0]["title"], "Проверка уведомлений"
        )
