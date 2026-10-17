"""Обвязка тестов: единое ожидание poll и его масштаб AIP_TEST_TIMEOUT_SCALE (QA-017)."""

import unittest
from unittest import mock

from tests import helpers


class PollTest(unittest.TestCase):
    def test_returns_first_value_that_is_done(self):
        values = iter([0, 1, 2, 3])
        self.assertEqual(helpers.poll(lambda: next(values), lambda v: v >= 2, timeout=5, interval=0), 2)

    def test_timeout_reports_last_value(self):
        with self.assertRaisesRegex(AssertionError, "не дождались задачи: 7"):
            helpers.poll(lambda: 7, lambda _v: False, timeout=0.05, interval=0.01, what="задачи")

    def test_timeout_is_scaled(self):
        with mock.patch.object(helpers, "TIMEOUT_SCALE", 3.0):
            self.assertEqual(helpers.scaled(10), 30)
        self.assertEqual(helpers.scaled(10), 10 * helpers.TIMEOUT_SCALE)


if __name__ == "__main__":
    unittest.main()
