"""Предложение включить ежедневный дайджест и уведомления (PRD-003)."""

import time

from aiplatform.core import settings as settings_module
from aiplatform.studio import schedule
from tests.helpers import StudioCase

DAY = 86400


class AutopilotOfferTest(StudioCase):
    @property
    def module(self):
        return self.app.modules["studio"]

    def digest(self, day: str) -> str:
        return self.create(scenario="digest", day=day)["id"]

    def offer(self) -> bool:
        return self.json("GET", "/api/studio/board")[1]["offers"]["digest_autopilot"]

    def test_shown_after_two_manual_digests_in_three_days(self):
        self.digest("2026-10-01")
        self.assertFalse(self.offer(), "один дайджест — рано")
        self.digest("2026-10-02")
        self.assertTrue(self.offer())
        self.assertFalse(self.module.autopilot_offer(now=time.time() + 4 * DAY), "старше 3 дней — не в счёт")

    def test_schedule_digests_do_not_count(self):
        self.digest("2026-10-01")
        pid = self.digest("2026-10-02")
        self.module.studio.annotate(pid, origin=schedule.ORIGIN)
        self.assertFalse(self.offer())

    def test_enable_hides_banner(self):
        self.digest("2026-10-01")
        self.digest("2026-10-02")
        self.assertTrue(self.offer())
        status, body = self.json("POST", "/api/settings/section/schedule", {"enabled": True, "time": "07:00"})
        self.assertEqual(status, 200, body)
        status, body = self.json("POST", "/api/settings/section/notify", {"enabled": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["schedule"]["enabled"])
        self.assertTrue(body["notify"]["enabled"])
        self.assertFalse(self.offer())

    def test_dismiss_survives_restart(self):
        self.digest("2026-10-01")
        self.digest("2026-10-02")
        status, _ = self.json("POST", "/api/settings/section/offers", {"digest_autopilot": False})
        self.assertEqual(status, 200)
        self.assertFalse(self.offer())
        # Флаг — в settings.json: новый объект настроек (как после перезапуска) читает его с диска.
        fresh = settings_module.Settings(self.app.ctx.config.settings.path)
        self.assertFalse(fresh.load()["offers"]["digest_autopilot"])
