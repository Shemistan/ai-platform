"""Воркер моделей mlx-audio: держит модель в памяти и озвучивает по запросам.

Запускается отдельным процессом в своём окружении (см. narrator.mlx_engines):
mlx-audio тянет transformers 5, а основному окружению с f5-tts он не подходит.
Поэтому здесь только stdlib и mlx_audio, из narrator ничего не импортируется.

Протокол — строки JSON. Воркер пишет {"ready": true, "sample_rate": N}, затем
на каждый запрос {"text", "out", "ref_audio", "ref_text", "kwargs"} отвечает
{"ok": true, "samples": N, "sample_rate": N} или {"error": "…"}. Ответы идут в
отдельный поток: всё, что библиотеки печатают в stdout, уходит в stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback


def reply(channel, payload: dict) -> None:
    channel.write(json.dumps(payload, ensure_ascii=False) + "\n")
    channel.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="репозиторий HuggingFace или папка модели")
    parser.add_argument("--check", action="store_true", help="только проверить окружение")
    args = parser.parse_args()

    # Ответы — в прежний stdout, а сам fd 1 отдаём под stderr: mlx-audio печатает в него.
    channel = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)

    try:
        import mlx.core as mx
        import numpy as np
        from mlx_audio.audio_io import write as audio_write
        from mlx_audio.tts.utils import load_model
        from mlx_audio.utils import load_audio
        from mlx_audio.version import __version__ as version
    except Exception as e:  # noqa: BLE001 — причину покажет родитель
        reply(channel, {"error": f"окружение mlx-audio не собралось: {e!r}"})
        return 1
    if args.check:
        reply(channel, {"ready": True, "version": version})
        return 0

    try:
        model = load_model(args.model)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        reply(channel, {"error": f"модель {args.model} не загрузилась: {e!r}"})
        return 1
    sample_rate = int(model.sample_rate)
    reply(channel, {"ready": True, "sample_rate": sample_rate, "version": version})

    references: dict[str, object] = {}  # образец голоса читаем один раз
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            kwargs = dict(req.get("kwargs") or {})
            ref = req.get("ref_audio")
            if ref:
                if ref not in references:
                    references[ref] = load_audio(ref, sample_rate=sample_rate)
                kwargs["ref_audio"] = references[ref]
                # MOSS не знает частоту массива и ждёт её отдельным параметром.
                if ".moss_tts" in type(model).__module__:
                    kwargs["ref_audio_sample_rate"] = sample_rate
                if req.get("ref_text"):
                    kwargs["ref_text"] = req["ref_text"]
            # Модель может отдать несколько кусков (длинный текст режет сама) — склеиваем.
            parts = [
                np.asarray(result.audio, dtype=np.float32).reshape(-1)
                for result in model.generate(text=req["text"], verbose=False, **kwargs)
            ]
            if not parts:
                raise RuntimeError("модель не вернула звук")
            audio = np.concatenate(parts)
            audio_write(req["out"], audio, sample_rate, format="wav")
            mx.clear_cache()
            reply(channel, {"ok": True, "samples": int(len(audio)), "sample_rate": sample_rate})
        except Exception as e:  # noqa: BLE001 — один плохой абзац не должен убить воркер
            traceback.print_exc()
            reply(channel, {"error": repr(e)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
