"""Раскадровка «Монтажа» по MASTER_SPEC (PRD-027): материалы, знания, биты, стиль, 12 шаблонов, сцены,
рендер по хэшу и путь через API на фейках."""

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aiplatform.core import media
from aiplatform.fakes import fake_llm
from aiplatform.montage import documents, knowledge, narrative, scenes, schemas, storyboard, templates
from aiplatform.montage import style as style_module
from aiplatform.montage.service import MontageModule
from aiplatform.motion import agents
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase

FAKE_HF = Path(__file__).resolve().parents[1] / "aiplatform" / "fakes" / "fake_hyperframes.py"


def out_words(n: int, fps: int = 30) -> list[dict]:
    """Слова результата: по 0.4 с каждое, src = индекс (ничего не вырезано)."""
    return [{"w": f"слово{i}.", "s": i * 0.4, "e": i * 0.4 + 0.35, "src": i, "start_frame": round(i * 0.4 * fps),
             "end_frame": round((i * 0.4 + 0.35) * fps)} for i in range(n)]  # fmt: skip


def docx_bytes(paragraphs: list[str]) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{p[2:]}</w:t></w:r></w:p>'
        if p.startswith("# ")
        else f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>"
        for p in paragraphs
    )
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


class DocumentsTest(unittest.TestCase):
    def test_markdown_docx_and_pdf_without_pdftotext(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            text = "# Каналы\n\nБуфер два.\n\n" + "слово " * 600 + "\n\n## Итог\n\nвсё."
            md = documents.add(folder, "конспект.md", text.encode())
            self.assertEqual((md["id"], md["ext"], md["status"]), ("doc_1", "md", "ready"))
            self.assertGreaterEqual(md["chunks"], 2)
            docx = documents.add(folder, "статья.docx", docx_bytes(["# Глава", "Абзац один.", "Абзац два."]))
            self.assertEqual((docx["ext"], docx["status"]), ("docx", "ready"))
            self.assertIn("# Глава", (folder / "documents" / "doc_2.txt").read_text())
            with mock.patch("shutil.which", return_value=None):
                pdf = documents.add(folder, "paper.pdf", b"%PDF-1.4 fake")
            self.assertEqual(pdf["status"], "unavailable")
            self.assertIn("pdftotext", pdf["error"])
            with self.assertRaises(documents.DocumentError):
                documents.add(folder, "x.bin", b"\xff\xfe\x00binary")
            items = documents.load(folder)
            self.assertEqual([i["id"] for i in items], ["doc_1", "doc_2", "doc_3"])
            self.assertEqual(schemas.validate_many("source_document", items), [])
            chunks = documents.chunks_of(folder)
            self.assertEqual({c["doc"] for c in chunks}, {"doc_1", "doc_2"}, "PDF без текста не даёт фрагментов")
            self.assertEqual(chunks[0]["heading"], "Каналы")
            self.assertIn("[doc_1#0] (Каналы) Буфер два.", documents.pack(chunks))
            self.assertTrue(documents.remove(folder, "doc_3"))
            self.assertFalse((folder / "documents" / "doc_3.pdf").exists())
            self.assertFalse(documents.remove(folder, "doc_9"))

    def test_pdf_pages_from_form_feed(self):
        chunks = documents.chunk("стр1\n\fстр2 \n\n\fстр3", "doc_1")
        self.assertEqual([(c["page"], c["text"]) for c in chunks], [(1, "стр1"), (2, "стр2"), (3, "стр3")])


class KnowledgeAndBeatsTest(unittest.TestCase):
    def setUp(self):
        self.words = out_words(40)
        self.chunks = [{"doc": "doc_1", "n": 0, "heading": "", "page": None, "text": "буфер два"}]

    def test_knowledge_clean(self):
        raw = {"claims": [
            {"statement": "Буфер на два.", "transcript": [5, 2], "citations": [
                {"doc": "doc_1", "chunk": 0, "quote": "буфер два"}, {"doc": "doc_9", "chunk": 0, "quote": "нет"}],
             "confidence": 1.7, "contradiction_status": "contradicted"},
            {"statement": "", "citations": []},
            {"statement": "Без цитат", "transcript": [100, 200], "citations": [], "confidence": "x",
             "contradiction_status": "странно"},
        ]}  # fmt: skip
        claims = knowledge.clean(raw, self.words, self.chunks)
        self.assertEqual(schemas.validate_many("knowledge_claim", claims), [])
        self.assertEqual([c["id"] for c in claims], ["claim_001", "claim_002"])
        first = claims[0]
        self.assertEqual((first["transcript"], first["confidence"], len(first["citations"])), ([2, 5], 1.0, 1))
        self.assertEqual(claims[0]["contradiction_status"], "contradicted")
        self.assertEqual((claims[1]["transcript"], claims[1]["confidence"], claims[1]["contradiction_status"]),
                         ([39, 39], 0.5, "none"))  # fmt: skip
        self.assertEqual(knowledge.clean(raw, self.words, [])[0]["contradiction_status"], "none", "без материалов")
        self.assertEqual(len(knowledge.flagged(claims)), 1)
        prompt = knowledge.prompt("[0] 0.0 привет", self.chunks, "Обзор")
        self.assertTrue(prompt.startswith("### task: montage_knowledge"))
        self.assertIn("[doc_1#0] буфер два", prompt)
        self.assertIn("игнорируй", prompt)

    def test_beats_clean_frames_and_pacing(self):
        claims = [{"id": "claim_001"}]
        raw = {"beats": [
            {"from": 10, "to": 20, "purpose": "explanation", "goal": "как", "difficulty": 9, "priority": 0,
             "claims": ["claim_001", "claim_7"], "confidence": 0.9},
            {"from": 0, "to": 12, "purpose": "hook", "goal": "зачем"},
            {"from": 30, "to": 35, "purpose": "???", "keywords": ["Go", 5]},
        ]}  # fmt: skip
        beats = narrative.clean(raw, self.words, claims)
        self.assertEqual(schemas.validate_many("narrative_beat", beats), [])
        self.assertEqual([(b["from"], b["to"], b["purpose"]) for b in beats],
                         [(0, 12, "hook"), (13, 20, "explanation"), (30, 35, "other")], "по порядку")  # fmt: skip
        self.assertEqual((beats[1]["difficulty"], beats[1]["priority"], beats[1]["claims"]), (5, 1, ["claim_001"]))
        self.assertEqual(beats[2]["keywords"], ["Go"])
        self.assertEqual(beats[0]["start_frame"], 0)
        self.assertEqual(beats[0]["end_frame"], self.words[12]["end_frame"])
        # Вырезали слова 13–20 → бит explanation без кадров, бит hook на месте.
        kept = [w for w in self.words if not 13 <= w["src"] <= 20]
        again = narrative.with_frames(beats, kept)
        self.assertEqual((again[1]["start_frame"], again[1]["end_frame"]), (0, 0))
        self.assertEqual(again[0]["end_frame"], beats[0]["end_frame"])
        pacing = narrative.pacing(beats, 30)
        self.assertEqual(pacing["beats"], 3)
        self.assertIn("explanation", pacing["share"])
        self.assertIn("- beat_001 [0–12] hook (p3, d3): зачем — «слово0.", narrative.pack_beats(beats))


class StyleTest(unittest.TestCase):
    def test_clean_defaults_and_revisions(self):
        bad = {"name": "Яркий", "palette": {"bg": "red", "accent": "#FFAA00"}, "font": "comic",
               "motion": {"enter": "zoom"}, "captions": {"size": 500}, "density": "ultra"}  # fmt: skip
        style = style_module.clean(bad)
        palette = style["palette"]
        self.assertEqual((style["name"], palette["bg"], palette["accent"]), ("Яркий", "#0f1420", "#ffaa00"))
        self.assertEqual((style["font"], style["motion"]["enter"], style["captions"]["size"], style["density"]),
                         ("system", "power3.out", 72, "medium"))  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertIsNone(style_module.load(folder))
            first = style_module.save(folder, style)
            second = style_module.save(folder, {**first, "name": "Второй"})
            self.assertEqual((first["revision"], second["revision"]), ("style_001", "style_002"))
            self.assertEqual(style_module.load(folder)["name"], "Второй")
            self.assertEqual(schemas.validate("style_bible", second), [])
            self.assertIn("--accent:#ffaa00;", style_module.css_vars(second))
            self.assertIn("Стиль «Второй»", style_module.describe(second))


@NEEDS_FFMPEG
class TemplatesTest(unittest.TestCase):
    def test_catalog_has_twelve_core_templates_with_schemas_in_sync(self):
        self.assertEqual(len(templates.CORE), 12)
        self.assertTrue(set(templates.CORE) <= set(templates.CATALOG))
        for tid in templates.CATALOG:
            path = templates.SCHEMA_DIR / f"{tid}.schema.json"
            self.assertTrue(path.is_file(), f"нет схемы {tid}: python -m aiplatform.montage.templates --schemas")
            self.assertEqual(json.loads(path.read_text()), templates.schema_of(tid), f"схема {tid} отстала от кода")
            props = templates.clean_props(tid, templates.get(tid).example)
            self.assertEqual(schemas.errors(props, templates.schema_of(tid)), [], tid)
        self.assertEqual(templates.resolve("title"), "chapter_title")
        self.assertEqual(templates.resolve("list"), "step_list")
        with self.assertRaises(templates.TemplateError):
            templates.resolve("hologram")

    def test_props_are_cleaned_not_trusted(self):
        with self.assertRaises(templates.TemplateError):
            templates.clean_props("step_list", {"title": "x", "steps": ["один"]})
        with self.assertRaises(templates.TemplateError):
            templates.clean_props("chapter_title", {"kicker": "только надпись"})
        props = templates.clean_props("data_chart", {"title": "t" * 100, "kind": "pie", "series": [
            {"label": "a", "value": "x"}, {"label": "b", "value": 3}, "мусор"]})  # fmt: skip
        self.assertEqual((len(props["title"]), props["kind"], props["series"][0]["value"]), (40, "bar", 0.0))
        long_code = "\n".join(f"line {i}" for i in range(30))
        code = templates.clean_props("code_highlight", {"code": long_code, "highlight": [2, 99, 1.5]})
        self.assertEqual(len(code["code"].splitlines()), 12)
        self.assertEqual(code["highlight"], [1, 2])
        zoom = templates.clean_props("callout_zoom", {"text": "тут", "x_pct": 250, "y_pct": "?"})
        self.assertEqual((zoom["x_pct"], zoom["y_pct"], zoom["radius_pct"]), (100.0, 50.0, 22.5))

    def test_every_template_renders_deterministically_with_the_fake(self):
        style = style_module.clean({})
        style["revision"] = "style_001"
        for tid, t in templates.CATALOG.items():
            props = templates.clean_props(tid, t.example)
            doc = templates.composition(tid, props, style, t.default_s, title=t.label)
            self.assertEqual(doc, templates.composition(tid, props, style, t.default_s, title=t.label), "детерминизм")
            self.assertIn("Content-Security-Policy", doc)
            self.assertIn("window.__timelines.scene = tl;", doc)
            self.assertIn("--accent:", doc)
            body = doc.split("<body>", 1)[1]
            for pattern, what in agents.FORBIDDEN:
                if what in ("свой запуск таймлайна",):
                    continue
                self.assertIsNone(pattern.search(agents._SVG_NS.sub("", body.split("<script>")[0])), (tid, what))
            self.assertNotIn("Math.random", doc)
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / "index.html").write_text(doc, encoding="utf-8")
                argv = [sys.executable, str(FAKE_HF), "render", "--composition", "index.html", "--format", "mov",
                        "--fps", "30", "--output", str(folder / "s.mov")]  # fmt: skip
                run = subprocess.run(argv, cwd=folder, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, (tid, run.stderr))
                self.assertAlmostEqual(media.duration(folder / "s.mov"), t.default_s, delta=0.1, msg=tid)
                scene = {"duration_frames": round(t.default_s * 30), "fps": {"num": 30, "den": 1}}
                self.assertIsNone(scenes.validate(folder / "s.mov", scene, "ffmpeg"), tid)
                self.assertTrue(scenes.thumbnail(folder / "s.mov", folder / "thumb.png", "ffmpeg"), tid)
                self.assertGreater((folder / "thumb.png").stat().st_size, 100)

    def test_html_is_escaped(self):
        props = templates.clean_props("chapter_title", {"title": "<b>x</b> & «y»", "kicker": '"q"'})
        card, _ = templates.render("chapter_title", props)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; «y»", card)
        self.assertNotIn("<b>x</b>", card)


class StoryboardTest(unittest.TestCase):
    def setUp(self):
        self.words = out_words(400)  # 160 с
        self.style = style_module.clean({})
        self.style["revision"] = "style_001"
        self.claims = [{"id": "claim_001"}]
        self.beats = narrative.clean({"beats": [
            {"from": 0, "to": 70, "purpose": "hook", "goal": "a"},
            {"from": 71, "to": 150, "purpose": "explanation", "goal": "b", "claims": ["claim_001"]},
            {"from": 151, "to": 230, "purpose": "demo", "goal": "c"},
            {"from": 231, "to": 300, "purpose": "recap", "goal": "d"},
        ]}, self.words, self.claims)  # fmt: skip

    def scene(self, beat, tid="chapter_title", risk=2, score_boost=0, claims=(), **extra):
        necessity = {"clarity": 3 + score_boost, "novelty": 3, "cognitive_load": 3, "production_cost": 3,
                     "factual_risk": risk}  # fmt: skip
        return {"beat_id": beat, "template_id": tid, "props": {"title": "T", "steps": ["a", "b"]}, "intent": "i",
                "necessity": necessity, "claim_ids": list(claims), "rationale": "r", **extra}  # fmt: skip

    def test_threshold_gap_and_risk(self):
        raw = {"scenes": [
            self.scene("beat_001", score_boost=2, duration_s=99),  # score 14 — проходит, длительность режется
            self.scene("beat_002", "task_scheduler", risk=5),  # риск без claim — прочь (props не те — тоже прочь)
            self.scene("beat_002", risk=5, claims=["claim_001"], score_boost=2),  # риск с claim, score 15 → ниже 16
            self.scene("beat_003", "step_list", score_boost=2, start_word=220),  # проходит, 88 с
            self.scene("beat_004", score_boost=2),  # score 17, но 92.4 с — теснее 20 с к сцене beat_003
            self.scene("beat_009", score_boost=2),
            "мусор",
        ]}  # fmt: skip
        picked = storyboard.clean(raw, self.beats, self.claims, self.style, self.words)
        expected = [("beat_001", "chapter_title"), ("beat_003", "step_list")]
        self.assertEqual([(s["beat_id"], s["template_id"]) for s in picked], expected)
        self.assertEqual(picked[0]["duration_frames"], round(templates.MAX_S * 30))
        self.assertEqual(picked[1]["anchor_word"], 220)
        self.assertEqual(picked[1]["edit_start_frame"], self.words[220]["start_frame"])
        self.assertEqual(picked[0]["id"], "scene_001")
        self.assertEqual(len(picked[0]["hash"]), 16)
        self.assertEqual(picked[0]["layers"], ["presenter", "title", "captions"])
        self.assertEqual(schemas.validate_many("storyboard_scene", picked), [])

    def test_hash_edit_style_and_reanchor(self):
        raw = {"scenes": [self.scene("beat_001", score_boost=2)]}
        scene = storyboard.clean(raw, self.beats, self.claims, self.style, self.words)[0]
        same = storyboard.edit(scene, {"enabled": True}, 10_000)
        self.assertEqual(same["hash"], scene["hash"], "rationale и enabled в хэш не входят")
        moved = storyboard.edit(scene, {"edit_start_frame": 300}, 10_000)
        self.assertEqual(moved["hash"], scene["hash"], "сдвиг по времени не требует перерисовки")
        changed = storyboard.edit({**scene, "status": "rendered"}, {"props": {"title": "Другой"}}, 10_000)
        self.assertNotEqual(changed["hash"], scene["hash"])
        self.assertEqual(changed["status"], "proposed")
        other = storyboard.edit(scene, {"template_id": "quote", "props": {"text": "мысль"}}, 10_000)
        self.assertEqual((other["template_id"], other["visual_type"]), ("quote", "kinetic"))
        self.assertEqual(other["props"]["text"], "мысль")
        with self.assertRaises(storyboard.EditError):
            storyboard.edit(scene, {"duration_frames": 10_000}, 500)
        with self.assertRaises(storyboard.EditError):
            storyboard.edit(scene, {"template_id": "step_list", "props": {"steps": ["x"]}}, 10_000)
        restyled = storyboard.with_style([{**scene, "status": "rendered"}], {**self.style, "revision": "style_002"})
        self.assertNotEqual(restyled[0]["hash"], scene["hash"])
        self.assertEqual(restyled[0]["status"], "proposed")
        disabled = storyboard.edit(scene, {"enabled": False}, 10_000)
        self.assertEqual(disabled["status"], "disabled")
        # Якорь вырезали → следующее слово бита; весь бит вырезали → blocked.
        kept = [w for w in self.words if w["src"] not in (0, 1, 2)]
        again = storyboard.reanchor([scene], kept, self.beats)
        self.assertEqual(again[0]["edit_start_frame"], self.words[3]["start_frame"])
        gone = storyboard.reanchor([scene], [w for w in self.words if w["src"] > 70], self.beats)
        self.assertEqual((gone[0]["status"], gone[0]["enabled"]), ("blocked", False))
        self.assertEqual(storyboard.summary([scene, disabled])["enabled"], 1)

    def test_fake_storyboard_covers_different_templates(self):
        prompt = storyboard.prompt(self.beats, self.claims, self.style, "Обзор")
        self.assertTrue(prompt.startswith("### task: montage_storyboard"))
        self.assertIn("- task_scheduler", prompt)
        raw = fake_llm.story("montage_storyboard", prompt)
        picked = storyboard.clean(raw, self.beats, self.claims, self.style, self.words)
        self.assertGreaterEqual(len({s["template_id"] for s in picked}), 3, picked)
        with mock.patch.dict("os.environ", {"FAKE_LLM_STORY_RISK": "1"}):
            risky = fake_llm.story("montage_storyboard", prompt)
        self.assertEqual(risky["scenes"][0]["necessity"]["factual_risk"], 5)
        kept = storyboard.clean(risky, self.beats, self.claims, self.style, self.words)
        self.assertNotIn("beat_001", [s["beat_id"] for s in kept])


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class StoryApiTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        from tests.test_montage import make_video

        cls.media = tempfile.TemporaryDirectory()
        cls.head = Path(cls.media.name) / "head.mp4"
        make_video(cls.head, 150)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "montage" / "projects" / pid

    def settle(self, pid, statuses, timeout=240):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def prepared(self, **options) -> str:
        payload = {"title": "Каналы в Go", "options": {"check": False, **options}}
        status, project = self.json("POST", "/api/montage/projects", payload)
        self.assertEqual(status, 201, project)
        pid = project["id"]
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/sources/head", self.head.read_bytes())[0], 201)
        return pid

    def test_story_scenes_render_cache_and_invalidation(self):
        pid = self.prepared(mode="reviewed")
        docs_url = f"/api/montage/projects/{pid}/documents"
        note = "# Каналы\n\nБуфер два. противоречие тут.".encode()
        status, doc = self.json("POST", docs_url, note, headers={"X-File-Name": "konspekt.md"})
        self.assertEqual(status, 201, doc)
        self.assertEqual(doc["status"], "ready")
        self.assertEqual(self.json("POST", docs_url, b"", headers={"X-File-Name": "x.md"})[0], 400)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        self.settle(pid, ("review", "failed"))
        run_url = f"/api/montage/projects/{pid}/storyboard/run"
        self.assertEqual(self.json("POST", run_url)[0], 409, "план не подтверждён")
        self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "edl"})
        self.assertEqual(self.json("POST", run_url)[0], 200)
        waiting = self.settle(pid, ("waiting_approval", "failed"))
        self.assertEqual(waiting["status"], "waiting_approval", waiting)
        self.assertEqual([s["id"] for s in waiting["stages"]], ["timemap", "knowledge", "beats", "style", "storyboard"])
        _, view = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        self.assertGreaterEqual(len(view["knowledge"]["claims"]), 1)
        self.assertEqual(view["knowledge"]["claims"][0]["citations"][0]["doc"], "doc_1")
        self.assertEqual(len(view["flagged"]), len(view["knowledge"]["claims"]), "фрагмент с «противоречие»")
        self.assertGreaterEqual(len(view["beats"]["beats"]), 3)
        self.assertEqual(view["style"]["revision"], "style_001")
        templates_used = {s["template_id"] for s in view["scenes"]}
        self.assertGreaterEqual(len(templates_used), 3, view["summary"])
        self.assertTrue(all(s["status"] == "proposed" for s in view["scenes"]))
        folder = self.folder(pid)
        for name in ("knowledge.json", "beats.json", "style_bible.json", "storyboard.json", "transcript_edit.json"):
            self.assertTrue((folder / name).is_file(), name)
        # Сборка без подтверждения раскадровки — отказ; правка сцены и подтверждение.
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 409)
        first = view["scenes"][0]
        board_url = f"/api/montage/projects/{pid}/storyboard"
        patch = {"scenes": [{"id": first["id"], "props": {**first["props"], "title": "Правка"}}]}
        status, saved = self.json("POST", board_url, patch)
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.json("POST", board_url, {"scenes": [{"id": first["id"], "enabled": "да"}]})[0], 400)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "storyboard"})[0], 200)
        self.assertEqual(self.json("POST", f"/api/montage/projects/{pid}/render")[0], 200)
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        self.assertIn("scenes", [s["id"] for s in done["stages"]])
        self.assertGreaterEqual(done["motion"]["by_status"].get("rendered", 0), 3, done["motion"])
        _, view2 = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        rendered = [s for s in view2["scenes"] if s["status"] == "rendered"]
        movs = {s["id"]: folder / "scenes" / s["id"] / f"{s['hash']}.mov" for s in rendered}
        self.assertTrue(all(p.is_file() for p in movs.values()))
        scene_url = f"/api/montage/projects/{pid}/scenes/{rendered[0]['id']}"
        status, headers, _ = self.call("GET", f"{scene_url}/thumb.png")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(self.call("GET", f"{scene_url}/index.html")[0], 200)
        self.assertEqual(self.json("GET", f"/api/montage/projects/{pid}/scenes/scene_999/thumb.png")[0], 404)
        # Повторная сборка: ни одна сцена не перерисована (хэш-кэш, §11).
        before = {k: p.stat().st_mtime_ns for k, p in movs.items()}
        self.json("POST", f"/api/montage/projects/{pid}/render")
        self.assertEqual(self.settle(pid, ("done", "failed"))["status"], "done")
        self.assertEqual({k: p.stat().st_mtime_ns for k, p in movs.items()}, before)
        # Правка одной сцены → новый файл только у неё; остальные на месте.
        target = rendered[1]
        key = "title" if "title" in target["props"] else "text"
        patch = {"scenes": [{"id": target["id"], "props": {**target["props"], key: "Ещё"}}]}
        _, saved = self.json("POST", board_url, patch)
        self.assertFalse(saved["gates"]["storyboard"]["approved"], "правка снимает подтверждение")
        self.json("POST", f"/api/montage/projects/{pid}/approve", {"gate": "storyboard"})
        self.json("POST", f"/api/montage/projects/{pid}/render")
        self.assertEqual(self.settle(pid, ("done", "failed"))["status"], "done")
        _, view3 = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        new_target = next(s for s in view3["scenes"] if s["id"] == target["id"])
        self.assertNotEqual(new_target["hash"], target["hash"])
        self.assertTrue((folder / "scenes" / target["id"] / f"{new_target['hash']}.mov").is_file())
        self.assertFalse(movs[target["id"]].is_file(), "старая версия сцены убрана")
        untouched = {k: p.stat().st_mtime_ns for k, p in movs.items() if k != target["id"]}
        self.assertEqual(untouched, {k: v for k, v in before.items() if k != target["id"]})
        # Правка стиля → новая ревизия → все сцены снова proposed; правка плана → раскадровка сброшена.
        new_style = {"name": "Светлый", "palette": {"accent": "#ff0000"}}
        status, restyled = self.json("POST", f"/api/montage/projects/{pid}/style", new_style)
        self.assertEqual(status, 200, restyled)
        self.assertEqual(restyled["style"]["revision"], "style_002")
        _, view4 = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        self.assertTrue(all(s["status"] == "proposed" for s in view4["scenes"] if s["enabled"]))
        _, plan = self.json("GET", f"/api/montage/projects/{pid}/plan")
        self.json("POST", f"/api/montage/projects/{pid}/plan", {"cuts": plan["cuts"][1:], "layout": plan["layout"]})
        self.assertFalse((folder / "storyboard.json").exists())
        self.assertTrue(any(folder.glob("scenes/*/*.mov")), "кэш сцен цел")
        _, log = self.json("GET", f"/api/montage/projects/{pid}/decisions")
        actions = [d["action"] for d in log["decisions"]]
        for role in ("research:llm", "narrative:llm", "art_director:llm", "storyboard:llm", "approve:storyboard",
                     "storyboard:edit", "style:edit"):  # fmt: skip
            self.assertIn(role, actions)

    def test_autonomous_with_graphics_goes_through_storyboard(self):
        pid = self.prepared(mode="autonomous", motion=True)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        ids = [s["id"] for s in done["stages"]]
        self.assertEqual(ids[:4], ["audio", "silences", "transcribe", "plan"])
        self.assertTrue({"knowledge", "beats", "style", "storyboard", "scenes"} <= set(ids), ids)
        self.assertTrue(done["gates"]["storyboard"]["auto"])
        self.assertGreaterEqual(done["motion"]["by_status"].get("rendered", 0), 3, done["motion"])

    def test_redo_scene_and_without_hyperframes_scenes_are_blocked(self):
        pid = self.prepared(mode="autonomous", motion=True)
        self.json("POST", f"/api/montage/projects/{pid}/start")
        self.assertEqual(self.settle(pid, ("done", "failed"))["status"], "done")
        _, view = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        sid = view["scenes"][0]["id"]
        scenes_url = f"/api/montage/projects/{pid}/scenes"
        self.assertEqual(self.json("POST", f"{scenes_url}/scene_999/redo", {"text": "x"})[0], 409)
        status, body = self.json("POST", f"{scenes_url}/{sid}/redo", {"text": "Сделай список"})
        self.assertEqual(status, 200, body)
        redone = self.settle(pid, ("done", "waiting_approval", "failed"))
        self.assertEqual(redone["status"], "done", "autonomous подтверждает сам")
        _, again = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        self.assertEqual([s["id"] for s in again["scenes"]], [s["id"] for s in view["scenes"]])
        # Без HyperFrames сцены blocked, финал собирается, подделок нет.
        module = self.app.modules["montage"]
        with mock.patch.object(type(module.config), "hyperframes_available", lambda self: False):
            self.json("POST", f"/api/montage/projects/{pid}/render")
            done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done)
        self.assertIn("HyperFrames", done["motion"].get("skipped", ""))
        _, blocked = self.json("GET", f"/api/montage/projects/{pid}/storyboard")
        self.assertTrue(all(s["status"] == "blocked" for s in blocked["scenes"] if s["enabled"]))
        self.assertTrue(all(re.match(r"^scene_\d{3}$", s["id"]) for s in blocked["scenes"]))


if __name__ == "__main__":
    unittest.main()
