"""Вкладка «Монтаж» (PRD-021): лист монтажа, промпты, команды ffmpeg, сдвиг экрана, путь через API на фейках."""

import array
import math
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from aiplatform.core import media
from aiplatform.fakes import fake_llm
from aiplatform.montage import edl, plan, render, stages, sync
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase


def w(text: str, s: float, e: float) -> dict:
    return {"w": text, "s": s, "e": e}


# «раз два» — пауза 2 с — «три (четыре) пять»
WORDS = [w("раз", 0.5, 0.8), w("два.", 0.9, 1.2), w("три", 3.2, 3.5), w("четыре", 3.6, 4.0), w("пять.", 4.1, 4.5)]


class EdlTest(unittest.TestCase):
    def test_long_pause_is_shrunk_and_cut_words_are_gone(self):
        result = edl.build(
            WORDS, 5.0, [{"from": 3, "to": 3, "kind": "slip", "reason": ""}], [{"from": 0, "layout": "head"}]
        )
        self.assertEqual([(s.first, s.last) for s in result.segments], [(0, 1), (2, 2), (4, 4)])
        first, second, third = result.segments
        self.assertLessEqual(first.end, 1.2 + edl.KEEP_PAUSE_S / 2 + 1e-9, "пауза ужата: от неё осталась половинка")
        self.assertGreaterEqual(second.start, 3.2 - edl.KEEP_PAUSE_S / 2 - 1e-9)
        self.assertLessEqual(second.end, 3.6, "граница — до вырезанного слова")
        self.assertGreaterEqual(third.start, 4.0, "и после него")
        self.assertAlmostEqual(result.duration, sum(s.duration for s in result.segments))
        self.assertEqual(result.segments[1].out_start, first.duration)

    def test_layout_switch_inside_speech_is_seamless(self):
        layout = [{"from": 0, "layout": "head"}, {"from": 3, "layout": "split"}]
        result = edl.build(WORDS, 5.0, [], layout)
        modes = [(s.layout, s.first, s.last) for s in result.segments]
        self.assertEqual(modes, [("head", 0, 1), ("head", 2, 2), ("split", 3, 4)])
        self.assertAlmostEqual(result.segments[1].end, result.segments[2].start, msg="стык без паузы")
        self.assertAlmostEqual(result.segments[1].end, (3.5 + 3.6) / 2)

    def test_boundary_snaps_to_silence(self):
        # Whisper поставил начало «три» на 3.2, а по звуку речь пошла с 3.0 — граница по звуку.
        result = edl.build(WORDS, 5.0, [], [{"from": 0, "layout": "head"}], [(1.25, 3.0)])
        self.assertAlmostEqual(result.segments[1].start, 3.0 - edl.KEEP_PAUSE_S / 2)
        self.assertAlmostEqual(result.segments[0].end, 1.25 + edl.KEEP_PAUSE_S / 2)
        # Тишина «залезает» в слово (тихий звук в начале) — внутрь слова граница не идёт.
        late = edl.build(WORDS, 5.0, [], [{"from": 0, "layout": "head"}], [(1.25, 3.35)])
        self.assertLessEqual(late.segments[1].start, 3.2)

    def test_output_words_and_time_mapping(self):
        result = edl.build(WORDS, 5.0, [{"from": 1, "to": 2, "kind": "retake", "reason": ""}], [])
        out = edl.output_words(WORDS, result)
        self.assertEqual([x["src"] for x in out], [0, 3, 4])
        self.assertTrue(all(a["e"] <= b["s"] + 1e-9 for a, b in zip(out, out[1:], strict=False)))
        self.assertAlmostEqual(result.to_source(out[1]["s"]), 3.6, places=3)

    def test_clean_cuts_and_layout(self):
        raw = [
            {"from": 3, "to": 1, "kind": "slip"},
            {"from": 2, "to": 4, "kind": "slip"},
            {"from": 4, "to": 9, "kind": "retake", "reason": "x" * 400},
            {"from": "a"},
            "мусор",
        ]
        cuts = edl.clean_cuts(raw, 6)
        self.assertEqual([(c["from"], c["to"], c["kind"]) for c in cuts], [(1, 4, "slip"), (5, 5, "retake")])
        self.assertEqual(len(cuts[1]["reason"]), 300)
        self.assertEqual(edl.clean_cuts([{"from": 0, "kind": "??"}], 3)[0]["kind"], "extra")
        marks = [{"from": 3, "layout": "screen"}, {"from": 5, "layout": "screen"}, {"from": 9, "layout": "head"},
                 {"from": 1, "layout": "tv"}]  # fmt: skip
        self.assertEqual(
            edl.clean_layout(marks, 8, True), [{"from": 0, "layout": "head"}, {"from": 3, "layout": "screen"}]
        )
        self.assertEqual(edl.clean_layout(marks, 8, False), [{"from": 0, "layout": "head"}])

    def test_everything_cut_gives_no_segments(self):
        self.assertEqual(edl.build(WORDS, 5.0, [{"from": 0, "to": 4, "kind": "extra", "reason": ""}], []).segments, [])


class PlanTest(unittest.TestCase):
    def test_pack_marks_phrases_and_long_pauses(self):
        packed = plan.pack(WORDS)
        self.assertEqual(packed.splitlines(), ["[0] 0.5 раз два.", "(пауза 2.0 с)", "[2] 3.2 три четыре пять."])

    def test_terms_and_lessons(self):
        text = plan.LESSONS_TEMPLATE
        self.assertEqual(plan.terms(text), ["Claude Code", "ffmpeg", "GitHub"])
        added = plan.add_lesson(text, "  Не  резать «вот» в показе. ", today=date(2026, 10, 18))
        self.assertTrue(added.endswith("## Правила\n- 2026-10-18: Не резать «вот» в показе.\n"))
        self.assertEqual(plan.add_lesson(text, "   "), text)
        self.assertIn("## Правила\n\n- ", plan.add_lesson("# свои", "правило"))

    def test_prompts_and_parsing(self):
        prompt = plan.plan_prompt("[0] 0.0 привет", "ПРАВИЛА", "УРОКИ", True, "Обзор")
        self.assertTrue(prompt.startswith("### task: montage_plan\nПРАВИЛА"))
        self.assertIn("Есть запись экрана", prompt)
        revise = plan.revise_prompt("[0] 0.0 привет", "П", "У", False, [], [{"from": 0, "layout": "head"}], "резче")
        self.assertIn('"lesson"', revise)
        self.assertIn("резче", revise)
        parsed = plan.parse_plan(
            {"cuts": [{"from": 0, "to": 0, "kind": "filler"}], "layout": [], "lesson": "x"}, 2, False
        )
        self.assertEqual(
            (len(parsed["cuts"]), parsed["layout"], parsed["lesson"]), (1, [{"from": 0, "layout": "head"}], "x")
        )
        with self.assertRaises(ValueError):
            plan.parse_plan([], 2, False)
        self.assertEqual(plan.parse_issues({"issues": [{"from": 1, "to": 1}]}, 3)[0]["kind"], "check")

    def test_fake_llm_follows_the_rules_it_was_given(self):
        text = "Привет. Э-э, начнём. Новый режим, новый режим монтажа. Покажи экран. Тут видно. Вырежи это. Итоги."
        words = [w(t, i * 0.4, i * 0.4 + 0.35) for i, t in enumerate(text.split())]
        answer = fake_llm.montage("montage_plan", plan.plan_prompt(plan.pack(words), "", "", True))
        kinds = sorted((c["from"], c["kind"]) for c in answer["cuts"])
        self.assertIn((1, "filler"), kinds)
        self.assertIn((3, "retake"), kinds)
        self.assertIn((8, "command"), kinds, "«Покажи экран» — команда")
        self.assertIn((10, "command"), kinds, "«Вырежи это» убирает фразу перед собой")
        self.assertIn((12, "command"), kinds)
        self.assertEqual(answer["layout"][1], {"from": 10, "layout": "split"})


class RenderArgvTest(unittest.TestCase):
    def spec(self, layout, screen=True):
        return render.SegmentSpec(10.0, 12.5, layout, "punch", Path("h.mp4"), Path("s.mov") if screen else None, -1.5)

    def test_segment_argv_per_layout(self):
        head = render.segment_argv("ffmpeg", self.spec("head"), Path("o.mp4"))
        self.assertEqual(head.count("-i"), 1)
        self.assertIn("eq=contrast", " ".join(head))
        split = render.segment_argv("ffmpeg", self.spec("split"), Path("o.mp4"), Path("m.png"))
        joined = " ".join(split)
        self.assertEqual(split.count("-i"), 3)
        self.assertIn("-ss 8.500", joined, "экран со сдвигом")
        self.assertIn("alphamerge", joined)
        self.assertIn("afade=t=out:st=2.470", joined)
        fallback = render.segment_argv("ffmpeg", self.spec("screen", screen=False), Path("o.mp4"))
        self.assertEqual(fallback.count("-i"), 1, "без записи экрана — голова")

    def test_cache_key_changes_with_parameters(self):
        a, b = self.spec("head"), self.spec("split")
        self.assertNotEqual(a.key(), b.key())
        self.assertEqual(a.key(), self.spec("head").key())

    def test_final_argv_copies_video_without_effects(self):
        plain = render.final_argv("ffmpeg", Path("j.mp4"), Path("o.mp4"))
        self.assertIn("copy", plain)
        self.assertIn(render.LOUDNORM, plain)
        fancy = render.final_argv("ffmpeg", Path("j.mp4"), Path("o.mp4"), subtitles=Path("/x/it's:a.ass"),
                                  overlays=[render.Overlay(Path("ov.mov"), 3.0, 2.0)])  # fmt: skip
        graph = fancy[fancy.index("-filter_complex") + 1]
        self.assertIn("between(t,3.000,5.000)", graph)
        self.assertIn(r"subtitles=filename='/x/it\'s\:a.ass'", graph)

    def test_subtitles(self):
        words = [w("Привет,", 0, 0.4), w("это", 0.5, 0.7), w("тест.", 0.8, 1.0), w("Дальше", 1.2, 1.5),
                 w("идёт", 2.5, 2.8)]  # fmt: skip
        items = render.chunks(words)
        self.assertEqual([c["text"] for c in items], ["Привет, это тест.", "Дальше", "идёт"])
        self.assertIn("00:00:01,200 --> 00:00:01,500\nДальше", render.srt(items))
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Main,,0,0,0,,ПРИВЕТ, ЭТО ТЕСТ.", render.ass(items, ""))
        self.assertIn("file 'a'\\''b.mp4'", render.concat_list([Path("a'b.mp4")]))


class SyncTest(unittest.TestCase):
    def test_offset_from_envelopes(self):
        def bursts(shift_s: float) -> array.array:
            out = array.array("h", [0] * int(sync.RATE * 30))
            for k, at in enumerate((2.0, 3.1, 7.4, 9.0, 13.3, 17.8, 21.2)):
                start = int((at + shift_s) * sync.RATE)
                for i in range(int(0.3 * sync.RATE) + k * 200):
                    if 0 <= start + i < len(out):
                        out[start + i] = int(8000 * math.sin(i / 3))
            return out

        offset, score = sync.find_offset(bursts(0.0), bursts(-1.73), max_lag_s=5)
        self.assertAlmostEqual(offset, -1.73, delta=0.02)
        self.assertGreater(score, 0.8)


def make_video(path: Path, seconds: float, *, audio: bool = True, size: str = "640x360") -> None:
    argv = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:rate=30:duration={seconds}",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-shortest", "-c:a", "aac"]
    subprocess.run([*argv, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class StagesTest(unittest.TestCase):
    """Этапы действия (PRD-025): состав, переходы, общий процент и оценка «осталось»."""

    def test_plan_depends_on_action_and_options(self):
        auto = {"mode": "autonomous", "check": True, "motion": False}
        self.assertEqual(
            stages.plan_for("prepare", auto, screen_audio=True),
            ["audio", "sync", "silences", "transcribe", "plan", "segments", "join", "final", "check"],
        )
        self.assertEqual(stages.plan_for("prepare", {"mode": "reviewed"}), ["audio", "silences", "transcribe", "plan"])
        self.assertEqual(stages.plan_for("prepare", {"mode": "manual"}), ["audio", "silences", "transcribe"])
        self.assertEqual(stages.plan_for("revise", auto), ["plan"])
        self.assertEqual(stages.plan_for("render", {"check": False, "motion": True}),
                         ["segments", "join", "final", "motion"])  # fmt: skip

    def test_advance_finish_and_estimates(self):
        items = stages.new(["audio", "transcribe", "plan"])  # веса 3, 40, 8
        stages.advance(items, "audio", 50, now=100)
        self.assertEqual(items[0]["status"], "running")
        self.assertIsNone(stages.remaining(items, now=100), "ещё не по чему судить")
        stages.advance(items, "transcribe", 25, "кусок 1 из 4", now=110)  # звук закрылся сам
        self.assertEqual((items[0]["status"], items[0]["percent"]), ("done", 100.0))
        self.assertEqual(stages.overall(items), round(100 * (3 + 40 * 0.25) / 51, 1))
        # Расшифровка: 25% за 30 с → ещё 90 с; план (вес 8) по скорости 40 с на 13 единиц веса.
        self.assertEqual(stages.stage_remaining(items[1], now=140), 90)
        self.assertEqual(stages.remaining(items, now=140), round(90 + 8 * 40 / 13))
        stages.advance(items, "transcribe", 10, now=141)
        self.assertEqual(items[1]["percent"], 25.0, "процент этапа не откатывается")
        stages.finish(items, "failed", now=150)
        self.assertEqual([s["status"] for s in items], ["done", "failed", "pending"])
        items = stages.new(["segments", "final", "motion"])
        stages.advance(items, "final", 40, now=1)
        stages.finish(items, "done", now=2)
        self.assertEqual([s["status"] for s in items], ["done", "done", "skipped"])
        self.assertEqual(stages.overall(items), 100.0)


class MontageApiTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        root = Path(cls.media.name)
        cls.head, cls.screen, cls.mute = root / "head.mp4", root / "screen.mp4", root / "mute.mp4"
        make_video(cls.head, 24)
        make_video(cls.screen, 26, size="1280x800")
        make_video(cls.mute, 3, audio=False)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def project(self, **options) -> dict:
        status, project = self.json("POST", "/api/montage/projects", {"title": "Обзор недели", "options": options})
        self.assertEqual(status, 201, project)
        return project

    def upload(self, pid, role, path, expect=201):
        status, body = self.json("POST", f"/api/montage/projects/{pid}/sources/{role}", path.read_bytes())
        self.assertEqual(status, expect, body)
        return body

    def settle(self, pid, statuses=("review", "done", "failed", "canceled"), timeout=120):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def test_config(self):
        status, cfg = self.json("GET", "/api/montage/config")
        self.assertEqual(status, 200)
        self.assertEqual((cfg["default_model"], cfg["backend"]), ("whisper-large-v3-turbo", "fake"))
        self.assertEqual([g["id"] for g in cfg["grades"]], ["none", "punch", "warm"])

    def test_record_to_video_with_review(self):
        project = self.project(grade="punch")
        pid = project["id"]
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/start")[0], 409, "без записи нельзя")
        uploaded = self.upload(pid, "head", self.head)
        self.assertAlmostEqual(uploaded["sources"]["head"]["duration"], 24, delta=0.2)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 409, "до расшифровки нельзя")

        status, started = self.json("POST", f"/api/montage/projects/{pid}/start")
        self.assertEqual(status, 200, started)
        ready = self.settle(pid)
        self.assertEqual(ready["status"], "review", ready)
        self.assertGreater(ready["plan"]["cuts"], 0)
        self.assertLess(ready["plan"]["result_s"], ready["plan"]["source_s"])

        status, view = self.json("GET", f"/api/montage/projects/{pid}/plan")
        self.assertEqual(status, 200)
        kinds = {c["kind"] for c in view["cuts"]}
        self.assertTrue({"filler", "retake", "command"} <= kinds, kinds)
        self.assertEqual({s["layout"] for s in view["segments"]}, {"head"}, "без экрана — только голова")

        # Человек снимает одну вырезку и добавляет свою.
        edited = [*view["cuts"][1:], {"from": 0, "to": 0, "kind": "human", "reason": "лишнее приветствие"}]
        status, saved = self.json(
            "POST", f"/api/montage/projects/{pid}/plan", {"cuts": edited, "layout": view["layout"]}
        )
        self.assertEqual(status, 200, saved)
        _, view2 = self.json("GET", f"/api/montage/projects/{pid}/plan")
        self.assertEqual(view2["cuts"][0], {"from": 0, "to": 0, "kind": "human", "reason": "лишнее приветствие"})

        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 409, "план не подтверждён")
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "edl"})[0], 200)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 200)
        done = self.settle(pid, ("done", "failed", "canceled"))
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["check"]["rounds"][0]["issues"], [])
        status, headers, _ = self.call("GET", done["video_url"] + "?download=1")
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))
        self.assertIn(f"montage-{pid}.mp4", headers["Content-Disposition"])
        status, _, srt = self.call("GET", done["srt_url"])
        self.assertEqual(status, 200)
        self.assertIn("-->", srt.decode())
        out = Path(self.tmp.name) / "montage" / "projects" / pid / "output.mp4"
        self.assertAlmostEqual(media.duration(out), done["output"]["duration"], delta=0.3)

        # Пересборка без правок берёт сегменты из кэша.
        cache = Path(self.tmp.name) / "montage" / "projects" / pid / "segments"
        before = {p.name: p.stat().st_mtime_ns for p in cache.glob("*.mp4")}
        self.json("POST", f"/api/montage/projects/{pid}/render")
        self.settle(pid, ("done", "failed"))
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in cache.glob("*.mp4")}, before)

    def test_screen_layouts_auto_and_feedback_lesson(self):
        project = self.project(auto=True, check=False, subtitles=True)
        pid = project["id"]
        self.upload(pid, "head", self.head)
        self.upload(pid, "screen", self.screen)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        done = self.settle(pid, ("done", "failed", "canceled"))
        self.assertEqual(done["status"], "done", done)
        ids = ["audio", "sync", "silences", "transcribe", "plan", "segments", "join", "final"]
        self.assertEqual([s["id"] for s in done["stages"]], ids, "auto без проверки: подготовка и сборка")
        self.assertTrue(all(s["status"] == "done" and s["finished_at"] >= s["started_at"] for s in done["stages"]))
        self.assertIsNone(done["eta_s"])
        self.assertIn(done["sync"]["method"], ("audio", "weak"), "у экрана есть звук — сдвиг искали")
        _, view = self.json("GET", f"/api/montage/projects/{pid}/plan")
        self.assertIn("split", {s["layout"] for s in view["segments"]}, "«Покажи экран» → экран и голова")
        self.assertTrue((Path(self.tmp.name) / "montage" / "projects" / pid / "subtitles.ass").is_file())

        status, revised = self.json("POST", f"/api/montage/projects/{pid}/revise", {"text": "Не режь слово «вот»"})
        self.assertEqual(status, 200, revised)
        again = self.settle(pid, ("review", "failed"))
        self.assertEqual(again["status"], "review", again)
        _, rules = self.json("GET", "/api/montage/rules")
        self.assertIn("демо-правило: Не режь слово «вот»", rules["lessons"])
        self.assertIn("Термины", rules["lessons"])
        self.assertEqual([h["action"] for h in again["history"]][-3:], ["feedback", "lesson", "plan"])

    def test_bad_requests(self):
        for options, reason in (
            ({"model": "nope"}, "модель"),
            ({"grade": "sepia"}, "Цветокоррекция"),
            ({"auto": "да"}, "auto"),
            ({"max_pause": 10}, "пауза"),
        ):
            status, body = self.json("POST", "/api/montage/projects", {"options": options})
            self.assertEqual(status, 400, body)
            self.assertIn(reason, body["error"])
        pid = self.project()["id"]
        self.assertIn("видео", self.upload(pid, "head", Path(__file__), expect=400)["error"])
        self.assertIn("звука", self.upload(pid, "head", self.mute, expect=400)["error"])
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/sources/face", b"x")[0], 400)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/revise", {"text": " "})[0], 400)
        self.assertEqual(self.json("GET", "/api/montage/projects/0123456789ab")[0], 404)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/delete")[0], 200)
        self.assertEqual(self.json("GET", f"/api/montage/projects/{pid}")[0], 404)
        self.assertEqual(self.json("POST", "/api/montage/rules", {"lessons": 5})[0], 400)

    def test_disabled_model_is_hidden_and_refused(self):
        settings = self.config.settings
        pid = self.project(model="whisper-large-v3-turbo")["id"]
        self.upload(pid, "head", self.head)
        settings.change(lambda data: data["disabled"].update(asr=["whisper-large-v3-turbo"]))
        try:
            _, cfg = self.json("GET", "/api/montage/config")
            self.assertNotIn("whisper-large-v3-turbo", [m["id"] for m in cfg["models"]])
            self.assertEqual(cfg["default_model"], "whisper-large-v3")
            self.assertEqual(self.project()["options"]["model"], "whisper-large-v3")
            status, err = self.json("POST", f"/api/montage/projects/{pid}/start")
            self.assertEqual(status, 409, err)
            self.assertIn("Настройки", err["error"])
        finally:
            settings.change(lambda data: data["disabled"].update(asr=[]))

    def test_cancel_and_failed_asr(self):
        pid = self.project()["id"]
        self.upload(pid, "head", self.head)
        with mock.patch.dict("os.environ", {"FAKE_ASR_FAIL": "1"}):
            self.json("POST", f"/api/montage/projects/{pid}/start")
            failed = self.settle(pid, ("failed", "review"))
        self.assertEqual(failed["status"], "failed")
        states = {s["id"]: s["status"] for s in failed["stages"]}
        self.assertEqual(states, {"audio": "done", "silences": "done", "transcribe": "failed", "plan": "pending"})
        self.assertTrue(failed["error"])
        with mock.patch.dict("os.environ", {"FAKE_ASR_STEP_DELAY": "2"}):
            self.json("POST", f"/api/montage/projects/{pid}/start")
            self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/cancel")[0], 200)
            canceled = self.settle(pid, ("canceled", "review", "failed"))
        self.assertEqual(canceled["status"], "canceled", canceled)


class ProblemsTest(PlatformCase):
    modules = staticmethod(montage_only)
    extra_env = {"AIP_BACKEND": ""}

    def test_no_model_is_a_problem_and_start_is_refused(self):
        module = self.app.modules["montage"]
        problems = module.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("make models M=whisper-large-v3-turbo", problems[0])
        self.assertFalse(any(m["installed"] for m in module.describe()["models"]))


if __name__ == "__main__":
    unittest.main()
