"""Ударения для озвучки: RUAccent turbo3.1 офлайн + ручные акуты + словарь поправок.

Supertonic сам ставит ударения с ошибками («вырабóтать», «намерéния»), акут на
каждом слове их исправляет — проверено на прослушивании. ESpeech обучен на
тексте с «+» перед ударной гласной. Поэтому текст блока сначала размечается
RUAccent, потом поверх — акуты, поставленные в script.txt вручную (они важнее
модели: так чинят её ошибки), потом словарь поправок для сленга, которого
RUAccent не знает.

Внутреннее представление — «+» перед ударной гласной, как у RUAccent.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from narrator import models
from narrator.script import ACUTE, normalize

VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
# Меняется логика расстановки — меняется версия, и кэш клипов пересобирается.
ACCENT_VERSION = "ruaccent-turbo3.1-v1"
OMOGRAPH_MODEL = "turbo3.1"

# Основа слова → основа с ударением. Применяется к словоформам, начинающимся
# с основы, и перекрывает RUAccent. Ручной акут в script.txt главнее.
FIXES = {
    "продакт": "пр+одакт",
    "фронтенд": "фр+онтенд",
    "бэкенд": "б+экенд",
    "дедлайн": "дедл+айн",
    "фреймворк": "фр+еймворк",
    "релиз": "рел+из",
}

_WORD = re.compile(r"[а-яёА-ЯЁ+" + ACUTE + r"]+")


def ruaccent_dir() -> Path:
    return models.locate(models.RUACCENT, "~/.cache/ruaccent", "NARRATOR_RUACCENT_DIR")


def acute_to_plus(text: str) -> str:
    """«за́мок» → «з+амок»."""
    out: list[str] = []
    for ch in normalize(text):
        if ch == ACUTE and out and out[-1] in VOWELS:
            out.insert(len(out) - 1, "+")
        elif ch != ACUTE:
            out.append(ch)
    return "".join(out)


def plus_to_acute(text: str) -> str:
    """«з+амок» → «за́мок»."""
    return re.sub(r"\+([" + VOWELS + r"])", lambda m: m.group(1) + ACUTE, text).replace("+", "")


def strip_marks(text: str) -> str:
    return normalize(text).replace(ACUTE, "").replace("+", "")


def _apply_fixes(word: str) -> str:
    bare = word.replace("+", "")
    low = bare.lower()
    for stem, stressed in FIXES.items():
        if low.startswith(stem):
            fixed = stressed + bare[len(stem) :]
            return fixed if bare[:1].islower() else fixed[:1].upper() + fixed[1:]
    return word


def merge(source: str, accented: str) -> str:
    """Слова из разметки RUAccent, но ручные ударения из source важнее.

    source — текст блока (может содержать акуты), accented — вывод RUAccent
    для source без акутов. Слова сопоставляются по порядку; если RUAccent
    поменял число слов, ручные ударения накладываются только когда совпало.
    """
    src_words = _WORD.findall(normalize(source))
    acc_words = _WORD.findall(accented)
    aligned = len(src_words) == len(acc_words)
    it = iter(range(len(acc_words)))

    def repl(m: re.Match[str]) -> str:
        i = next(it)
        word = m.group()
        if aligned and ACUTE in src_words[i]:
            return acute_to_plus(src_words[i])
        return _apply_fixes(word)

    return _WORD.sub(repl, accented)


def manual_marks(text: str) -> str:
    """Только ручные ударения из script.txt и словарь FIXES, в «+»-записи.

    Для Silero: остальные слова он размечает сам, со своими омографами.
    """
    marked = acute_to_plus(text)
    return _WORD.sub(lambda m: m.group() if "+" in m.group() else _apply_fixes(m.group()), marked)


class Accentor:
    """RUAccent turbo3.1 из локальной папки, без обращений к huggingface.co."""

    def __init__(self, workdir: Path | None = None):
        self.workdir = workdir or ruaccent_dir()
        if not (self.workdir / "dictionary").exists():
            raise RuntimeError(f"нет моделей RUAccent в {self.workdir}: {models.missing_hint()}")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import ruaccent
        import ruaccent.accent_model as accent_model

        # Морфоанализатор koziev RUAccent ищет внутри своего пакета.
        pkg_koziev = Path(ruaccent.__file__).parent / "koziev"
        if not pkg_koziev.exists():
            src = self.workdir / "koziev"
            if not src.exists():
                raise RuntimeError(
                    f"нет {src} (морфоанализатор RUAccent): sh tools/fetch-models.sh"
                )
            shutil.copytree(src, pkg_koziev)
        accent_model.AccentModel.put_accent = _put_accent
        self.model = ruaccent.RUAccent()
        self.model.load(
            omograph_model_size=OMOGRAPH_MODEL, use_dictionary=True, workdir=str(self.workdir)
        )

    def __call__(self, text: str) -> str:
        """Текст блока → текст с «+» перед каждой ударной гласной."""
        return merge(text, self.model.process_all(strip_marks(text)))


def _put_accent(self, word):
    """Копия AccentModel.put_accent: onnx-модель ждёт token_type_ids, токенизатор их не отдаёт."""
    import numpy as np

    tokens = self.tokenizer(word.lower(), return_tensors="np")
    inputs = {k: v.astype(np.int64) for k, v in tokens.items()}
    if "token_type_ids" not in inputs and any(
        i.name == "token_type_ids" for i in self.session.get_inputs()
    ):
        inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
    outputs = self.session.run(None, inputs)
    names = {o.name: i for i, o in enumerate(self.session.get_outputs())}
    logits = outputs[names["logits"]]
    e = np.exp(logits - logits.max(-1, keepdims=True))
    probs = e / e.sum(-1, keepdims=True)
    pred = [
        {"label": self.id2label[str(label)], "score": float(score)}
        for label, score in zip(np.argmax(logits, -1)[0], np.max(probs, -1)[0], strict=True)
    ]
    return self.render_stress(word, pred)
