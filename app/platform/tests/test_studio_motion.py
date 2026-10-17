"""Динамические слайды и ритм: расписание появления, планы картинки, сборка каждого вида."""

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
MAX_STILL_S = 5.0  # дольше кадр стоять не должен — требование ритма 3–5 с

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

    def test_beats_fill_the_rest_of_long_slide(self):
        self.assertEqual(
            motion.beats(8.5, 30.0, 3),
            [(0, 12.5, 16.5), (1, 16.5, 20.5), (2, 20.5, 24.5), (0, 24.5, 28.5), (1, 28.5, 30.0)],
        )
        self.assertEqual(motion.beats(1.0, 4.0, 2), [], "короткому слайду подсветка не нужна")

    def test_numbers_in_russian(self):
        self.assertEqual(motion.number(1234567.5, 1), "1 234 567,5")
        self.assertEqual(motion.number(-3, 0), "−3")
        self.assertEqual(motion.decimals(12.50), 1)
        self.assertEqual(motion.decimals(7.0), 0)

    def test_image_slide_is_cut_into_shots(self):
        self.assertEqual(render.shots(4.0), [100])
        self.assertEqual(render.shots(5.0), [125])
        self.assertEqual(render.shots(12.0), [100, 100, 100])
        self.assertTrue(all(f / render.FPS <= render.SHOT_MAX_S for f in render.shots(31.0)))
        self.assertEqual(sum(render.shots(31.0)), 31 * render.FPS)


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
        if sum(abs(x - y) for x, y in zip(a, b, strict=True)) > 20:
            spans.append(run / 10)
            run = 0
        else:
            run += 1
    return spans + [run / 10]


@NEEDS_FFMPEG
class RhythmTest(unittest.TestCase):
    seconds = 16.0

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.renderer = Renderer(ffmpeg="ffmpeg", font=default_font(), scale=0.25)

    def render(
        self, name: str, kind: str, data: dict, bullets: list[str], orientation: str = render.LANDSCAPE, image=None
    ):
        out = self.dir / f"{name}-{orientation}.mp4"
        self.renderer.slide_clip(
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
        self.assertLessEqual(max(still_spans(video)), MAX_STILL_S)

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

    def test_image_slide_changes_shot(self):
        image = self.dir / "bg.png"
        fake_gen.write_png(str(image), 320, 180, 3)
        video = self.render("image", "image", {}, ["Тезис"], image=image)
        self.assertLessEqual(max(still_spans(video)), MAX_STILL_S)


# Каждый вид слайда — отдельный тест, а не subTest в одном: параллельный прогон (QA-016)
# раскладывает их по воркерам, а не держит 22 рендера в одном.
def _rhythm_test(name: str, orientation: str):
    def test(self):
        self.check_rhythm(name, orientation)

    test.__doc__ = f"Слайд «{name}» ({orientation}) меняется не реже раза в {MAX_STILL_S:g} с."
    return test


for _name in SCENES:
    for _orientation in (render.LANDSCAPE, render.VERTICAL):
        setattr(RhythmTest, f"test_rhythm_{_name}_{_orientation}", _rhythm_test(_name, _orientation))

if __name__ == "__main__":
    unittest.main()
