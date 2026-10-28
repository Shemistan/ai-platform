"""Озвучка script.txt локально, без сети: движки ESpeech-TTS-1, Silero v5.5, Supertonic 3.

Каждый блок (абзац или заголовок) синтезируется отдельно и кэшируется в
.cache/<sha1>.wav: после правки текста заново озвучиваются только изменённые
блоки. Клипы склеиваются потоково с паузами, итог — mp3 через ffmpeg.

Движки (сравнение на прослушивании — спека, раздел «Ударения и движки»):
- espeech — ESpeech-TTS-1 RL-V2 (F5-TTS, обучен на русском с «+» перед
  ударной гласной), голос клонируется с образца Charon. Лучшее качество, но на
  CPU в 15 раз медленнее реального времени: запускать там, где есть
  видеокарта (MPS на Mac, CUDA). В sandbox агента видеокарты нет.
- silero — Silero TTS v5.5, русская модель со своими ударениями и
  омографами. Около 37× быстрее реального времени на CPU. Лицензия CC BY-NC.
- supertonic — Supertonic 3, голос M1, ударения акутами. Около 5× быстрее
  реального времени на CPU.
- higgs, fish, moss, qwen3, chatterbox — большие модели на mlx-audio для
  сравнения, только Mac на Apple Silicon; см. narrator.mlx_engines.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from narrator import mlx_engines, models
from narrator.script import Block
from narrator.stress import ACCENT_VERSION, plus_to_acute

MLX_ENGINES = tuple(mlx_engines.MLX_ENGINES)
ENGINES = ("espeech", "silero", "supertonic", *MLX_ENGINES)
# Голос этих движков — клон образца (CLONE_VOICES), по умолчанию Charon.
CLONE_ENGINES = ("espeech", *MLX_ENGINES)
# Движки, у которых настраиваются шаги и скорость.
TUNABLE_ENGINES = ("espeech", "supertonic")
DEFAULT_ENGINE = "espeech"

SUPERTONIC_MODEL = "supertonic-3"
# Мужской M1 выбран на прослушивании против F2.
DEFAULT_VOICE = "M1"
VOICES = [f"{g}{n}" for g in "FM" for n in range(1, 6)]

SILERO_MODEL = "v5_5_ru"
# sha256 файла v5_5_ru.pt (models.silero.ai): torch.package исполняет код из файла,
# подменённый файл не грузим (SEC-011). Та же сумма — в tools/fetch-models.sh.
SILERO_SHA256 = "50081637b602126ee06cb3bc8a744d25651d2da149ee8864b9a379bfdd934437"
SILERO_VOICES = ["eugene", "aidar", "baya", "kseniya", "xenia"]
SILERO_SR = 48000
# Silero не берёт текст длиннее ~1000 символов за вызов: абзац режем по
# предложениям на группы с запасом (+ удлиняют текст).
SILERO_MAX_CHARS = 700

# Образцы для клонирования: голос → имя файлов <имя>.wav и <имя>.txt.
# В пакете (voices/) — только синтетические: M1 из Supertonic и Charon —
# голос Gemini TTS, отрывок дайджеста за 9 октября 2026; другой образец
# записывает `./narrate voice charon` в <папка моделей>/voices/, он главнее.
# Shem, Shem-v2 и Shem-v3 — три записи живого голоса человека: это личные
# данные, в git их нет (CMP-009). Они лежат только в <папка моделей>/voices/
# рядом с записью о согласии `<имя>.consent.json` (consent.py); без неё клон
# не строится.
CLONE_VOICES = {
    "M1": "m1",
    "Charon": "charon",
    "Shem": "shem",
    "Shem-v2": "shem-v2",
    "Shem-v3": "shem-v3",
}
# Голоса, которые есть в Gemini TTS: их образец можно записать по ключу.
GEMINI_VOICES = ("Charon",)

ENGINE_VOICES = {
    "supertonic": VOICES,
    "silero": SILERO_VOICES,
    **{e: list(CLONE_VOICES) for e in CLONE_ENGINES},
}
# У клонов по умолчанию Charon: им же Gemini TTS озвучивает дайджест.
DEFAULT_VOICES = {"supertonic": DEFAULT_VOICE, "silero": "eugene"} | {
    e: "Charon" for e in CLONE_ENGINES
}

ESPEECH_MODEL = "ESpeech-TTS-1_RL-V2"
ESPEECH_CFG = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
# Образец голоса для клонирования: M1 из Supertonic с ударениями RUAccent.
VOICE_DIR = Path(__file__).parent / "voices"
REF_WAV = VOICE_DIR / "m1.wav"
REF_TEXT = VOICE_DIR / "m1.txt"
# Образец вместе с новым текстом ESpeech считает в окне 22 с: длинный образец
# не оставит места тексту.
REF_SECONDS = (3.0, 12.0)

# Шаги и скорость по умолчанию для каждого движка.
ENGINE_DEFAULTS = {"supertonic": (8, 1.05), "espeech": (32, 1.0), "silero": (0, 1.0)} | {
    e: (0, 1.0) for e in MLX_ENGINES
}

# Паузы, секунды. Внутри блока паузы между кусками ставит сама модель.
PAUSE_PARAGRAPH = 0.7
PAUSE_BEFORE_HEADING = 1.2
PAUSE_AFTER_HEADING = 0.6
PAUSE_INNER = 0.25

MANIFEST = "manifest.json"


class ModelMissingError(RuntimeError):
    pass


class ConsentMissingError(ModelMissingError):
    """Образец живого голоса есть, а записи о согласии владельца нет (CMP-009)."""


@dataclass(frozen=True)
class Settings:
    engine: str = "supertonic"
    voice: str | None = None
    speed: float | None = None
    steps: int | None = None

    @property
    def effective_voice(self) -> str:
        return self.voice or DEFAULT_VOICES[self.engine]

    @property
    def effective_steps(self) -> int:
        return self.steps or ENGINE_DEFAULTS[self.engine][0]

    @property
    def effective_speed(self) -> float:
        return self.speed or ENGINE_DEFAULTS[self.engine][1]


def voices_dir() -> Path:
    """Куда пишутся образцы голоса, которых нет в пакете."""
    return models.models_root() / models.VOICES


def ref_sample(voice: str) -> tuple[Path, Path]:
    """Образец голоса для клонирования: wav и его текст с «+» перед ударными.

    Живой голос — только с записью о согласии рядом (CMP-009): иначе
    ConsentMissingError, синтез не начнётся.
    """
    from narrator import consent

    stem = CLONE_VOICES[voice]
    # Перезаписанный человеком образец главнее того, что в пакете.
    for d in (voices_dir(), VOICE_DIR):
        wav, text = d / f"{stem}.wav", d / f"{stem}.txt"
        if wav.exists() and text.exists():
            try:
                consent.check(stem, wav)
            except consent.ConsentError as e:
                raise ConsentMissingError(str(e)) from e
            return wav, text
    if stem not in consent.SYNTHETIC:
        raise ModelMissingError(
            f"нет образца голоса {voice} в {voices_dir()}: живой голос в пакете не лежит "
            f"(CMP-009) — положи туда {stem}.wav и {stem}.txt или запиши: ./narrate voice "
            f"{stem} --from <файл> --text «…» --owner «чей голос» --form self|written "
            f"({consent.DOC})"
        )
    raise ModelMissingError(
        f"нет образца голоса {voice} в {voices_dir()}: запиши его один раз вне sandbox — "
        f"./narrate voice {stem}"
    )


def has_voice(engine: str, voice: str) -> bool:
    """Голос можно выбрать: у клона образец уже записан."""
    if engine not in CLONE_ENGINES:
        return True
    try:
        ref_sample(voice)
    except ModelMissingError:
        return False
    return True


def clip_key(block: Block, s: Settings) -> str:
    voice = s.effective_voice
    if s.engine in CLONE_ENGINES:
        voice = f"{CLONE_VOICES.get(voice, voice)}.wav"
    raw = (
        f"{s.engine}|{ACCENT_VERSION}|{voice}|{s.effective_speed}|{s.effective_steps}"
        f"|{block.kind}|{block.text}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def pauses(blocks: list[Block]) -> list[float]:
    """Тишина перед каждым блоком."""
    out: list[float] = []
    for i, b in enumerate(blocks):
        if i == 0:
            out.append(0.0)
        elif b.kind == "heading":
            out.append(PAUSE_BEFORE_HEADING)
        elif blocks[i - 1].kind == "heading":
            out.append(PAUSE_AFTER_HEADING)
        else:
            out.append(PAUSE_PARAGRAPH)
    return out


def block_starts(blocks: list[Block], clips: list[Path]) -> list[float]:
    """С какой секунды склеенного render звука начинается каждый блок."""
    starts, t = [], 0.0
    for pause, path in zip(pauses(blocks), clips, strict=True):
        t += pause
        starts.append(round(t, 3))
        info = sf.info(path)
        t += info.frames / info.samplerate
    return starts


def supertonic_dir() -> Path:
    return models.locate(models.SUPERTONIC, "~/.cache/supertonic3", "SUPERTONIC_CACHE_DIR")


def load_supertonic():
    """Модель из папки моделей (или $SUPERTONIC_CACHE_DIR), без скачивания."""
    os.environ["SUPERTONIC_CACHE_DIR"] = str(supertonic_dir())
    from supertonic import TTS

    try:
        return TTS(model=SUPERTONIC_MODEL, auto_download=False)
    except Exception as e:  # у supertonic нет своего типа ошибки «нет модели»
        raise ModelMissingError(
            f"модель {SUPERTONIC_MODEL} не загружена из {supertonic_dir()} ({e}): "
            f"{models.missing_hint()}"
        ) from e


def download_model() -> Path:
    os.environ["SUPERTONIC_CACHE_DIR"] = str(supertonic_dir())
    from supertonic import TTS

    return Path(TTS(model=SUPERTONIC_MODEL, auto_download=True).model_dir)


def espeech_dir() -> Path:
    return models.locate(
        models.ESPEECH, f"~/.cache/espeech/{ESPEECH_MODEL}", "NARRATOR_ESPEECH_DIR"
    )


def vocos_dir() -> Path:
    """Вокодер ESpeech; папки нет — f5_tts возьмёт его из кэша HuggingFace."""
    return models.locate(models.VOCOS, env="NARRATOR_VOCOS_DIR")


class Synthesizer:
    """Общее: ударения через RUAccent (лениво), озвучка блока — в наследниках."""

    def __init__(self, settings: Settings, accentor=None):
        voices = ENGINE_VOICES.get(settings.engine, [])
        if settings.effective_voice not in voices:
            raise ValueError(
                f"голос {settings.effective_voice!r} у {settings.engine} неизвестен, "
                f"есть: {', '.join(voices)}"
            )
        self.settings = settings
        self._accentor = accentor

    def accent(self, text: str) -> str:
        """Текст блока с «+» перед ударными гласными."""
        if self._accentor is None:
            from narrator.stress import Accentor

            self._accentor = Accentor()
        return self._accentor(text)

    def check_chars(self, blocks: list[Block]) -> list[str]:
        return []

    def prepare(self) -> None:
        """Загрузить модель заранее, чтобы её сообщения не ломали полосу прогресса."""

    def clip(self, block: Block, on_part=None) -> tuple[np.ndarray, int]:
        """Озвучить блок; on_part(доля) — если движок умеет сообщать прогресс внутри блока."""
        raise NotImplementedError

    def close(self) -> None:
        """Освободить модель: у движков-воркеров это отдельный процесс."""


class SupertonicSynth(Synthesizer):
    def __init__(self, settings: Settings, accentor=None, tts=None):
        super().__init__(settings, accentor)
        self._tts = tts

    @property
    def tts(self):
        if self._tts is None:
            self._tts = load_supertonic()
        return self._tts

    def check_chars(self, blocks: list[Block]) -> list[str]:
        ok, bad = self.tts.model.text_processor.validate_text("".join(b.text for b in blocks))
        return [] if ok else bad

    def prepare(self) -> None:
        _ = self.tts

    def clip(self, block: Block, on_part=None) -> tuple[np.ndarray, int]:
        s = self.settings
        wav, _ = self.tts.synthesize(
            plus_to_acute(self.accent(block.text)),
            voice_style=self.tts.get_voice_style(s.effective_voice),
            total_steps=s.effective_steps,
            speed=s.effective_speed,
            silence_duration=PAUSE_INNER,
            lang="ru",
        )
        return wav.squeeze().astype(np.float32), self.tts.sample_rate


class ESpeechSynth(Synthesizer):
    def __init__(self, settings: Settings, accentor=None):
        super().__init__(settings, accentor)
        self._model = None

    def _load(self):
        if self._model is not None:
            return
        d = espeech_dir()
        if not (d / "espeech_tts_rlv2.pt").exists():
            raise ModelMissingError(f"нет модели ESpeech в {d}: {models.missing_hint()}")
        ref_sample(self.settings.effective_voice)  # нет образца — скажем до загрузки модели
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from f5_tts.infer import utils_infer
        from f5_tts.model import DiT

        device = os.environ.get("NARRATOR_DEVICE", utils_infer.device)
        self.device = device
        hint = "" if device in ("mps", "cuda") else " — видеокарта не найдена, будет медленно"
        print(f"ESpeech считает на: {device}{hint}", file=sys.stderr, flush=True)
        self._model = utils_infer.load_model(
            DiT, ESPEECH_CFG, str(d / "espeech_tts_rlv2.pt"), vocab_file=str(d / "vocab.txt"),
            device=device,
        )  # fmt: skip
        vocos = vocos_dir()
        local = (vocos / "config.yaml").exists()
        self._vocoder = utils_infer.load_vocoder(
            is_local=local, local_path=str(vocos), device=device
        )
        self._utils = utils_infer
        ref_wav, ref_text = ref_sample(self.settings.effective_voice)
        self._ref_text = ref_text.read_text(encoding="utf-8").strip() + " "
        # Образец читаем сами: infer_process грузит его через torchaudio, а тот
        # требует разделяемых библиотек FFmpeg (torchcodec), которых может не быть.
        import torch

        audio, sr = sf.read(ref_wav, dtype="float32")
        self._ref = (torch.from_numpy(audio).unsqueeze(0), sr)

    def prepare(self) -> None:
        self._load()

    def clip(self, block: Block, on_part=None) -> tuple[np.ndarray, int]:
        self._load()
        s = self.settings
        audio, sr = self._ref
        # Как в f5_tts.infer_process: длина куска текста — по темпу речи образца.
        ref_sec = audio.shape[-1] / sr
        per_sec = len(self._ref_text.encode("utf-8")) / ref_sec
        max_chars = int(per_sec * (22 - ref_sec) * s.effective_speed)
        batches = self._utils.chunk_text(self.accent(block.text), max_chars=max_chars)
        total = sum(len(b) for b in batches) or 1
        waves: list[np.ndarray] = []
        out_sr = self._utils.target_sample_rate
        # Куски — строго по очереди в этом потоке. Без streaming f5_tts считает их
        # в ThreadPoolExecutor параллельно, и на MPS это падало с segfault (код 139)
        # на первом абзаце из нескольких кусков.
        for batch in batches:
            chunks = self._utils.infer_batch_process(
                self._ref, self._ref_text, [batch], self._model, self._vocoder,
                nfe_step=s.effective_steps, speed=s.effective_speed, device=self.device,
                progress=None, streaming=True, chunk_size=1 << 30,
            )  # fmt: skip
            parts = [np.asarray(c, dtype=np.float32) for c, out_sr in chunks]
            waves.append(np.concatenate(parts) if parts else np.zeros(0, np.float32))
            if on_part:
                on_part(len(batch) / total)
        return crossfade(waves, int(CROSSFADE * out_sr)), out_sr


# Перекрытие кусков одного абзаца, секунды: как cross_fade_duration в f5_tts.
CROSSFADE = 0.15


def crossfade(waves: list[np.ndarray], overlap: int) -> np.ndarray:
    """Склеивает куски с линейным перекрытием, как infer_batch_process в f5_tts."""
    out = waves[0] if waves else np.zeros(0, np.float32)
    for nxt in waves[1:]:
        n = min(overlap, len(out), len(nxt))
        if n <= 0:
            out = np.concatenate([out, nxt])
            continue
        fade = np.linspace(0, 1, n, dtype=np.float32)
        mixed = out[-n:] * (1 - fade) + nxt[:n] * fade
        out = np.concatenate([out[:-n], mixed, nxt[n:]])
    return out.astype(np.float32)


def silero_path() -> Path:
    legacy = f"~/.cache/torch/hub/snakers4_silero-models_master/src/silero/model/{SILERO_MODEL}.pt"
    return models.locate(f"{models.SILERO}/{SILERO_MODEL}.pt", legacy, "NARRATOR_SILERO_MODEL")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_silero(path: Path) -> None:
    """Сумма модели Silero до загрузки: PackageImporter исполняет код из файла."""
    got = file_sha256(path)
    if got != SILERO_SHA256:
        raise ModelMissingError(
            f"{path}: контрольная сумма не совпала ({got[:12]}… вместо {SILERO_SHA256[:12]}…) — "
            "файл подменён или битый, удали его и скачай заново: make models"
        )


def sentence_groups(text: str, limit: int) -> list[str]:
    """Режет текст по предложениям на группы не длиннее limit (длинное предложение — целиком)."""
    import re

    groups: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if groups and len(groups[-1]) + 1 + len(sentence) <= limit:
            groups[-1] += " " + sentence
        elif sentence:
            groups.append(sentence)
    return groups


class SileroSynth(Synthesizer):
    """Silero TTS v5.5: русская модель со своими ударениями и омографами.

    Ударения ставит сама, поверх — ручные акуты из script.txt и словарь FIXES.
    Быстрая, на CPU. Лицензия CC BY-NC — только некоммерческое использование.
    """

    def __init__(self, settings: Settings, accentor=None):
        super().__init__(settings, accentor)
        self._model = None

    def prepare(self) -> None:
        if self._model is not None:
            return
        path = silero_path()
        if not path.exists():
            raise ModelMissingError(f"нет модели Silero в {path}: {models.missing_hint()}")
        check_silero(path)
        import torch

        self._model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")

    def clip(self, block: Block, on_part=None) -> tuple[np.ndarray, int]:
        from narrator.stress import manual_marks

        self.prepare()
        groups = sentence_groups(manual_marks(block.text), SILERO_MAX_CHARS)
        total = sum(len(g) for g in groups) or 1
        pause = np.zeros(int(PAUSE_INNER * SILERO_SR), dtype=np.float32)
        parts: list[np.ndarray] = []
        for g in groups:
            audio = self._model.apply_tts(
                text=g, speaker=self.settings.effective_voice, sample_rate=SILERO_SR,
                put_accent=True, put_yo=True,
            )  # fmt: skip
            if parts:
                parts.append(pause)
            parts.append(audio.numpy().astype(np.float32))
            if on_part:
                on_part(len(g) / total)
        return np.concatenate(parts), SILERO_SR


class MlxSynth(Synthesizer):
    """Модель mlx-audio в отдельном процессе-воркере; голос — клон образца.

    Ударения модель ставит сама: ручные акуты и «+» из текста убираются.
    """

    def __init__(self, settings: Settings, accentor=None, worker=None):
        super().__init__(settings, accentor)
        self.spec = mlx_engines.MLX_ENGINES[settings.engine]
        self._worker = worker

    def prepare(self) -> None:
        if self._worker is not None:
            return
        problem = mlx_engines.problem(self.spec.id)
        if problem:
            raise ModelMissingError(f"{self.spec.label}: {problem}")
        ref_sample(self.settings.effective_voice)  # нет образца — скажем до загрузки модели
        print(f"{self.spec.label}: загружаю модель, это может занять минуту", file=sys.stderr)
        self._worker = mlx_engines.Worker(self.spec)

    def clip(self, block: Block, on_part=None) -> tuple[np.ndarray, int]:
        from narrator.stress import strip_marks

        self.prepare()
        groups = sentence_groups(strip_marks(block.text), mlx_engines.MAX_CHARS)
        total = sum(len(g) for g in groups) or 1
        ref_wav, ref_text_path = ref_sample(self.settings.effective_voice)
        ref_text = strip_marks(ref_text_path.read_text(encoding="utf-8").strip())
        sr = self._worker.sample_rate
        pause = np.zeros(int(PAUSE_INNER * sr), dtype=np.float32)
        parts: list[np.ndarray] = []
        with tempfile.TemporaryDirectory(prefix="narrator-mlx-") as tmp:
            for n, group in enumerate(groups):
                out = Path(tmp) / f"{n}.wav"
                self._worker.synthesize(group, out, ref_wav, ref_text, dict(self.spec.kwargs))
                audio, sr = sf.read(out, dtype="float32")
                if parts:
                    parts.append(pause)
                parts.append(audio if audio.ndim == 1 else audio.mean(axis=1))
                if on_part:
                    on_part(len(group) / total)
        return np.concatenate(parts), sr

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None


def make_synth(settings: Settings, accentor=None) -> Synthesizer:
    if settings.engine == "supertonic":
        return SupertonicSynth(settings, accentor)
    if settings.engine == "espeech":
        return ESpeechSynth(settings, accentor)
    if settings.engine == "silero":
        return SileroSynth(settings, accentor)
    if settings.engine in MLX_ENGINES:
        return MlxSynth(settings, accentor)
    raise ValueError(f"движок {settings.engine!r} неизвестен, есть: {', '.join(ENGINES)}")


class Progress:
    """Полоса прогресса статьи по символам текста.

    В терминале — полоса tqdm: процент, прошло и осталось времени, скорость.
    Раз в секунду перерисовывается, так что таймер тикает и во время долгого
    абзаца — видно, что процесс жив. Не в терминале (агент, лог в файл) —
    обычные строки по каждому абзацу.
    """

    def __init__(self, total: int, title: str, log: Callable[[str], None] | None):
        self.title = title
        self.tty = log is None and sys.stderr.isatty()
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.bar = None
        self._stop = None
        if self.tty:
            import threading

            from tqdm import tqdm

            self.bar = tqdm(
                total=total,
                file=sys.stderr,
                dynamic_ncols=True,
                bar_format="{desc} {percentage:3.0f}%|{bar}| {n:.0f}/{total:.0f} симв "
                "[прошло {elapsed}, осталось ~{remaining}]",
            )
            self._stop = threading.Event()
            threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            self.bar.refresh()

    def stage(self, text: str) -> None:
        if self.bar is not None:
            self.bar.set_description_str(f"{self.title} {text}", refresh=True)
        else:
            self.log(f"{self.title} {text}")

    def advance(self, chars: float) -> None:
        if self.bar is not None:
            self.bar.update(chars)

    def note(self, msg: str) -> None:
        if self.bar is not None:
            self.bar.write(msg, file=sys.stderr)
        else:
            self.log(msg)

    def close(self) -> None:
        if self.bar is not None:
            self._stop.set()
            self.bar.close()


def render(
    blocks: list[Block],
    out_wav: Path,
    cache_dir: Path,
    synth: Synthesizer,
    log: Callable[[str], None] | None = None,
    title: str = "",
) -> float:
    """Озвучивает блоки в out_wav; возвращает длительность в секундах.

    Пишет cache_dir/manifest.json: блоки и их клипы — для narrator verify.
    log задан — сообщения идут в него и полосы прогресса нет (тесты, агент).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = [cache_dir / f"{clip_key(b, synth.settings)}.wav" for b in blocks]
    todo = [i for i, p in enumerate(paths) if not p.exists()]
    if todo:
        synth.prepare()
    total = sum(len(blocks[i].text) for i in todo)
    progress = Progress(total, title, log)
    if len(todo) < len(blocks):
        progress.note(f"{len(blocks) - len(todo)} из {len(blocks)} абзацев уже в кэше")
    started = time.monotonic()
    try:
        for n, i in enumerate(todo, start=1):
            block, path = blocks[i], paths[i]
            size = len(block.text)
            progress.stage(f"абзац {n}/{len(todo)}")
            done = 0.0

            def on_part(share: float, size=size) -> None:
                nonlocal done
                step = share * size
                done += step
                progress.advance(step)

            t0 = time.monotonic()
            wav, sr = synth.clip(block, on_part=on_part)
            progress.advance(size - done)
            tmp = path.with_suffix(".tmp.wav")
            sf.write(tmp, wav, sr)
            tmp.replace(path)
            took = time.monotonic() - t0
            progress.note(
                f"[{i + 1}/{len(blocks)}] {len(wav) / sr:5.1f} с аудио за {took:4.0f} с  "
                f"{block.text[:50]}"
            )
    finally:
        progress.close()
    clips = paths
    msg = f"синтез: {time.monotonic() - started:.0f} с, склеиваю {len(clips)} клипов"
    (log or (lambda m: print(m, file=sys.stderr)))(msg)

    sr = sf.info(clips[0]).samplerate
    total = 0
    with sf.SoundFile(out_wav, "w", samplerate=sr, channels=1, subtype="PCM_16") as out:
        for pause, path in zip(pauses(blocks), clips, strict=True):
            if pause:
                out.write(np.zeros(int(pause * sr), dtype=np.float32))
                total += int(pause * sr)
            data, clip_sr = sf.read(path, dtype="float32")
            if clip_sr != sr:
                raise RuntimeError(f"{path}: частота {clip_sr}, ждали {sr}")
            out.write(data)
            total += len(data)

    manifest = [
        {"line": b.line, "kind": b.kind, "text": b.text, "clip": p.name}
        for b, p in zip(blocks, clips, strict=True)
    ]
    (cache_dir / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return total / sr


# Синтетический голос — метка в ID3 (TXXX:AI_GENERATED, комментарий): EU AI Act ст. 50, CMP-011.
AI_METADATA = ["-metadata", "comment=AI-generated: synthetic voice", "-metadata", "AI_GENERATED=1"]


def encode_mp3(wav: Path, mp3: Path, title: str, bitrate: str = "64k") -> None:
    tmp = mp3.with_suffix(".tmp.mp3")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
        "-ac", "1", "-codec:a", "libmp3lame", "-b:a", bitrate,
        "-metadata", f"title={title}", "-metadata", "album=Озвученные статьи",
        "-metadata", "artist=narrator", *AI_METADATA, str(tmp),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    tmp.replace(mp3)


def retime_mp3(src: Path, dst: Path, tempo: float, bitrate: str = "64k") -> None:
    """Тот же mp3 в другом темпе: длительность делится на tempo, высота голоса не меняется."""
    tmp = dst.with_suffix(".tmp.mp3")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
        "-filter:a", f"atempo={tempo:g}", "-ac", "1", "-codec:a", "libmp3lame",
        "-b:a", bitrate, *AI_METADATA, str(tmp),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    tmp.replace(dst)
