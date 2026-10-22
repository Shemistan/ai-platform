"""Динамические слайды и ритм: расписание появления, сборка каждого вида, кадр стоит после появления."""

import dataclasses
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from aiplatform.config import default_font
from aiplatform.fakes import fake_gen
from aiplatform.studio import motion, render
from aiplatform.studio.render import Renderer

NEEDS_FFMPEG = unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")
CALM_S = 5.0  # всё показано — дальше кадр стоит: движение только при появлении (PRD-031)
# Разница кадров 32×18, ниже которой кадр «стоит»: ключевой кадр x264 раз в 10 с даёт ~110, наезд — 170–420.
NOISE = 150

SCENES = {
    "bullets": ("bullets", {}, ["Первый пункт", "Второй пункт", "Третий пункт"]),
    "stat": ("stat", {"value": 42.5, "prefix": "", "suffix": "%", "label": "доля сервисов"}, []),
    "bar": (
        "chart",
        {"chart": "bar", "unit": "мс", "data": [{"label": "a", "value": 10}, {"label": "b", "value": 4}]},
        [],
    ),
    "line": (
        "chart",
        {"chart": "line", "unit": "", "data": [{"label": str(y), "value": v} for y, v in ((1, 1), (2, 3), (3, 2))]},
        [],
    ),
    "flow": ("flow", {"steps": ["Раз", "Два", "Три"]}, []),
    "compare": (
        "compare",
        {"left": {"title": "Было", "items": ["медленно"]}, "right": {"title": "Стало", "items": ["быстро"]}},
        [],
    ),
    "quote": ("quote", {"text": "Лучшая пауза — та, которой нет.", "author": "Автор"}, []),
    "title": ("bullets", {}, []),
    "chapter": ("chapter", {"number": 2, "subtitle": "Как брокер теряет сообщения"}, []),
    "code": ("code", {"code": 'func main() {\n\tfmt.Println("привет")\n}\n// 100% покрытие', "language": "go"}, []),
    "summary": ("summary", {"items": ["Брокер — не база", "Сообщения повторяются", "Идемпотентность"]}, []),
}


class ScheduleTest(unittest.TestCase):
    def test_reveal_spreads_over_slide_but_not_slower_than_beat(self):
        self.assertEqual(motion.reveal_times(3, 9.0), [0.5, 4.25, 8.0])
        long = motion.reveal_times(3, 30.0)
        self.assertEqual([round(b - a, 3) for a, b in zip(long, long[1:], strict=False)], [motion.BEAT_S] * 2)
        self.assertEqual(motion.reveal_times(1, 10.0), [motion.START_S])
        self.assertEqual(motion.reveal_times(0, 10.0), [])
        # Короткий слайд: всё появляется до конца, пусть и чаще.
        short = motion.reveal_times(4, 2.0)
        self.assertLess(short[-1], 2.0)

    def test_numbers_in_russian(self):
        self.assertEqual(motion.number(1234567.5, 1), "1 234 567,5")
        self.assertEqual(motion.number(-3, 0), "−3")
        self.assertEqual(motion.decimals(12.50), 1)
        self.assertEqual(motion.decimals(7.0), 0)

    def test_image_zooms_in_once_then_stands(self):
        moving = render._zoompan(500, 320, 180, animate=True)
        self.assertIn(f"min(1,on/{round(render.ENTER_S * render.FPS)})", moving)
        self.assertNotIn("iw-iw/zoom", moving, "панорамы нет")
        self.assertIn("z='1'", render._zoompan(500, 320, 180, animate=False))

    def test_still_canvas_draws_no_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = motion.Canvas(w=1920, h=1080, margin=96, font="", work=Path(tmp), top=100, bottom=1000, animate=False)
            data = {"chart": "bar", "data": [{"label": "a", "value": 10}, {"label": "b", "value": 4}]}
            motion.draw(c, {"kind": "chart", "title": "График", **data}, 10.0)
            boxes = sum("drawbox" in p and "fill" in p for p in c.parts)
            self.assertEqual(boxes, 4, "полоска заголовка, ось и два столбца — без ступеней роста")
            self.assertFalse(any("pow(1-" in p for p in c.parts), "текст не выезжает")


def still_spans(video: Path, ffmpeg: str = "ffmpeg") -> list[float]:
    """Сколько секунд подряд кадр не меняется: ролик в 32×18 серым, 10 кадров в секунду."""
    raw = subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            "fps=10,scale=32:18,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    size = 32 * 18
    frames = [raw[i : i + size] for i in range(0, len(raw) - size + 1, size)]
    spans, run = [], 0
    for a, b in zip(frames, frames[1:], strict=False):
        if sum(abs(x - y) for x, y in zip(a, b, strict=True)) > NOISE:
            spans.append(run / 10)
            run = 0
        else:
            run += 1
    return spans + [run / 10]


@NEEDS_FFMPEG
class RhythmTest(unittest.TestCase):
    seconds = 20.0

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.renderer = Renderer(ffmpeg="ffmpeg", font=default_font(), scale=0.25)

    def render(
        self,
        name: str,
        kind: str,
        data: dict,
        bullets: list[str],
        orientation: str = render.LANDSCAPE,
        image=None,
        animate: bool = True,
    ):
        out = self.dir / f"{name}-{orientation}-{animate}.mp4"
        dataclasses.replace(self.renderer, animate=animate).slide_clip(
            title=f"Слайд {name}",
            bullets=bullets,
            image=image,
            out=out,
            seconds=self.seconds,
            orientation=orientation,
            cancel=threading.Event(),
            kind=kind,
            data=data,
            notes=["Пометка внизу кадра"],
        )
        return out

    def check_rhythm(self, name: str, orientation: str):
        kind, data, bullets = SCENES[name]
        video = self.render(name, kind, data, bullets, orientation)
        self.assertGreaterEqual(still_spans(video)[-1], CALM_S, "после появления всех элементов кадр стоит")

    def test_progress_bar_with_chapter(self):
        out = self.dir / "progress.mp4"
        self.renderer.slide_clip(
            title="Слайд",
            bullets=["Пункт"],
            image=None,
            out=out,
            seconds=4.0,
            orientation=render.LANDSCAPE,
            cancel=threading.Event(),
            progress=render.Progress(10.0, 14.0, 40.0, "Глава про очереди"),
        )
        self.assertTrue(out.is_file())
        self.assertEqual((out.with_suffix("") / "chapter.txt").read_text(encoding="utf-8"), "ГЛАВА ПРО ОЧЕРЕДИ")

    def test_image_slide_zooms_only_on_enter(self):
        image = self.dir / "bg.png"
        fake_gen.write_png(str(image), 320, 180, 3)
        spans = still_spans(self.render("image", "image", {}, ["Тезис"], image=image))
        self.assertGreaterEqual(spans[-1], self.seconds - 3.0, "наезд и тезисы — в первые секунды, дальше кадр стоит")

    def test_image_slide_without_motion_stands(self):
        image = self.dir / "bg.png"
        fake_gen.write_png(str(image), 320, 180, 3)
        spans = still_spans(self.render("image", "image", {}, ["Тезис"], image=image, animate=False))
        self.assertGreaterEqual(sum(spans), self.seconds - 2.0, "двигаются только проявления текста")

    def test_scene_without_motion_still_renders(self):
        kind, data, bullets = SCENES["stat"]
        spans = still_spans(self.render("stat", kind, data, bullets, animate=False))
        self.assertGreaterEqual(spans[-1], CALM_S)


# Каждый вид слайда — отдельный тест, а не subTest в одном: параллельный прогон (QA-016)
# раскладывает их по воркерам, а не держит 22 рендера в одном.
def _rhythm_test(name: str, orientation: str):
    def test(self):
        self.check_rhythm(name, orientation)

    test.__doc__ = f"Слайд «{name}» ({orientation}): всё показано — последние {CALM_S:g} с кадр стоит."
    return test


for _name in SCENES:
    for _orientation in (render.LANDSCAPE, render.VERTICAL):
        setattr(RhythmTest, f"test_rhythm_{_name}_{_orientation}", _rhythm_test(_name, _orientation))

if __name__ == "__main__":
    unittest.main()
