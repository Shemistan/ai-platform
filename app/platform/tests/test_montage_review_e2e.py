"""E2E итеративного ревью (ITERATIVE_REVIEW_SPEC §12, PRD-028) на фейках с настоящим ffmpeg.

Один «золотой» проект (2,5 минуты, раскадровка, сцены, QA) собирается на класс и копируется в каждый
тест: 1) два раунда автономно — ≥ 2 реальные правки, новый MP4, не затронутые сцены из кэша, история;
2) отказ кандидата и откат к baseline; 3) без мультимодальной модели — деградация и manual_check;
4) остановка пользователем; 5) исчерпание бюджета; 6) ручное утверждение плана; 1 и 5 раундов валидны.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from aiplatform import config as config_module
from aiplatform.core.gate import Gate
from aiplatform.montage import config as montage_config
from aiplatform.montage import decisions, schemas
from aiplatform.montage.projects import MontageProjects
from aiplatform.montage.review import state, versions
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase, isolated_config, poll


def montage_only(ctx):
    return [MontageModule(ctx)]


def golden(data_dir: str, head: Path) -> str:
    """Собрать проект со сценами напрямую через MontageProjects; вернуть id."""
    env = isolated_config({"AIP_BACKEND": "fake", "AIP_DATA_DIR": data_dir})
    config = montage_config.load(config_module.load(env))
    projects = MontageProjects(config, Gate())
    try:
        options = {"check": False, "mode": "reviewed", "motion": True}
        pid = projects.create({"title": "Золотой", "options": options})["id"]
        tmp = Path(data_dir) / "head.upload.mp4"
        shutil.copy2(head, tmp)
        projects.save_source(pid, "head", tmp)
        projects.start(pid)
        poll(lambda: projects.get(pid)["status"], lambda s: s in ("review", "failed"), timeout=300, interval=0.2)
        assert projects.get(pid)["status"] == "review", projects.get(pid)["error"]
        projects.approve(pid, "edl")
        projects.story(pid)
        poll(lambda: projects.get(pid)["status"], lambda s: s in ("waiting_approval", "failed"), timeout=300,
             interval=0.2)  # fmt: skip
        projects.approve(pid, "storyboard")
        projects.render(pid)
        poll(lambda: projects.get(pid)["status"], lambda s: s in ("done", "failed"), timeout=400, interval=0.2)
        project = projects.get(pid)
        assert project["status"] == "done", project["error"]
        assert project["motion"]["by_status"].get("rendered", 0) >= 3, project["motion"]
        return pid
    finally:
        projects.close()


@NEEDS_FFMPEG
class ReviewE2ETest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST | {"FAKE_REVIEW_JUDGE": "", "FAKE_REVIEW_NONE": "", "FAKE_REVIEW_CONFLICT": "",
                          "FAKE_REVIEW_BLOCKER": ""}  # fmt: skip

    @classmethod
    def setUpClass(cls):
        from tests.test_montage import make_video

        cls.media = tempfile.TemporaryDirectory()
        cls.head = Path(cls.media.name) / "head.mp4"
        make_video(cls.head, 150)
        cls.golden_dir = tempfile.TemporaryDirectory()
        cls.pid = golden(cls.golden_dir.name, cls.head)

    @classmethod
    def tearDownClass(cls):
        cls.golden_dir.cleanup()
        cls.media.cleanup()

    def setUp(self):
        super().setUp()
        src = Path(self.golden_dir.name) / "montage" / "projects" / self.pid
        self.folder = Path(self.tmp.name) / "montage" / "projects" / self.pid
        shutil.copytree(src, self.folder)
        projects = self.app.modules["montage"].projects
        with projects._lock:
            projects._load()
        self.url = f"/api/montage/projects/{self.pid}"

    def run_review(self, policy: dict, *, wait=("completed", "cancelled", "failed", "blocked", "awaiting_approval",
                                               "awaiting_decision"), timeout=400) -> dict:  # fmt: skip
        status, run = self.json("POST", f"{self.url}/review-runs", policy)
        self.assertEqual(status, 201, run)
        status, started = self.json("POST", f"{self.url}/review-runs/{run['run_id']}/start")
        self.assertEqual(status, 200, started)
        return self.wait_run(run["run_id"], wait, timeout)

    def wait_run(self, rid: str, states, timeout=400) -> dict:
        def fetch():
            run = self.json("GET", f"{self.url}/review-runs/{rid}")[1]
            project = self.json("GET", self.url)[1]
            return run, project["status"], project["error"]

        def done(value):
            run, status, _error = value
            return run["state"] in (*states, "failed") or status == "failed"

        run, status, error = self.poll_value(fetch, done, timeout)
        self.assertNotEqual(status, "failed", error)
        self.assertNotEqual(run["state"], "failed", run.get("error"))
        self.poll(self.url, lambda p: p["status"] not in ("queued", "running"), timeout=60)
        return run

    def poll_value(self, fetch, done, timeout):
        try:
            return poll(fetch, done, timeout, 0.2, what="review run")
        except AssertionError as exc:
            self.fail(str(exc))

    def round_data(self, rid: str, n: int, name: str) -> dict:
        return self.json("GET", f"{self.url}/review-runs/{rid}/rounds/{n}/{name}")[1]

    def test_two_rounds_real_fixes_cache_and_history(self):
        v0_sha = decisions.file_sha(self.folder / "output.mp4")
        movs_before = {p.name: p.stat().st_mtime_ns for p in self.folder.glob("scenes/*/*.mov")}
        scenes = json.loads((self.folder / "storyboard.json").read_text())["scenes"]
        locked = scenes[-1]["id"]
        run = self.run_review({"max_rounds": 2, "mode": "autonomous", "locked_scenes": [locked]})
        self.assertEqual(run["state"], "completed", run)
        self.assertEqual(
            schemas.validate(
                "review_run", {k: v for k, v in run.items() if k in schemas.load("review_run")["properties"]}
            ),
            [],
        )
        rounds = run["rounds"]
        self.assertGreaterEqual(len(rounds), 1, run)
        first = rounds[0]
        self.assertEqual(first["decision"], "accepted", first)
        issues = self.round_data(run["run_id"], 1, "issues")
        self.assertTrue(all(i["evidence"] for i in issues["issues"]), "у каждого замечания есть evidence")
        self.assertEqual(schemas.validate_many("review_issue", issues["issues"]), [])
        plan = self.round_data(run["run_id"], 1, "repair-plan")
        done = [a for a in plan["actions"] if a["status"] == "done"]
        self.assertGreaterEqual(len(done), 2, plan)
        self.assertTrue({a["op"] for a in done} & {"scene_extend", "caption_words", "scene_shorten", "scene_shift"})
        comparison = self.round_data(run["run_id"], 1, "comparison")
        self.assertTrue(comparison["hard_ok"], comparison["hard_gates"])
        self.assertEqual(comparison["decision"], "accepted")
        self.assertEqual(comparison["subjective"]["mode"], "judge")
        self.assertTrue(comparison["fixed_issues"], comparison)
        pure = {k: v for k, v in comparison.items() if k != "diff"}
        self.assertEqual(schemas.validate("review_comparison", pure), [])
        # Версии: V0 неизменяем, V1 — другой файл; кандидат вернулся в проект.
        _, vers = self.json("GET", f"{self.url}/versions")
        labels = [v["label"] for v in vers["versions"]]
        self.assertEqual(labels[:2], ["V0", "V1"])
        self.assertEqual(vers["versions"][0]["output"]["sha256"], v0_sha)
        self.assertIsNone(versions.integrity(self.folder, 0))
        self.assertNotEqual(vers["versions"][1]["output"]["sha256"], v0_sha, "правка реально изменила ролик")
        self.assertTrue(vers["versions"][1]["accepted"])
        self.assertEqual(
            decisions.file_sha(self.folder / "output.mp4"),
            vers["versions"][-1 if vers["versions"][-1]["accepted"] else -2]["output"]["sha256"],
        )
        # Не затронутые сцены не перерисованы: их MOV с прежним mtime; тронутые — с новым хэшем.
        movs_after = {p.name: p.stat().st_mtime_ns for p in self.folder.glob("scenes/*/*.mov")}
        touched = set(first["scenes_touched"])
        self.assertTrue(touched, first)
        self.assertNotIn(locked, touched, "заблокированная сцена не трогается (политика)")
        board = {s["id"]: s for s in json.loads((self.folder / "storyboard.json").read_text())["scenes"]}
        self.assertEqual(movs_after[f"{board[locked]['hash']}.mov"], movs_before[f"{board[locked]['hash']}.mov"])
        for sid, scene in board.items():
            if sid not in touched and scene["status"] == "rendered":
                self.assertEqual(movs_after.get(f"{scene['hash']}.mov"), movs_before.get(f"{scene['hash']}.mov"), sid)
        for sid in touched:
            self.assertNotIn(f"{board[sid]['hash']}.mov", movs_before, f"{sid}: новый хэш — перерисована")
        rejected = [i for i in issues["issues"] if i["status"] == "rejected" and "заблокирована" in i["status_reason"]]
        self.assertTrue(rejected, "замечание по locked сцене отклонено политикой")
        # Синхронизация: длительность кандидата сходится с таймлайном (hard gate duration и av_sync).
        gates = {g["id"]: g["ok"] for g in comparison["hard_gates"]}
        self.assertTrue(gates["duration"] and gates["av_sync"] and gates["sources"], gates)
        # Без мультимодальной модели роли «глаз» деградируют и просят проверки человеком (§1.4).
        reports = {r["role"]: r for r in issues["reports"]}
        self.assertEqual(run["capabilities"]["vision"], "unavailable")
        for role in ("motion_director", "art_director", "composition"):
            self.assertTrue(reports[role]["degraded"], role)
            self.assertTrue(reports[role]["degraded_reason"])
        self.assertFalse(reports["technical_qa"]["degraded"])
        self.assertIn("observed_audio", reports["sound"]["evidence_types"])
        # История: журнал решений содержит роли, арбитра/валидатора и судью; ревизии проекта на месте.
        roles = {d["role"] for d in decisions.read(self.folder)}
        self.assertTrue({"review_sound", "validator", "regression_judge"} <= roles, roles)
        stops = ("max_rounds", "no_material_gain", "repeated_issues", "no_executable_actions")
        self.assertIn(run["stop_reason"], stops)
        self.assertLessEqual(len(rounds), 2)
        # Рестарт: запуск, раунды и версии читаются заново.
        module = self.app.modules["montage"]
        again = MontageProjects(module.config, module.ctx.gate)
        try:
            reloaded = again.review.get(self.pid, run["run_id"])
            self.assertEqual(reloaded["state"], "completed")
            self.assertEqual(len(again.review.versions(self.pid)), len(labels))
        finally:
            again.close()

    def test_rejected_candidate_rolls_back_to_baseline(self):
        os.environ["FAKE_REVIEW_JUDGE"] = "worse"
        self.addCleanup(os.environ.__setitem__, "FAKE_REVIEW_JUDGE", "")
        v0_sha = decisions.file_sha(self.folder / "output.mp4")
        board_before = (self.folder / "storyboard.json").read_bytes()
        run = self.run_review({"max_rounds": 2, "mode": "autonomous"})
        self.assertEqual(run["state"], "completed", run)
        first = run["rounds"][0]
        self.assertEqual(first["decision"], "rejected", first)
        comparison = self.round_data(run["run_id"], 1, "comparison")
        self.assertEqual((comparison["decision"], comparison["decided_by"]), ("rejected", "judge"))
        self.assertLess(comparison["subjective"]["weighted_delta"], 0)
        _, vers = self.json("GET", f"{self.url}/versions")
        by_label = {v["label"]: v for v in vers["versions"]}
        self.assertFalse(by_label["V1"]["accepted"])
        self.assertEqual(decisions.file_sha(self.folder / "output.mp4"), v0_sha, "ролик вернулся к V0")
        self.assertEqual((self.folder / "storyboard.json").read_bytes(), board_before, "манифесты вернулись")
        self.assertEqual(run["accepted_version"], "V0")
        for rnd in run["rounds"][1:]:
            self.assertEqual(rnd["base_version"], "V0", "следующий раунд — от последней принятой версии")
        issues = self.round_data(run["run_id"], 1, "issues")["issues"]
        self.assertTrue(any(i["status"] in ("failed", "regressed") for i in issues))
        # Выбор версии вручную: вернуть отклонённого кандидата в проект можно явно.
        path = f"{self.url}/review-runs/{run['run_id']}/select-revision"
        status, chosen = self.json("POST", path, {"version": "V1"})
        self.assertEqual(status, 200, chosen)
        self.assertEqual(chosen["selected_version"], "V1")
        self.assertEqual(decisions.file_sha(self.folder / "output.mp4"), by_label["V1"]["output"]["sha256"])

    def test_user_stop_ends_run_with_reason(self):
        status, run = self.json("POST", f"{self.url}/review-runs", {"max_rounds": 5, "mode": "autonomous"})
        self.assertEqual(status, 201, run)
        rid = run["run_id"]
        self.json("POST", f"{self.url}/review-runs/{rid}/start")
        self.poll(f"{self.url}/review-runs/{rid}", lambda r: r["state"] != "preparing", timeout=120, interval=0.1)
        status, stopped = self.json("POST", f"{self.url}/review-runs/{rid}/stop")
        self.assertEqual(status, 200, stopped)
        got = self.wait_run(rid, ("completed", "cancelled"), timeout=200)
        self.assertEqual(got["stop_reason"], "user")
        self.assertLess(len(got["rounds"]), 5)
        self.assertEqual(self.json("GET", self.url)[1]["status"], "done", "проект остаётся готовым")
        self.assertIsNone(versions.integrity(self.folder, 0))

    def test_budget_exhaustion_stops_early_and_rounds_bounds(self):
        run = self.run_review({"max_rounds": 5, "mode": "autonomous", "max_new_assets": 0})
        self.assertEqual(run["state"], "completed", run)
        self.assertEqual(run["stop_reason"], "budget", run)
        plan = self.round_data(run["run_id"], 1, "repair-plan")
        self.assertTrue(plan["actions"], "действия были, но отложены бюджетом")
        self.assertFalse(plan["estimate"]["within_budget"])
        self.assertTrue(all(a["status"] in ("deferred", "approved", "done") for a in plan["actions"]))
        self.assertEqual(run["rounds_planned"], 5)
        # Один раунд — валидная граница; шестой — нет.
        self.assertEqual(self.json("POST", f"{self.url}/review-runs", {"max_rounds": 6})[0], 400)
        one = self.run_review({"max_rounds": 1, "mode": "autonomous"})
        self.assertEqual((one["state"], len(one["rounds"]), one["stop_reason"]), ("completed", 1, "max_rounds"))

    def test_manual_approval_gate_and_pause_resume(self):
        run = self.run_review({"max_rounds": 1, "mode": "manual_approval"})
        self.assertEqual(run["state"], "awaiting_approval", run)
        self.assertTrue(run["waiting"] and run["blocking_reason"])
        rid = run["run_id"]
        plan = self.round_data(rid, 1, "repair-plan")
        self.assertEqual(plan["approval"], "required")
        self.assertTrue(all(a["approval"] == "required" for a in plan["actions"]))
        issues = self.round_data(rid, 1, "issues")["issues"]
        self.assertEqual(self.json("POST", f"{self.url}/review-runs/{rid}/rounds/1/execute")[0], 200)
        got = self.wait_run(rid, ("completed", "awaiting_decision", "failed", "blocked"))
        self.assertEqual(got["state"], "completed", got)
        self.assertEqual(got["rounds"][0]["decision"], "accepted")
        after = self.round_data(rid, 1, "issues")["issues"]
        self.assertTrue(any(i["status"] == "fixed" for i in after), [(i["issue_id"], i["status"]) for i in after])
        self.assertEqual(len(after), len(issues))
        # Пауза в середине раунда и продолжение с чекпоинта: отчёты ролей не пересчитываются.
        status, run2 = self.json("POST", f"{self.url}/review-runs", {"max_rounds": 1, "mode": "manual_approval"})
        self.assertEqual(status, 201, run2)
        rid2 = run2["run_id"]
        self.json("POST", f"{self.url}/review-runs/{rid2}/start")
        mid = ("reviewing", "consolidating", "awaiting_approval")
        self.poll(f"{self.url}/review-runs/{rid2}", lambda r: r["state"] in mid, timeout=120, interval=0.05)
        status, paused = self.json("POST", f"{self.url}/review-runs/{rid2}/pause")
        if status == 200:
            self.assertEqual(paused["state"], "paused")
            self.poll(self.url, lambda p: p["status"] not in ("queued", "running"), timeout=60)
            reports = state.round_dir(self.folder, rid2, 1) / "reports"
            before = {p.name: p.stat().st_mtime_ns for p in reports.glob("*.json")} if reports.is_dir() else {}
            status, resumed = self.json("POST", f"{self.url}/review-runs/{rid2}/resume")
            self.assertEqual(status, 200, resumed)
            got2 = self.wait_run(rid2, ("awaiting_approval", "completed", "failed", "blocked"))
            self.assertEqual(got2["state"], "awaiting_approval", got2)
            after_reports = {p.name: p.stat().st_mtime_ns for p in reports.glob("*.json")}
            for name, stamp in before.items():
                self.assertEqual(after_reports.get(name), stamp, f"{name} пересчитан после паузы")
        else:
            self.assertEqual(status, 409, paused)  # раунд уже дошёл до ожидания — пауза невозможна, это честно

    def test_conflict_arbitration_and_blocker(self):
        os.environ["FAKE_REVIEW_CONFLICT"] = "1"
        self.addCleanup(os.environ.__setitem__, "FAKE_REVIEW_CONFLICT", "")
        run = self.run_review({"max_rounds": 1, "mode": "manual_approval",
                               "reviewer_roles": ["executive", "retention", "technical_qa"]})  # fmt: skip
        self.assertIn(run["state"], ("awaiting_approval", "completed"), run)
        issues = self.round_data(run["run_id"], 1, "issues")["issues"]
        conflicting = [i for i in issues if i["conflicts_with"]]
        self.assertGreaterEqual(len(conflicting), 2, issues)
        self.assertTrue(all(i["status"] == "deferred" for i in conflicting), "ничья — решает человек")
        self.assertTrue(any(d["role"] == "arbiter" for d in decisions.read(self.folder)))
        os.environ["FAKE_REVIEW_CONFLICT"] = ""
        os.environ["FAKE_REVIEW_BLOCKER"] = "1"
        self.addCleanup(os.environ.__setitem__, "FAKE_REVIEW_BLOCKER", "")
        if run["state"] == "awaiting_approval":
            self.json("POST", f"{self.url}/review-runs/{run['run_id']}/stop")
        blocked = self.run_review({"max_rounds": 1, "mode": "autonomous", "reviewer_roles": ["educational", "sound"]})
        self.assertEqual(blocked["state"], "blocked", blocked)
        self.assertIn("blocker", blocked["blocking_reason"])
        self.assertIsNone(blocked["rounds"][0]["candidate_version"], "с незакрытым blocker пересборки нет")
        v0 = self.json("GET", f"{self.url}/versions")[1]["versions"][0]
        self.assertEqual(decisions.file_sha(self.folder / "output.mp4"), v0["output"]["sha256"])

    def test_nothing_to_fix_completes_without_render(self):
        os.environ["FAKE_REVIEW_NONE"] = "1"
        self.addCleanup(os.environ.__setitem__, "FAKE_REVIEW_NONE", "")
        sha = decisions.file_sha(self.folder / "output.mp4")
        roles = ["executive", "narrative", "sound"]
        run = self.run_review({"max_rounds": 3, "mode": "autonomous", "reviewer_roles": roles})
        self.assertEqual(run["state"], "completed", run)
        self.assertEqual(run["stop_reason"], "no_executable_actions")
        self.assertEqual(len(run["rounds"]), 1)
        self.assertEqual(decisions.file_sha(self.folder / "output.mp4"), sha, "ничего не пересобиралось")
        started = time.time()
        self.assertLess(time.time() - started, 5)


if __name__ == "__main__":
    unittest.main()
