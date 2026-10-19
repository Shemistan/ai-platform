"""Сравнение моделей расшифровки на своей записи: `make asr-compare REC=<запись> REF=<эталон.md>`.

Эталон — текст, по которому читали (teleprompter.md): разметка markdown
убирается. По каждой установленной модели (или из --models): WER, доля верно
узнанных латинских слов, доля слов, попавших в тишину (грубая мера точности
времени), скорость. Таблица — в терминал и в `<запись>.asr-compare.md`, текст
каждой модели — рядом (`<запись>.<модель>.txt`), чтобы посмотреть глазами.

Идёт в обычном терминале: моделям нужен Metal (в песочнице агента его нет).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.montage import asr, transcript
from aiplatform.montage import config as montage_config

_MD = (
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>)\s+", re.M), ""),
    (re.compile(r"<[^>]+>"), " "),
    (re.compile(r"[*_~]{1,3}"), ""),
)


def strip_markdown(text: str) -> str:
    for pattern, repl in _MD:
        text = pattern.sub(repl, text)
    return text


def extract_audio(ffmpeg: str, source: Path, wav: Path) -> None:
    argv = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(wav)]  # fmt: skip
    subprocess.run(argv, check=True)


def silences_of(ffmpeg: str, wav: Path, duration: float) -> list[tuple[float, float]]:
    proc = subprocess.run(transcript.silence_argv(ffmpeg, wav), capture_output=True, text=True, check=True)
    return transcript.parse_silences(proc.stderr.splitlines(), duration)


def run_model(argv: list[str], label: str) -> float:
    """Воркер с прогрессом в одну строку терминала; возвращает секунды работы."""
    started = time.monotonic()
    with subprocess.Popen(argv, stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            step = asr.parse_progress(line)
            if step:
                print(f"\r  {label}: {step[0]:5.1f}% {step[1]:<30}", end="", flush=True)
    print()
    if proc.returncode:
        raise RuntimeError(f"{label}: воркер завершился с кодом {proc.returncode}")
    return time.monotonic() - started


def row(model: asr.Model, words: list[dict], ref: list[str], silences, seconds: float, duration: float) -> dict:
    hyp = transcript.normalize(" ".join(w["w"] for w in words))
    latin_hit, latin_all = transcript.latin_recall(ref, hyp)
    return {
        "model": model.label,
        "wer": transcript.wer(ref, hyp),
        "latin": f"{latin_hit}/{latin_all}" if latin_all else "—",
        "latin_share": latin_hit / latin_all if latin_all else None,
        "silent": transcript.words_in_silence(words, silences),
        "rtf": seconds / duration if duration else 0.0,
        "seconds": seconds,
    }


def table(rows: list[dict]) -> str:
    head = "| Модель | WER | Латиница | Слова в тишине | Время | × реального |\n|---|---|---|---|---|---|"
    lines = [
        f"| {r['model']} | {r['wer']:.1%} | {r['latin']} | {r['silent']:.1%} | {r['seconds']:.0f} с | {r['rtf']:.2f} |"
        for r in rows
    ]
    return "\n".join([head, *lines])


def best(rows: list[dict]) -> str:
    """Короткий вывод: меньше ошибок, затем латиница, затем скорость."""
    if not rows:
        return ""
    ranked = sorted(rows, key=lambda r: (round(r["wer"], 3), -(r["latin_share"] or 0), r["rtf"]))
    top = ranked[0]
    return f"Лучше всех по точности — {top['model']} (WER {top['wer']:.1%})."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сравнить модели расшифровки на записи с эталонным текстом.")
    parser.add_argument("recording", type=Path, help="видео или звук")
    parser.add_argument("reference", type=Path, help="эталонный текст (teleprompter.md)")
    parser.add_argument("--models", nargs="*", help=f"id моделей; по умолчанию все установленные: {asr.DEFAULT_ID}…")
    parser.add_argument("--terms", default="", help="термины через запятую — подсказка моделям (hotwords)")
    args = parser.parse_args(argv)

    cfg = montage_config.load(platform_config.load())
    chosen = [asr.get(m) for m in args.models] if args.models else [m for m in asr.CATALOG if asr.installed(m, cfg)]
    unknown = [name for name, m in zip(args.models, chosen, strict=True) if m is None] if args.models else []
    if unknown:
        parser.error(f"неизвестные модели: {', '.join(unknown)}. Есть: {', '.join(m.id for m in asr.CATALOG)}")
    models = [m for m in chosen if m is not None]
    for model in models:
        lack = asr.missing(model, cfg)
        if lack:
            parser.error(f"{model.label} не установлена (нет {', '.join(lack)}): make models M={model.id}")
    if not models:
        parser.error("ни одной модели расшифровки не установлено: make models M=whisper-large-v3-turbo")

    ref = transcript.normalize(strip_markdown(args.reference.read_text(encoding="utf-8")))
    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        print(f"→ звук из {args.recording.name}")
        extract_audio(cfg.ffmpeg, args.recording, wav)
        from aiplatform.core import media

        duration = media.duration(wav, cfg.ffmpeg)
        silences = silences_of(cfg.ffmpeg, wav, duration)
        splits = transcript.split_points(duration, silences)
        for model in models:
            out = Path(tmp) / f"{model.id}.json"
            argv_ = asr.build_argv(cfg, model, wav, out, splits=splits, hotwords=terms)
            seconds = run_model(argv_, model.label)
            words = json.loads(out.read_text(encoding="utf-8"))["words"]
            text_file = args.recording.with_name(f"{args.recording.stem}.{model.id}.txt")
            text_file.write_text(" ".join(w["w"] for w in words) + "\n", encoding="utf-8")
            rows.append(row(model, words, ref, silences, seconds, duration))
    report = table(rows)
    verdict = best(rows)
    print(report)
    print(verdict)
    target = args.recording.with_name(f"{args.recording.stem}.asr-compare.md")
    target.write_text(
        f"# Сравнение расшифровки — {args.recording.name}\n\n{date.today().isoformat()}, "
        f"{duration / 60:.1f} мин, эталон {args.reference.name} ({len(ref)} слов)\n\n{report}\n\n{verdict}\n\n"
        "WER — доля ошибок по словам (меньше — лучше). Латиница — сколько английских слов эталона узнано точно. "
        "Слова в тишине — доля слов, чьё время попало в паузу: чем больше, тем хуже ляжет склейка.\n",
        encoding="utf-8",
    )
    print(f"✓ отчёт: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
