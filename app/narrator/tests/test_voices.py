import base64
import json

import numpy as np
import pytest
import soundfile as sf

from narrator import gemini, synth
from narrator.cli import main
from narrator.script import Block
from narrator.synth import ModelMissingError, Settings, clip_key, has_voice, ref_sample

SR = 24000


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "models"))
    return tmp_path / "models"


def put_sample(models_dir, seconds=8.0):
    d = models_dir / "voices"
    d.mkdir(parents=True, exist_ok=True)
    sf.write(d / "charon.wav", np.zeros(int(seconds * SR), dtype=np.float32), SR)
    (d / "charon.txt").write_text("Образ+ец.\n", encoding="utf-8")
    return d / "charon.wav", d / "charon.txt"


def test_clone_voice_sample(models_dir, tmp_path, monkeypatch):
    assert ref_sample("M1") == (synth.REF_WAV, synth.REF_TEXT)
    assert "Charon" in synth.ENGINE_VOICES["espeech"] and "Charon" in synth.ENGINE_VOICES["qwen3"]
    assert "Charon" not in synth.ENGINE_VOICES["silero"]
    # Образец Charon лежит в пакете: голос работает без записи и без диска с моделями.
    wav, text = ref_sample("Charon")
    assert wav.parent == synth.VOICE_DIR and has_voice("fish", "Charon")
    low, high = synth.REF_SECONDS
    assert low <= sf.info(wav).duration <= high and sf.info(wav).channels == 1
    assert "+" in text.read_text(encoding="utf-8")
    # Shem — живой голос: в пакете его нет (CMP-009), только в папке моделей.
    assert not list(synth.VOICE_DIR.glob("shem*"))
    assert "Shem" in synth.ENGINE_VOICES["higgs"] and "Shem" not in synth.ENGINE_VOICES["silero"]
    with pytest.raises(ModelMissingError, match="живой голос в пакете не лежит"):
        ref_sample("Shem")
    assert not has_voice("higgs", "Shem-v2") and not has_voice("higgs", "Shem-v3")
    # Записанный человеком образец главнее.
    own = put_sample(models_dir)
    assert ref_sample("Charon") == own

    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "пусто"))
    monkeypatch.setattr(synth, "VOICE_DIR", tmp_path / "пусто")
    with pytest.raises(ModelMissingError, match="narrate voice charon"):
        ref_sample("Charon")
    assert not has_voice("espeech", "Charon") and has_voice("silero", "eugene")


def test_clip_key_depends_on_clone_voice():
    b = Block("para", "Текст.", 1)
    # По умолчанию клон говорит голосом Charon; клипы M1 лежат в кэше под своим ключом.
    assert Settings(engine="espeech").effective_voice == "Charon"
    assert Settings(engine="qwen3").effective_voice == "Charon"
    assert clip_key(b, Settings(engine="espeech")) == clip_key(
        b, Settings(engine="espeech", voice="Charon")
    )
    assert clip_key(b, Settings(engine="espeech")) != clip_key(
        b, Settings(engine="espeech", voice="M1")
    )


def test_espeech_without_sample_fails_before_loading(models_dir, tmp_path, monkeypatch):
    model = tmp_path / "espeech"
    model.mkdir()
    (model / "espeech_tts_rlv2.pt").write_bytes(b"")
    monkeypatch.setenv("NARRATOR_ESPEECH_DIR", str(model))
    monkeypatch.setattr(synth, "VOICE_DIR", tmp_path / "пусто")
    with pytest.raises(ModelMissingError, match="образца голоса Charon"):
        synth.make_synth(Settings(engine="espeech", voice="Charon")).prepare()


def gemini_answer(seconds: float) -> dict:
    pcm = (np.full(int(seconds * SR), 0.25) * 32767).astype("<i2").tobytes()
    inline = {"mimeType": f"audio/L16;codec=pcm;rate={SR}", "data": base64.b64encode(pcm).decode()}
    return {"candidates": [{"content": {"parts": [{"inlineData": inline}]}}]}


def test_gemini_speak():
    calls = []

    def post(url, body, key):
        calls.append((url, body, key))
        return gemini_answer(2.0)

    audio, sr = gemini.speak("Привет.", "Charon", "ключ", post=post)
    assert sr == SR and len(audio) == 2 * SR and audio.dtype == np.float32
    assert audio.max() == pytest.approx(0.25, abs=1e-3)
    ((url, body, key),) = calls
    assert gemini.DEFAULT_MODEL in url and key == "ключ"
    assert body["contents"][0]["parts"][0]["text"] == "Привет."
    config = body["generationConfig"]
    assert config["responseModalities"] == ["AUDIO"]
    assert config["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Charon"

    with pytest.raises(gemini.GeminiError, match="нет аудио"):
        gemini.speak("Привет.", "Charon", "ключ", post=lambda *a: {"candidates": []})


def test_voice_command_records_sample(models_dir, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert main(["voice", "m1"]) == 1  # образец M1 не перезаписывают
    assert main(["voice", "shem"]) == 1  # в Gemini такого голоса нет: только из записи
    assert "--from" in capsys.readouterr().err
    assert main(["voice", "charon"]) == 2
    err = capsys.readouterr().err
    assert "GEMINI_API_KEY" in err and "--from" in err and "+" not in err.split("«")[-1]

    said = []

    def speak(text, voice, key):
        said.append((text, voice, key))
        return np.zeros(8 * SR, dtype=np.float32), SR

    monkeypatch.setenv("GEMINI_API_KEY", "ключ")
    monkeypatch.setattr(gemini, "speak", speak)
    assert main(["voice", "charon"]) == 0
    ((text, voice, key),) = said
    assert voice == "Charon" and key == "ключ" and "+" not in text and text.startswith("Сегодня")
    wav, txt = ref_sample("Charon")
    assert wav.parent == models_dir / "voices" and sf.info(wav).duration == pytest.approx(8.0)
    # Текст образца — с ударениями: ESpeech читает его вместе с новым текстом.
    marked = synth.REF_TEXT.read_text(encoding="utf-8").strip()
    assert txt.read_text(encoding="utf-8").strip() == marked and "+" in marked


def test_voice_command_takes_ready_file(models_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    stereo = tmp_path / "charon-from-studio.wav"
    sf.write(stereo, np.zeros((9 * SR, 2), dtype=np.float32), SR)
    assert main(["voice", "charon", "--from", str(stereo)]) == 0
    info = sf.info(ref_sample("Charon")[0])
    assert info.channels == 1 and info.duration == pytest.approx(9.0)

    long = tmp_path / "long.wav"
    sf.write(long, np.zeros(20 * SR, dtype=np.float32), SR)
    assert main(["voice", "charon", "--from", str(long)]) == 1
    assert "20.0 с" in capsys.readouterr().err
    assert main(["voice", "charon", "--from", str(tmp_path / "нет.wav")]) == 1


def test_voice_command_takes_own_text(models_dir, tmp_path, monkeypatch):
    from narrator import stress

    class FakeAccentor:
        def __call__(self, text):
            return text.replace("выпуск", "в+ыпуск")

    monkeypatch.setattr(stress, "Accentor", FakeAccentor)
    cut = tmp_path / "cut.wav"
    sf.write(cut, np.zeros(6 * SR, dtype=np.float32), SR)
    assert main(["voice", "charon", "--from", str(cut), "--text", " Это выпуск. "]) == 0
    assert ref_sample("Charon")[1].read_text(encoding="utf-8") == "Это в+ыпуск.\n"
    # Живой голос — только с согласием: без --owner/--form образец не пишется.
    assert main(["voice", "shem", "--from", str(cut), "--text", "Это выпуск."]) == 1
    assert not (models_dir / "voices" / "shem.wav").exists()
    argv = ["voice", "shem", "--from", str(cut), "--text", "Это выпуск."]
    assert main([*argv, "--owner", "Шамистан", "--form", "self"]) == 0
    assert ref_sample("Shem")[0].parent == models_dir / "voices"
    record = json.loads((models_dir / "voices" / "shem.consent.json").read_text(encoding="utf-8"))
    assert record["kind"] == "live" and record["owner"] == "Шамистан" and record["form"] == "self"


def test_gemini_http_error_is_reported(models_dir, monkeypatch, capsys):
    def speak(text, voice, key):
        raise gemini.GeminiError("Gemini TTS: HTTP 403 API key not valid")

    monkeypatch.setenv("GEMINI_API_KEY", "ключ")
    monkeypatch.setattr(gemini, "speak", speak)
    assert main(["voice", "charon"]) == 3
    assert "HTTP 403" in capsys.readouterr().err
    assert not (models_dir / "voices").exists()


# --- согласие владельца живого голоса (CMP-009) -------------------------------


def put_live(models_dir, stem="shem"):
    d = models_dir / "voices"
    d.mkdir(parents=True, exist_ok=True)
    sf.write(d / f"{stem}.wav", np.zeros(8 * SR, dtype=np.float32), SR)
    (d / f"{stem}.txt").write_text("Образ+ец.\n", encoding="utf-8")
    return d / f"{stem}.wav"


def test_live_voice_needs_consent(models_dir, capsys):
    from narrator import consent

    wav = put_live(models_dir)
    with pytest.raises(synth.ConsentMissingError, match="без согласия"):
        ref_sample("Shem")
    assert not has_voice("espeech", "Shem")
    # Запись о согласии для образца, положенного руками.
    assert main(["voice-consent", "shem", "--owner", "Шамистан", "--form", "self"]) == 0
    assert ref_sample("Shem")[0] == wav
    assert consent.check("shem", wav)["kind"] == "live"
    # Истёкшее согласие — снова отказ.
    consent.write(wav, consent.make("Шамистан", "written", expires="2020-01-01"))
    with pytest.raises(synth.ConsentMissingError, match="истекло"):
        ref_sample("Shem")
    # Битая запись — понятная ошибка, а не падение.
    consent.path_for(wav).write_text("{}", encoding="utf-8")
    with pytest.raises(synth.ConsentMissingError, match="kind"):
        ref_sample("Shem")
    # Синтетическим голосам запись не нужна.
    assert consent.check("charon", synth.VOICE_DIR / "charon.wav")["kind"] == "synthetic"
    assert main(["voice-consent", "charon", "--owner", "x", "--form", "self"]) == 1


def test_consent_make_validates():
    from narrator import consent

    with pytest.raises(consent.ConsentError, match="чей"):
        consent.make("  ", "self")
    with pytest.raises(consent.ConsentError, match="форма"):
        consent.make("Аня", "устно")
    with pytest.raises(consent.ConsentError, match="ГГГГ"):
        consent.make("Аня", "written", expires="завтра")
