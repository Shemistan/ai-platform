import json
import sys
import textwrap

import pytest
import soundfile as sf

from narrator import mlx_engines
from narrator.mlx_engines import MLX_ENGINES, Worker, WorkerError
from narrator.script import Block
from narrator.synth import (
    CLONE_ENGINES,
    ENGINES,
    PAUSE_INNER,
    REF_WAV,
    VOICE_DIR,
    MlxSynth,
    ModelMissingError,
    Settings,
    make_synth,
)

SR = 8000

# Вместо mlx-audio: тот же протокол, тон длиной 10 мс на символ; запросы пишет в журнал.
FAKE_WORKER = textwrap.dedent(
    """
    import json, sys
    import numpy as np, soundfile as sf
    log = open(sys.argv[1], "a", encoding="utf-8")
    print(json.dumps({"ready": True, "sample_rate": 8000}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        log.write(line); log.flush()
        if "сломайся" in req["text"]:
            print(json.dumps({"error": "RuntimeError('нет памяти')"}), flush=True)
            continue
        if "умри" in req["text"]:
            sys.exit(7)
        audio = np.full(len(req["text"]) * 80, 0.1, dtype=np.float32)
        sf.write(req["out"], audio, 8000)
        print(json.dumps({"ok": True, "samples": len(audio), "sample_rate": 8000}), flush=True)
    """
)


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("NARRATOR_MLX_WORKER", f"{sys.executable} {script} {log}")
    monkeypatch.setattr(mlx_engines, "problem", lambda engine_id: None)
    return log


def requests(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_engines_are_registered():
    assert set(MLX_ENGINES) == {"higgs", "fish", "moss", "qwen3", "chatterbox"}
    assert set(MLX_ENGINES) <= set(ENGINES) and set(MLX_ENGINES) <= set(CLONE_ENGINES)
    assert isinstance(make_synth(Settings(engine="moss")), MlxSynth)
    # Язык у каждой модели задаётся по-своему.
    assert MLX_ENGINES["moss"].kwargs == {"language": "Russian"}
    assert MLX_ENGINES["qwen3"].kwargs == {"lang_code": "russian"}
    assert MLX_ENGINES["chatterbox"].kwargs == {"lang_code": "ru"}


def test_clip_sends_plain_text_and_voice_sample(fake_worker):
    synth = make_synth(Settings(engine="qwen3"))
    try:
        wav, sr = synth.clip(Block("para", "За́мок сто+ит на горе́.", 1))
    finally:
        synth.close()
    assert sr == SR and len(wav) == len("Замок стоит на горе.") * 80
    (req,) = requests(fake_worker)
    assert req["text"] == "Замок стоит на горе."  # ударения модель ставит сама
    assert req["ref_audio"] == str(VOICE_DIR / "charon.wav") and "+" not in req["ref_text"]
    assert req["kwargs"] == {"lang_code": "russian"}


def test_clip_splits_long_paragraph_by_sentences(fake_worker, monkeypatch):
    monkeypatch.setattr(mlx_engines, "MAX_CHARS", 30)
    synth = make_synth(Settings(engine="higgs"))
    done = []
    try:
        wav, sr = synth.clip(
            Block("para", "Первое предложение абзаца. Второе предложение абзаца.", 1),
            on_part=done.append,
        )
    finally:
        synth.close()
    texts = [r["text"] for r in requests(fake_worker)]
    assert texts == ["Первое предложение абзаца.", "Второе предложение абзаца."]
    assert len(wav) == sum(len(t) for t in texts) * 80 + int(PAUSE_INNER * SR)
    assert sum(done) == pytest.approx(1.0)


def test_worker_keeps_model_between_clips_and_survives_error(fake_worker):
    synth = make_synth(Settings(engine="fish"))
    try:
        synth.clip(Block("para", "Первый абзац текста.", 1))
        with pytest.raises(WorkerError, match="нет памяти"):
            synth.clip(Block("para", "Теперь сломайся, пожалуйста.", 3))
        wav, _ = synth.clip(Block("para", "Третий абзац текста.", 5))
    finally:
        synth.close()
    assert len(wav) and len(requests(fake_worker)) == 3  # процесс один и тот же


def test_dead_worker_is_reported(fake_worker):
    synth = make_synth(Settings(engine="chatterbox"))
    try:
        with pytest.raises(WorkerError, match="кодом 7"):
            synth.clip(Block("para", "Воркер, умри сейчас.", 1))
    finally:
        synth.close()


def test_missing_model_is_named_before_worker_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path))
    monkeypatch.delenv("NARRATOR_MLX_CACHE", raising=False)
    monkeypatch.setattr(mlx_engines.sys, "platform", "darwin")
    monkeypatch.setattr(mlx_engines.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(mlx_engines.shutil, "which", lambda name: "/usr/bin/uv")
    assert mlx_engines.problem("moss") == "модель не скачана: ./narrate setup --mlx moss"
    with pytest.raises(ModelMissingError, match="setup --mlx moss"):
        make_synth(Settings(engine="moss")).prepare()

    # Скачаны и модель, и её токенизатор звука — движок готов.
    for repo in ("OpenMOSS-Team/MOSS-TTS-v1.5", "OpenMOSS-Team/MOSS-Audio-Tokenizer"):
        snapshot = mlx_engines.repo_dir(repo) / "snapshots" / "abc"
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
    assert mlx_engines.repo_dir(repo).parent == tmp_path / "mlx-audio"
    assert mlx_engines.problem("moss") is None


def test_problem_on_other_platforms(monkeypatch):
    monkeypatch.setattr(mlx_engines.sys, "platform", "linux")
    assert mlx_engines.problem("higgs") == "нужен Mac на Apple Silicon"


def test_worker_env_is_offline_and_points_to_models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path))
    monkeypatch.delenv("NARRATOR_MLX_CACHE", raising=False)
    monkeypatch.delenv("NARRATOR_MLX_ONLINE", raising=False)
    env = mlx_engines.worker_env()
    assert env["HF_HUB_CACHE"] == str(tmp_path / "mlx-audio") and env["HF_HUB_OFFLINE"] == "1"
    monkeypatch.delenv("NARRATOR_MLX_WORKER", raising=False)
    command = mlx_engines.worker_command()
    assert command[:2] == ["uv", "run"] and mlx_engines.MLX_AUDIO in command
    assert command[-1].endswith("mlx_worker.py")


def test_worker_reports_failed_start(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_MLX_WORKER", str(tmp_path / "нет-такой-команды"))
    with pytest.raises(WorkerError, match="не запустился"):
        Worker(MLX_ENGINES["higgs"])


def test_fake_worker_writes_valid_wav(fake_worker, tmp_path):
    worker = Worker(MLX_ENGINES["higgs"])
    try:
        out = tmp_path / "x.wav"
        assert worker.synthesize("Привет.", out, REF_WAV, "образец", {}) == len("Привет.") * 80
    finally:
        worker.close()
    assert sf.info(out).samplerate == SR


def test_live_voice_clone_needs_consent(fake_worker, tmp_path, monkeypatch):
    # CMP-009: клон живого голоса без согласия — отказ до запуска модели; с согласием — синтез.
    import numpy as np
    import soundfile as sf

    from narrator import consent
    from narrator.synth import ConsentMissingError

    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "models"))
    voices = tmp_path / "models" / "voices"
    voices.mkdir(parents=True)
    sf.write(voices / "shem.wav", np.zeros(8 * 8000, dtype=np.float32), 8000)
    (voices / "shem.txt").write_text("Образ+ец.\n", encoding="utf-8")
    synth = make_synth(Settings(engine="qwen3", voice="Shem"))
    with pytest.raises(ConsentMissingError, match="без согласия"):
        synth.prepare()
    assert not fake_worker.exists(), "модель не запускалась"
    consent.write(voices / "shem.wav", consent.make("Шамистан", "self"))
    synth = make_synth(Settings(engine="qwen3", voice="Shem"))
    try:
        wav, _ = synth.clip(Block("para", "Проверка голоса.", 1))
    finally:
        synth.close()
    assert len(wav)
    (req,) = requests(fake_worker)
    assert req["ref_audio"] == str(voices / "shem.wav")
