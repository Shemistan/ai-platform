import json
import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf

from narrator.script import Block
from narrator.synth import (
    MANIFEST,
    PAUSE_AFTER_HEADING,
    PAUSE_BEFORE_HEADING,
    PAUSE_PARAGRAPH,
    ESpeechSynth,
    Settings,
    SupertonicSynth,
    Synthesizer,
    clip_key,
    encode_mp3,
    make_synth,
    pauses,
    render,
    retime_mp3,
)

SR = 1000


class FakeSynth(Synthesizer):
    """Вместо модели — тон длиной 10 мс на символ; считает вызовы."""

    def __init__(self):
        super().__init__(Settings(), accentor=lambda t: t)
        self.calls: list[str] = []

    def clip(self, block, on_part=None):
        self.calls.append(block.text)
        return np.full(len(block.text) * 10, 0.1, dtype=np.float32), SR


BLOCKS = [
    Block("heading", "Раздел", 1),
    Block("para", "Первый.", 3),
    Block("para", "Второй.", 5),
    Block("heading", "Ещё", 7),
]


def test_pauses():
    assert pauses(BLOCKS) == [0.0, PAUSE_AFTER_HEADING, PAUSE_PARAGRAPH, PAUSE_BEFORE_HEADING]


def test_clip_key_depends_on_text_and_settings():
    b = Block("para", "Текст.", 1)
    assert clip_key(b, Settings()) == clip_key(Block("para", "Текст.", 9), Settings())
    assert clip_key(b, Settings()) != clip_key(Block("para", "Текст!", 1), Settings())
    assert clip_key(b, Settings()) != clip_key(b, Settings(voice="F2"))


def test_clip_key_depends_on_engine():
    b = Block("para", "Текст.", 1)
    assert clip_key(b, Settings(engine="supertonic")) != clip_key(b, Settings(engine="espeech"))


def test_engine_defaults():
    assert Settings(engine="espeech").effective_steps == 32
    assert Settings(engine="supertonic").effective_speed == 1.05
    assert Settings(engine="espeech", speed=1.2).effective_speed == 1.2


def test_make_synth():
    assert isinstance(make_synth(Settings(engine="espeech")), ESpeechSynth)
    assert isinstance(make_synth(Settings(engine="supertonic")), SupertonicSynth)
    with pytest.raises(ValueError, match="движок"):
        make_synth(Settings(engine="nope"))


def test_unknown_voice_rejected():
    with pytest.raises(ValueError, match="голос"):
        SupertonicSynth(Settings(voice="X9"), tts=object())


def test_supertonic_gets_acutes():
    class FakeTTS:
        sample_rate = SR

        def get_voice_style(self, v):
            return v

        def synthesize(self, text, **kw):
            self.text = text
            return np.zeros((1, 10), dtype=np.float32), None

    tts = FakeTTS()
    synth = SupertonicSynth(Settings(), accentor=lambda t: "з+амок", tts=tts)
    synth.clip(Block("para", "замок", 1))
    assert tts.text == "за\u0301мок"


def test_render_concatenates_with_pauses_and_caches(tmp_path):
    synth = FakeSynth()
    out = tmp_path / "a.wav"
    dur = render(BLOCKS, out, tmp_path / "cache", synth, log=lambda m: None)
    speech = sum(len(b.text) * 10 for b in BLOCKS)
    silence = sum(int(p * SR) for p in pauses(BLOCKS))
    assert dur == pytest.approx((speech + silence) / SR)
    assert sf.info(out).frames == speech + silence
    manifest = json.loads((tmp_path / "cache" / MANIFEST).read_text(encoding="utf-8"))
    assert [m["line"] for m in manifest] == [1, 3, 5, 7]
    assert len(synth.calls) == 4

    # Правка одного абзаца — заново озвучивается только он.
    edited = [*BLOCKS[:2], Block("para", "Второй, исправленный.", 5), BLOCKS[3]]
    synth2 = FakeSynth()
    render(edited, out, tmp_path / "cache", synth2, log=lambda m: None)
    assert synth2.calls == ["Второй, исправленный."]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нет ffmpeg")
def test_encode_mp3(tmp_path):
    wav = tmp_path / "a.wav"
    sf.write(wav, np.zeros(44100, dtype=np.float32), 44100)
    mp3 = tmp_path / "a.mp3"
    encode_mp3(wav, mp3, "Заголовок")
    assert mp3.stat().st_size > 0
    assert not (tmp_path / "a.tmp.mp3").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нет ffmpeg")
def test_retime_mp3_halves_duration_and_keeps_title(tmp_path):
    wav = tmp_path / "a.wav"
    sf.write(wav, np.zeros(4 * 24000, dtype=np.float32), 24000)
    encode_mp3(wav, tmp_path / "a.mp3", "Заголовок")
    retime_mp3(tmp_path / "a.mp3", tmp_path / "fast.mp3", 2.0)
    probe = ["ffprobe", "-v", "error", "-of", "default=nw=1:nk=1", "-show_entries"]
    seconds, title = subprocess.run(
        [*probe, "format=duration:format_tags=title", str(tmp_path / "fast.mp3")],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    assert float(seconds) == pytest.approx(2.0, abs=0.15) and title == "Заголовок"
    assert not (tmp_path / "fast.tmp.mp3").exists()


def test_crossfade():
    from narrator.synth import crossfade

    a = np.ones(10, np.float32)
    b = np.full(10, 3.0, np.float32)
    out = crossfade([a, b], 4)
    assert len(out) == 16  # 10 + 10 - 4 перекрытия
    assert out[0] == 1 and out[-1] == 3
    assert 1 <= out[7] <= 3  # середина перехода
    assert len(crossfade([a], 4)) == 10
    assert len(crossfade([a, np.zeros(0, np.float32)], 4)) == 10


def test_progress_counts_every_char(monkeypatch):
    from narrator import synth as synth_mod

    advanced = []

    class Spy(synth_mod.Progress):
        def advance(self, chars):
            advanced.append(chars)

    monkeypatch.setattr(synth_mod, "Progress", Spy)

    class PartSynth(FakeSynth):
        def clip(self, block, on_part=None):
            on_part(0.5)
            return super().clip(block)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        render(BLOCKS, Path(d) / "a.wav", Path(d) / "c", PartSynth(), log=lambda m: None)
    assert sum(advanced) == pytest.approx(sum(len(b.text) for b in BLOCKS))


def test_sentence_groups():
    from narrator.synth import sentence_groups

    text = "Раз два. Три четыре! Пять шесть? Семь."
    assert sentence_groups(text, 20) == ["Раз два. Три четыре!", "Пять шесть? Семь."]
    assert sentence_groups("Очень длинное предложение без точки", 5) == [
        "Очень длинное предложение без точки"
    ]
    assert sentence_groups(text, 1000) == [text]


def test_voices_per_engine():
    from narrator.synth import SileroSynth

    assert isinstance(make_synth(Settings(engine="silero")), SileroSynth)
    assert Settings(engine="silero").effective_voice == "eugene"
    assert Settings(engine="supertonic").effective_voice == "M1"
    with pytest.raises(ValueError, match="голос"):
        make_synth(Settings(engine="silero", voice="M1"))
    b = Block("para", "Текст.", 1)
    assert clip_key(b, Settings(engine="silero")) != clip_key(
        b, Settings(engine="silero", voice="aidar")
    )
