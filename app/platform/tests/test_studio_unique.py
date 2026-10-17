"""Создание выпуска атомарно (BE-009): проверка дубля и создание под одним замком."""

import threading
import time
import unittest

from aiplatform.core.http import HttpError
from tests.fixtures import ARTICLE
from tests.helpers import StudioCase


class UniqueCreateTest(StudioCase):
    def slow_create(self):
        """studio.create медленнее: без замка оба потока успели бы пройти проверку дубля."""
        studio = self.app.modules["studio"].studio
        real = studio.create

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return real(*args, **kwargs)

        studio.create = slow
        self.addCleanup(setattr, studio, "create", real)

    def test_parallel_creates_give_one_project(self):
        self.slow_create()
        module = self.app.modules["studio"]
        results, errors = [], []

        def create():
            try:
                results.append(module.create({"scenario": "article", "text": ARTICLE}))
            except HttpError as exc:
                errors.append(exc.status)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 1)
        self.assertEqual(errors, [409])
        projects = self.json("GET", "/api/studio/projects")[1]["projects"]
        self.assertEqual(len(projects), 1)

        again = module.create({"scenario": "article", "text": ARTICLE, "force_new": True})
        self.assertNotEqual(again["id"], results[0]["id"], "force_new по-прежнему создаёт новый")

    def test_idempotency_key_returns_same_project(self):
        self.slow_create()
        body = {"scenario": "article", "text": ARTICLE, "force_new": True}
        headers = {"Idempotency-Key": "form-1", "Content-Type": "application/json"}
        answers = []

        def post():
            answers.append(self.json("POST", "/api/studio/projects", body, headers))

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual([a[0] for a in answers], [201, 201])
        self.assertEqual(answers[0][1]["id"], answers[1][1]["id"])
        status, other = self.json("POST", "/api/studio/projects", body, {"Idempotency-Key": "form-2"})
        self.assertEqual(status, 201)
        self.assertNotEqual(other["id"], answers[0][1]["id"])


if __name__ == "__main__":
    unittest.main()
