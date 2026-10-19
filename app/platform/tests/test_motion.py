"""Вкладка «Моушн» (PRD-026): роли агентов, время и пакет, импорт из «Монтажа», путь через API на фейках."""

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from aiplatform.motion import agents, scenes, source
from aiplatform.motion.projects import auto_scenes
from aiplatform.motion.service import MotionModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase
from tests.test_montage import make_video


def w(text: str, s: float, e: float) -> dict:
    return {"w": text, "s": s, "e": e}


# 120 слов по 0,5 с, фраза — 6 слов, после неё пауза 1 с
WORDS = []
for n in range(120):
    t = n * 0.5 + (n // 6) * 1.0
    WORDS.append(w(f"слово{n}" + ("." if n % 6 == 5 else ""), t, t + 0.4))
DURATION = WORDS[-1]["e"] + 5


class ScenesTest(unittest.TestCase):
    def test_timecode_slug_and_rows(self):
        self.assertEqual(scenes.timecode(3725.5, 30), "01:02:05:15")
        self.assertEqual(scenes.timecode(1.0, 29.97), "00:00:01:00")
        self.assertEqual(scenes.timecode(600.0, 29.97), "00:09:59:12", "как в монтажке без пропуска кадров")
        self.assertEqual(scenes.clock(65), "1:05")
        self.assertEqual(scenes.slug("Буфер канала: Go!"), "bufer-kanala-go")
        items = [
            {"n": 2, "at": 90.0, "end": 98.0, "status": "done", "title": "Слои TCP/IP", "layout": "full"},
            {"n": 1, "at": 30.0, "end": 36.5, "status": "done", "title": "Канал", "layout": "side", "concept": "буфер"},
            {"n": 3, "at": 120.0, "end": 125.0, "status": "failed", "title": "x", "layout": "side"},
            {"n": 4, "at": 150.0, "end": 155.0, "status": "done", "title": "y", "layout": "lower", "enabled": False},
        ]
        rows = scenes.rows(items, 30)
        self.assertEqual([r["n"] for r in rows], [1, 2], "по времени; упавшие и выключенные — мимо")
        self.assertEqual(rows[0]["file"], "01_00-00-30_kanal.mov")
        self.assertEqual(
            (rows[0]["start"], rows[0]["end"], rows[0]["layout"]), ("00:00:30:00", "00:00:36:15", "Половина кадра")
        )
        self.assertIn("01_00-00-30_kanal.mov", scenes.csv_text(rows))
        self.assertIn("00:00:30:00 → 00:00:36:15", scenes.txt_text(rows, "Выпуск", 30))

    def test_package_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mov = root / "a.mov"
            mov.write_bytes(b"mov")
            rows = scenes.rows(
                [{"n": 1, "at": 5.0, "end": 9.0, "status": "done", "title": "Канал", "layout": "side"}], 25
            )
            target = scenes.package(root / "package.zip", rows, {1: mov}, "Выпуск", 25, {"title": "Выпуск"})
            with zipfile.ZipFile(target) as zf:
                self.assertEqual(sorted(zf.namelist()), ["01_00-00-05_kanal.mov", "scenes.json", "timecodes.csv",
                                                         "timecodes.txt"])  # fmt: skip
                self.assertEqual(json.loads(zf.read("scenes.json"))["fps"], 25)

    def test_auto_scenes(self):
        self.assertEqual(auto_scenes(60), 3)
        self.assertEqual(auto_scenes(1758), 12)
        self.assertEqual(auto_scenes(7200), 15)


class AgentsTest(unittest.TestCase):
    def test_moments_are_cleaned_and_spaced(self):
        data = {
            "topic": "Каналы в Go",
            "moments": [
                {"from": 6, "to": 20, "title": "Буфер", "concept": "как значение кладётся в буфер", "score": 9},
                {"from": 12, "to": 30, "title": "Рядом", "concept": "слишком близко к первому", "score": 8},
                {"from": 60, "to": 80, "title": "Слабый", "concept": "польза мала", "score": 3},
                {"from": 90, "to": 400, "title": "Вне", "concept": "индекс за краем", "score": 9},
                {"from": 84, "to": 100, "title": "Блокировка", "concept": "отправитель ждёт", "score": 7, "kind": "x"},
                {"from": 0, "to": 3, "title": "", "concept": "без заголовка", "score": 9},
                "мусор",
            ],
        }
        topic, moments = agents.clean_moments(data, WORDS, DURATION, 5)
        self.assertEqual(topic, "Каналы в Go")
        self.assertEqual([m["title"] for m in moments], ["Буфер", "Блокировка"])
        self.assertEqual(moments[1]["kind"], "mechanism")
        self.assertEqual(moments[0]["at"], WORDS[6]["s"])
        self.assertEqual(agents.clean_moments(data, WORDS, DURATION, 1)[1][0]["title"], "Буфер", "лучшие по пользе")
        self.assertEqual(agents.clean_moments([], WORDS, DURATION, 5), ("", []))

    def test_design_falls_back_to_defaults(self):
        design = agents.clean_design({
            "name": "Неон",
            "palette": {"bg": "#000000", "accent": "red", "ink": "#FFFFFF"},
            "font": "comic-sans",
            "characters": [{"id": "gopher", "name": "Гофер", "look": "голубой"}, {"id": "Bad Id", "look": "x"},
                           {"id": "gopher", "look": "дубль"}],
        })  # fmt: skip
        self.assertEqual(design["palette"]["bg"], "#000000")
        self.assertEqual(design["palette"]["ink"], "#ffffff")
        self.assertEqual(design["palette"]["accent"], agents.DEFAULT_DESIGN["palette"]["accent"])
        self.assertEqual(design["font"], "system")
        self.assertEqual([c["id"] for c in design["characters"]], ["gopher"])
        self.assertIn("gopher (Гофер)", agents.design_text(design))

    def test_storyboard_timing_from_words(self):
        moment = {"from": 12, "to": 30, "title": "Буфер", "concept": "c"}
        design = agents.clean_design({"characters": [{"id": "gopher", "look": "g"}]})
        data = {"title": "Буфер канала", "layout": "full", "from": 12, "to": 30, "hold": 1,
                "characters": ["gopher", "nobody"], "camera": "дрейф",
                "beats": [{"word": 24, "action": "кладёт", "text": "send"}, {"word": 12, "action": "появляется"},
                          {"word": 999, "action": "вне окна"}, {"word": 18, "action": ""}]}  # fmt: skip
        board = agents.clean_storyboard(data, WORDS, moment, design)
        self.assertAlmostEqual(board["at"], WORDS[12]["s"] - agents.LEAD_S, places=3)
        self.assertEqual([b["word"] for b in board["beats"]], [12, 24])
        self.assertAlmostEqual(board["beats"][0]["t"], agents.LEAD_S, places=2)
        self.assertEqual(board["characters"], ["gopher"])
        self.assertLessEqual(board["end"] - board["at"], agents.MAX_SCENE_S)
        self.assertGreaterEqual(board["end"], WORDS[30]["e"])
        with self.assertRaises(agents.AgentError):
            agents.clean_storyboard({"beats": [{"word": 12, "action": "один"}]}, WORDS, moment, design)
        side = agents.clean_storyboard({**data, "layout": "???"}, WORDS, moment, design)
        self.assertEqual((side["layout"], side["camera"]), ("side", ""), "камера — только у full")

    def test_review_verdicts(self):
        moment = {"from": 12, "to": 30, "title": "Буфер", "concept": "c"}
        design = agents.clean_design({})
        beats = [{"word": 12, "action": "a"}, {"word": 20, "action": "b"}]
        board = agents.clean_storyboard({"beats": beats}, WORDS, moment, design)
        self.assertEqual(agents.apply_review({"verdict": "ok"}, WORDS, moment, board, design)[0], "ok")
        verdict, _, issues = agents.apply_review(
            {"verdict": "drop", "reason": "не о том"}, WORDS, moment, board, design
        )
        self.assertEqual((verdict, issues[0]), ("drop", "не о том"))
        fixed = {"verdict": "fix", "issues": ["слои наоборот"], "storyboard": {"title": "Верно", "beats": beats}}
        verdict, new, _ = agents.apply_review(fixed, WORDS, moment, board, design)
        self.assertEqual((verdict, new["title"]), ("fix", "Верно"))
        broken = {"verdict": "fix", "storyboard": {"beats": []}}
        verdict, same, issues = agents.apply_review(broken, WORDS, moment, board, design)
        self.assertEqual((verdict, same), ("ok", board))
        self.assertIn("исправление не принято", issues[-1])

    def test_code_checks(self):
        good = {"html": '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>', "css": "#scene{}",
                "js": 'tl.from("#scene svg", {opacity: 0}, BEATS[0].t);'}  # fmt: skip
        self.assertEqual(agents.clean_code(good), good)
        bad = {
            "<script": {**good, "html": "<script>alert(1)</script>"},
            "<script> закрытие": {**good, "js": "const s = '</script><img src=x onerror=alert(1)>';"},
            "<script> комментарий": {**good, "js": "const s = '<!--';"},
            "сеть": {**good, "js": "fetch('/x')"},
            "внешняя ссылка": {**good, "css": "#scene{background:url(https://evil.example/x.png)}"},
            "таймер": {**good, "js": "setTimeout(() => 0, 1)"},
            "Math.random": {**good, "js": "tl.to('#a', {x: Math.random()})"},
            "запуск": {**good, "js": "tl.play()"},
            "обработчик": {**good, "html": '<img src="x" onerror="alert(1)">'},
            "нет куска": {"html": "<b>x</b>", "css": ""},
        }
        for what, code in bad.items():
            with self.subTest(what=what), self.assertRaises(agents.AgentError) as ctx:
                agents.clean_code(code)
            self.assertIn(what.split()[0], str(ctx.exception))

    def test_composition_and_sizes(self):
        self.assertEqual(agents.frame_size(3840, 2160, "1080"), (1920, 1080))
        self.assertEqual(agents.frame_size(3840, 2160, "source"), (3840, 2160))
        self.assertEqual(agents.frame_size(1080, 1920, "1080"), (1080, 1920))
        self.assertEqual(agents.frame_size(1280, 720, "1080"), (1280, 720))
        moment = {"from": 12, "to": 30, "title": "Буфер", "concept": "c"}
        design = agents.clean_design({})
        beats = [{"word": 12, "action": "a", "text": "</script>"}, {"word": 20, "action": "b"}]
        data = {"title": "Буфер </title>", "layout": "side", "beats": beats}
        board = agents.clean_storyboard(data, WORDS, moment, design)
        code = {
            "html": '<div class="card">x</div>',
            "css": "#scene .card{}",
            "js": "tl.to('#scene .card', {x: 10}, 0);",
        }
        doc = agents.composition(board, code, design, (3840, 2160))
        self.assertIn('data-composition-id="scene"', doc)
        self.assertIn('data-width="3840" data-height="2160"', doc)
        self.assertIn("transform:scale(2.000000)", doc)
        self.assertIn("window.__timelines.scene = tl;", doc)
        self.assertIn("Content-Security-Policy", doc)
        self.assertIn("--accent:", doc)
        self.assertIn("<title>Буфер &lt;/title&gt;</title>", doc)
        self.assertNotIn('</script>"', doc, "текст бита не закрывает скрипт")
        self.assertIn("left:960px", doc, "half: правая половина логического кадра")


class SourceTest(unittest.TestCase):
    def test_montage_output_words_follow_cuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "abcdef123456"
            folder.mkdir()
            words = [w("раз", 0.5, 0.8), w("два.", 0.9, 1.2), w("три", 3.2, 3.5), w("четыре", 3.6, 4.0)]
            project = {"status": "done", "title": "Обзор", "sources": {"head": {"duration": 5.0}},
                       "options": {"max_pause": 0.6}, "output": {"duration": 3.0}, "updated_at": 1}  # fmt: skip
            (folder / "project.json").write_text(json.dumps(project))
            (folder / "words.json").write_text(json.dumps({"words": words}))
            (folder / "plan.json").write_text(json.dumps({"cuts": [{"from": 1, "to": 1, "kind": "slip"}]}))
            self.assertEqual(source.finished(Path(tmp)), [], "без output.mp4 — не готов")
            (folder / "output.mp4").write_bytes(b"x")
            self.assertEqual([p["id"] for p in source.finished(Path(tmp))], ["abcdef123456"])
            out = source.output_words(folder)
            self.assertEqual([x["w"] for x in out], ["раз", "три", "четыре"])
            self.assertLess(out[1]["s"], 1.5, "пауза ужата: «три» сразу после «раз»")
            self.assertTrue(all(b["s"] >= a["s"] for a, b in zip(out, out[1:], strict=False)))


def motion_only(ctx):
    return [MotionModule(ctx)]


@NEEDS_FFMPEG
class MotionApiTest(PlatformCase):
    modules = staticmethod(motion_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        cls.video = Path(cls.media.name) / "talk.mp4"
        make_video(cls.video, 40)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def settle(self, pid, timeout=120):
        return self.poll(f"/api/motion/projects/{pid}", lambda p: p["status"] in ("done", "failed", "canceled"),
                         timeout=timeout)  # fmt: skip

    def project(self, **body):
        status, project = self.json("POST", "/api/motion/projects", {"title": "Каналы в Go", **body})
        self.assertEqual(status, 201, project)
        return project

    def test_config(self):
        status, cfg = self.json("GET", "/api/motion/config")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in cfg["roles"]], ["editor", "designer", "director", "expert", "animator"])
        self.assertEqual(cfg["backend"], "fake")

    def test_video_to_scenes_and_package(self):
        pid = self.project(options={"max_scenes": 3, "brief": "показать каналы"})["id"]
        self.assertEqual(self.json("POST", f"/api/motion/projects/{pid}/start")[0], 409, "без видео нельзя")
        status, project = self.json("POST", f"/api/motion/projects/{pid}/source", self.video.read_bytes())
        self.assertEqual(status, 201, project)
        self.assertEqual(project["source"]["fps"], 30)
        self.json("POST", f"/api/motion/projects/{pid}/start")
        project = self.settle(pid)
        self.assertEqual(project["status"], "done", (project["error"], project["log"][-5:]))
        self.assertEqual([s["status"] for s in project["stages"]], ["done"] * 7)
        done = [s for s in project["scenes"] if s["status"] == "done"]
        self.assertTrue(1 <= len(done) <= 3, project["scenes"])
        scene = done[0]
        self.assertEqual(set(scene["urls"]), {"preview.mp4", "poster.jpg", "scene.mov", "index.html"})
        self.assertRegex(scene["timecode"], r"^00:00:\d\d:\d\d$")
        status, headers, body = self.call("GET", scene["urls"]["scene.mov"])
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Content-Type"], "video/quicktime")
        status, headers, _ = self.call("GET", scene["urls"]["index.html"])
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8", "композиция — не страница платформы")
        folder = Path(self.tmp.name) / "motion" / "projects" / pid
        mov = folder / "scenes" / scene["id"] / "scene.mov"
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name,pix_fmt,width", "-of", "csv=p=0", str(mov)],
                               capture_output=True, text=True, check=True).stdout  # fmt: skip
        self.assertIn("prores", probe)
        self.assertIn("yuva", probe, "альфа-канал")
        status, headers, raw = self.call("GET", project["package_url"])
        self.assertEqual(status, 200)
        package = folder / "check.zip"
        package.write_bytes(raw)
        with zipfile.ZipFile(package) as zf:
            names = zf.namelist()
        self.assertIn("timecodes.csv", names)
        self.assertEqual(sum(1 for n in names if n.endswith(".mov")), len(done))

        # Сцену выключили — в пакете её нет; переделка по замечанию — та же сцена, новая раскадровка.
        status, body = self.json("POST", f"/api/motion/projects/{pid}/scenes/{scene['id']}", {"enabled": False})
        self.assertEqual(status, 200, body)
        status, _, raw = self.call("GET", project["package_url"])
        if len(done) == 1:
            self.assertEqual(status, 404, "единственная сцена выключена — пакета нет")
        else:
            package.write_bytes(raw)
            with zipfile.ZipFile(package) as zf:
                self.assertEqual(sum(1 for n in zf.namelist() if n.endswith(".mov")), len(done) - 1)
        status, _ = self.json("POST", f"/api/motion/projects/{pid}/scenes/{scene['id']}/redo", {"text": "крупнее"})
        self.assertEqual(status, 200)
        project = self.settle(pid)
        redone = next(s for s in project["scenes"] if s["id"] == scene["id"])
        self.assertEqual((redone["status"], redone["title"], redone["feedback"]), ("done", "Правка", ["крупнее"]))

        # Своя сцена на 30-й секунде.
        status, body = self.json("POST", f"/api/motion/projects/{pid}/scenes", {"at": 30, "text": "покажи select"})
        self.assertEqual(status, 200, body)
        project = self.settle(pid)
        added = project["scenes"][-1]
        self.assertEqual(added["feedback"], ["покажи select"])
        self.assertEqual(added["status"], "done", added)

    def test_failed_redo_keeps_ready_scene(self):
        pid = self.project(options={"max_scenes": 1})["id"]
        self.json("POST", f"/api/motion/projects/{pid}/source", self.video.read_bytes())
        self.json("POST", f"/api/motion/projects/{pid}/start")
        scene = self.settle(pid)["scenes"][0]
        self.assertEqual(scene["status"], "done")
        self.process_env_extra({"FAKE_HYPERFRAMES_FAIL": "1"})
        self.json("POST", f"/api/motion/projects/{pid}/scenes/{scene['id']}/redo", {"text": "крупнее"})
        project = self.settle(pid)
        again = project["scenes"][0]
        self.assertEqual((again["status"], again["title"]), ("done", scene["title"]), "прежняя версия осталась")
        self.assertIn("переделка не удалась", again["error"])
        self.assertEqual(again["feedback"], ["крупнее"])
        self.assertEqual(self.call("GET", again["urls"]["scene.mov"])[0], 200)
        self.assertIsNotNone(project["package_url"])

    def test_animator_repairs_forbidden_code(self):
        self.process_env_extra({"FAKE_LLM_MOTION_BADCODE": "1"})
        pid = self.project(options={"max_scenes": 1})["id"]
        self.json("POST", f"/api/motion/projects/{pid}/source", self.video.read_bytes())
        self.json("POST", f"/api/motion/projects/{pid}/start")
        project = self.settle(pid)
        scene = project["scenes"][0]
        self.assertEqual((scene["status"], scene["attempts"]), ("done", 2), project["log"][-5:])
        self.assertTrue(any("таймер" in line for line in project["log"]))

    def test_expert_drops_scenes(self):
        self.process_env_extra({"FAKE_LLM_MOTION_DROP": "1"})
        pid = self.project(options={"max_scenes": 2})["id"]
        self.json("POST", f"/api/motion/projects/{pid}/source", self.video.read_bytes())
        self.json("POST", f"/api/motion/projects/{pid}/start")
        project = self.settle(pid)
        self.assertEqual(project["status"], "done")
        self.assertTrue(all(s["status"] == "dropped" for s in project["scenes"]))
        self.assertIsNone(project["package_url"], "нечего паковать")

    def test_from_montage(self):
        montage = Path(self.tmp.name) / "montage" / "projects" / "0123456789ab"
        montage.mkdir(parents=True)
        subprocess.run(["cp", str(self.video), str(montage / "output.mp4")], check=True)
        words = [w(f"w{n}" + ("." if n % 6 == 5 else ""), 2 + n * 0.5, 2.4 + n * 0.5) for n in range(60)]
        (montage / "words.json").write_text(json.dumps({"words": words}))
        meta = {"status": "done", "title": "Обзор недели", "sources": {"head": {"duration": 40}}, "updated_at": 1}
        (montage / "project.json").write_text(json.dumps(meta))
        status, listed = self.json("GET", "/api/motion/montage")
        self.assertEqual([p["id"] for p in listed["projects"]], ["0123456789ab"])
        project = self.project(title="", montage="0123456789ab")
        self.assertEqual((project["title"], project["transcript"]["model"]), ("Обзор недели", "montage"))
        self.assertEqual(self.json("POST", "/api/motion/projects", {"montage": "fffffffffff0"})[0], 400)
        self.json("POST", f"/api/motion/projects/{project['id']}/start")
        project = self.settle(project["id"])
        self.assertEqual(project["status"], "done", project["error"])
        self.assertNotIn("transcribe", [s["id"] for s in project["stages"]], "расшифровка — готовая")

    def process_env_extra(self, env):
        import os

        for key, value in env.items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)


if __name__ == "__main__":
    unittest.main()
