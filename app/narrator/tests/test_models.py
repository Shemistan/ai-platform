from narrator import models, stress, synth, verify


def test_locate_prefers_models_dir(tmp_path, monkeypatch):
    root, legacy = tmp_path / "models", tmp_path / "cache" / "ruaccent"
    (root / "ruaccent").mkdir(parents=True)
    legacy.mkdir(parents=True)
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(root))
    assert models.locate("ruaccent", str(legacy)) == root / "ruaccent"


def test_locate_falls_back_to_legacy_until_moved(tmp_path, monkeypatch):
    legacy = tmp_path / "cache" / "ruaccent"
    legacy.mkdir(parents=True)
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "models"))
    assert models.locate("ruaccent", str(legacy)) == legacy
    # Модели нет нигде — путь в папке моделей: туда её и скачают.
    assert models.locate("silero", str(tmp_path / "нет")) == tmp_path / "models" / "silero"


def test_locate_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("NARRATOR_RUACCENT_DIR", str(tmp_path / "свои"))
    assert stress.ruaccent_dir() == tmp_path / "свои"


def test_engine_paths_live_in_models_dir(tmp_path, monkeypatch):
    for name in ("NARRATOR_ESPEECH_DIR", "NARRATOR_SILERO_MODEL", "SUPERTONIC_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path))
    for name in (models.ESPEECH, models.SUPERTONIC, models.SILERO):
        (tmp_path / name).mkdir()
    (tmp_path / models.SILERO / "v5_5_ru.pt").write_bytes(b"x")
    assert synth.espeech_dir() == tmp_path / models.ESPEECH
    assert synth.supertonic_dir() == tmp_path / models.SUPERTONIC
    assert synth.silero_path() == tmp_path / models.SILERO / "v5_5_ru.pt"
    assert synth.vocos_dir() == tmp_path / models.VOCOS


def test_whisper_source_is_folder_when_downloaded(tmp_path, monkeypatch):
    monkeypatch.delenv("NARRATOR_WHISPER_DIR", raising=False)
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path))
    assert verify.whisper_source() == verify.WHISPER_MODEL  # папки нет — имя для кэша HF
    (tmp_path / models.WHISPER).mkdir()
    (tmp_path / models.WHISPER / "model.bin").write_bytes(b"x")
    assert verify.whisper_source() == str(tmp_path / models.WHISPER)


def test_missing_hint_names_unplugged_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "нет диска"))
    assert "подключи диск" in models.missing_hint()
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path))
    assert "fetch-models.sh" in models.missing_hint()
