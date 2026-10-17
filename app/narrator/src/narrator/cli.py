"""narrator — CLI озвучки статей. Порядок работы — CLAUDE.md, раздел «Озвучка статьи»."""

from __future__ import annotations

import argparse
import faulthandler
import sys
from pathlib import Path

from narrator import library, script
from narrator.library import META_NAME, Entry

SOURCE = "source.md"
SCRIPT = "script.txt"
CHECK = "check.md"
CACHE = ".cache"


def fail(msg: str, code: int = 1) -> int:
    print(f"narrator: {msg}", file=sys.stderr)
    return code


def resolve_dir(value: str) -> Path:
    """Путь к каталогу статьи или его имя в библиотеке: «2026/09/18/triple-debt»,
    «2026-09-18/triple-debt» или «2026-09-18-triple-debt» (так же называется её mp3)."""
    p = Path(value).expanduser()
    if not p.is_absolute() and not p.exists():
        root = library.library_root()
        library.migrate_layout(root)
        p = root / value
        day, slug = value[:10], value[11:]
        if not p.exists() and value[10:11] in ("-", "/") and slug:
            p = library.day_dir(root, day) / slug
    if not (p / META_NAME).exists():
        raise SystemExit(fail(f"{p}: нет {META_NAME} — это не каталог статьи"))
    return p.resolve()


def setup_mlx(ids: list[str]) -> int:
    """Скачивает модели mlx-audio и собирает окружение их воркера."""
    from narrator import mlx_engines

    unknown = [i for i in ids if i not in mlx_engines.MLX_ENGINES and i != "all"]
    if unknown:
        known = ", ".join(mlx_engines.MLX_ENGINES)
        return fail(f"движки mlx: {known} или all; не знаю {', '.join(unknown)}")
    chosen = list(mlx_engines.MLX_ENGINES) if "all" in ids else ids
    try:
        print(f"окружение воркера: mlx-audio {mlx_engines.check_environment()}")
        for engine_id in chosen:
            engine = mlx_engines.MLX_ENGINES[engine_id]
            print(f"== {engine.label}: {engine.repo}, около {engine.size_gb:g} ГБ", flush=True)
            print(f"модель: {mlx_engines.download(engine)}")
    except Exception as e:  # сеть, лицензия на huggingface.co, окружение
        return fail(f"{e}", 3)
    return 0


def cmd_voice(args) -> int:
    """Записывает образец голоса для клонирования: из Gemini TTS или из готового файла."""
    import soundfile as sf

    from narrator import gemini, models, synth
    from narrator.stress import strip_marks

    # M1 — образец из Supertonic, его не перезаписывают.
    stems = {stem: voice for voice, stem in synth.CLONE_VOICES.items() if voice != "M1"}
    stem = args.name.lower()
    if stem not in stems:
        return fail(f"голос {args.name!r} не записывается, есть: {', '.join(stems)}")
    voice = stems[stem]
    if args.text:
        # Свой текст образца (отрывок готовой озвучки): ударения ставит RUAccent.
        from narrator.stress import Accentor

        try:
            marked = Accentor()(args.text.strip()).strip()
        except RuntimeError as e:
            return fail(str(e), 3)
    else:
        # Фраза та же, что у образца M1: голоса сравниваются на одном тексте.
        marked = synth.REF_TEXT.read_text(encoding="utf-8").strip()
    phrase = strip_marks(marked).strip()
    if args.source:
        src = Path(args.source).expanduser()
        if not src.is_file():
            return fail(f"{src}: нет файла")
        try:
            audio, sr = sf.read(src, dtype="float32", always_2d=True)
        except RuntimeError as e:
            return fail(f"{src}: не читается как аудио ({e})")
        audio = audio.mean(axis=1)
    elif voice not in synth.GEMINI_VOICES:
        return fail(
            f"голоса {voice} в Gemini TTS нет: передай запись и её текст — "
            f"./narrate voice {stem} --from <файл> --text «…»"
        )
    else:
        key = gemini.api_key()
        if not key:
            return fail(
                f"нет ключа Gemini: задай $GEMINI_API_KEY или озвучь в Gemini TTS голосом "
                f"{voice} фразу «{phrase}» и передай файл: ./narrate voice {stem} --from <файл>",
                2,
            )
        try:
            audio, sr = gemini.speak(phrase, voice, key)
        except gemini.GeminiError as e:
            return fail(str(e), 3)
    seconds = len(audio) / sr
    low, high = synth.REF_SECONDS
    if not low <= seconds <= high:
        return fail(f"образец длится {seconds:.1f} с, нужно от {low:g} до {high:g} с: «{phrase}»")
    out = synth.voices_dir()
    try:
        out.mkdir(parents=True, exist_ok=True)
        sf.write(out / f"{stem}.wav", audio, sr, subtype="PCM_16")
        (out / f"{stem}.txt").write_text(marked + "\n", encoding="utf-8")
    except OSError as e:
        hint = "" if models.models_root().exists() else f": {models.missing_hint()}"
        return fail(f"образец не записан в {out} ({e}){hint}", 3)
    engines = ", ".join(synth.CLONE_ENGINES)
    print(f"{out / f'{stem}.wav'}  {seconds:.1f} с")
    print(f"голос {voice} готов для {engines}: --voice {voice}")
    return 0


def cmd_setup(args) -> int:
    if args.mlx:
        return setup_mlx(args.mlx)
    from narrator.synth import download_model

    print(f"модель синтеза: {download_model()}")
    if args.verify:
        from faster_whisper import download_model as download_whisper

        from narrator.verify import WHISPER_MODEL, WHISPER_REVISION, whisper_dir

        path = download_whisper(WHISPER_MODEL, str(whisper_dir()), revision=WHISPER_REVISION)
        print(f"модель распознавания: {path}")
    return 0


def cmd_fetch(args) -> int:
    from narrator.fetch import FetchError, fetch

    try:
        article = fetch(args.url)
    except FetchError as e:
        return fail(str(e), 2)
    entry = Entry(
        title=article.title or args.url,
        added=library.today(),
        url=args.url,
        author=article.author,
        published=article.published,
        source_quality="verbatim",
    )
    path = library.create_entry(library.library_root(), entry, args.slug)
    (path / SOURCE).write_text(article.to_markdown(), encoding="utf-8")
    library.write_index(library.library_root())
    print(path)
    return 0


def cmd_new(args) -> int:
    entry = Entry(
        title=args.title,
        added=library.today(),
        url=args.url,
        source_quality=args.quality,
    )
    path = library.create_entry(library.library_root(), entry, args.slug)
    library.write_index(library.library_root())
    print(path)
    return 0


def lint_file(path: Path) -> tuple[str, list[script.Issue]]:
    text = path.read_text(encoding="utf-8")
    issues = script.lint(text)
    for issue in issues:
        print(f"{path.name}:{issue}")
    errors = sum(i.level == "error" for i in issues)
    print(f"{path.name}: ошибок {errors}, предупреждений {len(issues) - errors}", file=sys.stderr)
    return text, issues


def speakable_text(path: Path) -> str:
    """Текст файла, приведённый к произношению; печатает, что изменилось и что осталось."""
    from narrator.normalize import changed, normalize

    raw = path.read_text(encoding="utf-8")
    text = normalize(raw)
    lines = changed(raw, text)
    if lines:
        print(f"{path.name}: к произношению приведено строк: {lines}", file=sys.stderr)
    for issue in script.lint(text):
        print(f"{path.name}:{issue}", file=sys.stderr)
    return text


def cmd_lint(args) -> int:
    p = Path(args.target)
    path = p if p.is_file() else resolve_dir(args.target) / SCRIPT
    if not path.exists():
        return fail(f"{path}: нет файла")
    _, issues = lint_file(path)
    return 1 if script.has_errors(issues) else 0


def settings_from(args):
    from narrator.synth import Settings

    return Settings(engine=args.engine, voice=args.voice, speed=args.speed, steps=args.steps)


def synth_entry(d: Path, synth, title: str = "", notify: bool = True) -> int:
    """Озвучивает каталог статьи уже созданным синтезатором (модель грузится один раз)."""
    from narrator.synth import CLONE_ENGINES, ModelMissingError, encode_mp3, render

    meta = Entry.load(d / META_NAME)
    if not (d / SCRIPT).exists():
        return fail(f"{d / SCRIPT}: нет текста для озвучки")
    if not meta.summary.strip():
        return fail(f"{d / META_NAME}: пустой summary — опиши статью для реестра")
    # Цифры, латиницу и сокращения нормализатор переводит сам: сырой текст не ошибка.
    blocks = script.parse(speakable_text(d / SCRIPT))
    if not blocks:
        return fail(f"{d / SCRIPT}: текст для озвучки пуст")
    try:
        bad = synth.check_chars(blocks)
        if bad:
            return fail(f"модель не знает символов: {bad}")
        cache = d / CACHE
        wav = cache / "full.wav"
        duration = render(blocks, wav, cache, synth, title=title)
    except (ModelMissingError, RuntimeError) as e:
        return fail(str(e), 3)
    s = synth.settings
    audio = d / library.audio_name(d, s.engine)
    encode_mp3(wav, audio, f"{meta.title} ({s.engine})")
    wav.unlink()

    meta.audio = audio.name
    meta.versions[s.engine] = round(duration, 1)
    meta.duration_sec = round(duration, 1)
    meta.engine = s.engine
    meta.voice = s.effective_voice + (" (клон)" if s.engine in CLONE_ENGINES else "")
    meta.save(d / META_NAME)
    index = library.write_index(d.parent.parent)
    print(f"{audio}  {library.format_duration(duration)}")
    print(f"реестр: {index}")
    if notify:
        notify_subscribers(d, s.engine)
    return 0


def cmd_synth(args) -> int:
    from narrator.synth import make_synth

    return synth_entry(
        resolve_dir(args.dir), make_synth(settings_from(args)), notify=not args.no_send
    )


def say_dir() -> Path:
    """Пробы `say`: $NARRATOR_SAY_DIR или tmp/say рядом с пакетом (tmp/ в .gitignore)."""
    import os

    env = os.environ.get("NARRATOR_SAY_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parents[2] / "tmp" / "say"


def say_name(path: Path) -> str:
    """Имя пробы для файла: «2026-10-08-digest-narration» для narration.md дайджеста."""
    parent = path.resolve().parent
    owner = library.article_id(parent) if (parent / META_NAME).exists() else parent.name
    return library.slugify(f"{owner}-{path.stem}", limit=80)


def cmd_normalize(args) -> int:
    """Показывает, каким текст уйдёт в модель."""
    from narrator.normalize import normalize

    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    sys.stdout.write(normalize(raw))
    return 0


def cmd_say(args) -> int:
    """Озвучивает любой текстовый файл как есть: без каталога статьи, реестра и телеграма."""
    import shutil

    from narrator.synth import DEFAULT_VOICES, ModelMissingError, encode_mp3, make_synth, render

    src = Path(args.file).expanduser()
    if not src.is_file():
        return fail(f"{src}: нет файла")
    work = say_dir() / say_name(src)
    work.mkdir(parents=True, exist_ok=True)
    text = speakable_text(src)
    blocks = script.parse(text)
    if not blocks:
        return fail(f"{src}: текст для озвучки пуст")
    (work / SCRIPT).write_text(text, encoding="utf-8")
    synth = make_synth(settings_from(args))
    cache = work / CACHE
    try:
        bad = synth.check_chars(blocks)
        if bad:
            return fail(f"модель не знает символов: {bad}")
        duration = render(blocks, cache / "full.wav", cache, synth)
    except (ModelMissingError, RuntimeError) as e:
        return fail(str(e), 3)
    finally:
        synth.close()
    s = synth.settings
    # Голос не по умолчанию — в имени: пробы разных голосов одного движка лежат рядом.
    tag = s.engine if s.effective_voice == DEFAULT_VOICES[s.engine] else f"{s.engine}-{s.voice}"
    audio = work / f"{work.name}_{tag.lower()}.mp3"
    encode_mp3(cache / "full.wav", audio, blocks[0].text[:80])
    (cache / "full.wav").unlink()
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(audio, out)
        audio = out
    print(f"{audio}  {library.format_duration(duration)}")
    if args.no_verify:
        return 0
    from narrator.verify import Transcriber, check, report, summary

    try:
        transcriber = Transcriber()
    except RuntimeError as e:
        print(f"проверка пропущена: {e}", file=sys.stderr)
        write_check_json(args.check_json, {"skipped": str(e)})
        return 0
    results = check(cache, transcriber)
    result = report(results, args.threshold)
    (work / CHECK).write_text(result, encoding="utf-8")
    write_check_json(args.check_json, summary(results, args.threshold))
    print(result.split("\n\n")[1])
    print(f"отчёт: {work / CHECK}")
    return 0


def write_check_json(target: str | None, data: dict) -> None:
    if target:
        import json

        Path(target).expanduser().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def verify_entry(d: Path, transcriber, threshold: float) -> int:
    from narrator.synth import MANIFEST
    from narrator.verify import check, report

    if not (d / CACHE / MANIFEST).exists():
        return fail("сначала narrator synth")
    text = report(check(d / CACHE, transcriber), threshold)
    (d / CHECK).write_text(text, encoding="utf-8")
    print(text.split("\n\n")[1])
    print(f"отчёт: {d / CHECK}")
    return 0


def cmd_verify(args) -> int:
    from narrator.verify import Transcriber

    try:
        transcriber = Transcriber()
    except RuntimeError as e:
        return fail(str(e), 3)
    return verify_entry(resolve_dir(args.dir), transcriber, args.threshold)


def cmd_render(args) -> int:
    """Все статьи, что ждут озвучки: synth + verify по каждой. Дайджесты — news render."""
    return render_pending(args, ("article",), "статья")


def render_pending(args, kinds: tuple[str, ...], noun: str) -> int:
    """synth + verify по каждому каталогу этих видов, где script.txt новее аудио."""
    from narrator.synth import make_synth
    from narrator.verify import Transcriber

    settings = settings_from(args)
    root = library.library_root()
    todo = library.pending(root, settings.engine, force=args.all, kinds=kinds)
    if not todo:
        print("озвучивать нечего: у всех каталогов с script.txt аудио свежее")
        return 0
    print(f"к озвучке {len(todo)}: " + ", ".join(library.article_id(p) for p in todo))
    synth = make_synth(settings)
    transcriber = None
    failed = []
    for k, d in enumerate(todo, start=1):
        name = library.article_id(d)
        print(f"\n=== {k}/{len(todo)} {name}", flush=True)
        if synth_entry(d, synth, f"{noun} {k}/{len(todo)},", notify=not args.no_send) != 0:
            failed.append(name)
            continue
        if args.no_verify:
            continue
        if transcriber is None:
            try:
                transcriber = Transcriber()
            except RuntimeError as e:
                print(f"проверка пропущена: {e}", file=sys.stderr)
                args.no_verify = True
                continue
        verify_entry(d, transcriber, args.threshold)
    if failed:
        return fail("не озвучены: " + ", ".join(failed))
    print("\nГотово. Скажи агенту, чтобы разобрал check.md.")
    return 0


def cmd_index(args) -> int:
    print(library.write_index(library.library_root()))
    return 0


# --- телеграм-бот --------------------------------------------------------


def report_delivery(done: list) -> None:
    ok = [d for d in done if d.ok]
    print(f"телеграм: озвучка отправлена {len(ok)} из {len(done)}")
    for d in done:
        if not d.ok:
            print(f"  не дошло {d.chat_id} {d.name}: {d.error}", file=sys.stderr)


def save_state(state, path: Path) -> int:
    """Сохраняет список подписчиков; 4 — если файл не записался."""
    try:
        state.save(path)
    except OSError as e:
        return fail(f"не записать {path}: {e}", 4)
    return 0


def notify_subscribers(d: Path, engine: str) -> None:
    """Шлёт свежую озвучку в канал или подписчикам. Сбой телеграма не валит озвучку."""
    from narrator import telegram

    try:
        bot = telegram.Bot(telegram.read_token())
    except telegram.TelegramError as e:
        print(f"телеграм: рассылка пропущена — {e}", file=sys.stderr)
        return
    path = telegram.state_path()
    state = telegram.State.load(path)
    try:
        telegram.pull(bot, state)  # вдруг кто-то подписался, пока шла озвучка
        meta = Entry.load(d / META_NAME)
        if not telegram.recipients(state, meta):
            print("телеграм: подписчиков нет — рассылать некому")
            return
        report_delivery(telegram.broadcast(bot, state, d, meta, engine))
    except telegram.TelegramError as e:
        print(f"телеграм: {e}", file=sys.stderr)
    finally:
        save_state(state, path)


def open_bot():
    """Бот и сохранённое состояние; TelegramError — если токена нет."""
    from narrator import telegram

    path = telegram.state_path()
    return telegram.Bot(telegram.read_token()), telegram.State.load(path), path


def cmd_bot_subscribers(args) -> int:
    from narrator import telegram

    try:
        bot, state, path = open_bot()
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    try:
        if not args.no_sync:
            for sub in telegram.pull(bot, state):
                print(f"новый подписчик: {sub.chat_id} {sub.name}")
    except telegram.TelegramError as e:
        print(f"телеграм: {e}", file=sys.stderr)
    if save_state(state, path):
        return 4
    live = state.active()
    print(f"подписчиков {len(live)}, отписалось {len(state.subscribers) - len(live)}")
    for s in state.subscribers:
        note = f"с {s.added}" if s.active else f"неактивен: {s.note}"
        print(f"  {s.chat_id:<12} {s.name:<28} {note}")
    print(f"список: {path}")
    return 0


def cmd_bot_audience(args) -> int:
    """Аудитория бота агрегатами (AN-011): подписки по источнику, retention D7/D30 по когортам.
    Ни chat_id, ни имён — только числа; сеть не нужна."""
    import json

    from narrator import telegram

    report = telegram.audience(telegram.State.load(telegram.state_path()))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"подписано сейчас: {report['active']}")
    print("подписки по источнику (метка /start):")
    for source, row in report["by_source"].items():
        print(f"  {source:<32} подписалось {row['subscribed']:>4}, осталось {row['active']:>4}")
    print("retention по недельным когортам (доля оставшихся; n — кому исполнилось 7/30 дней):")

    def share(row: dict, days: int) -> str:
        value = row[f"d{days}"]
        return "—" if value is None else f"{value:.0%} (n={row[f'd{days}_n']})"

    for row in report["cohorts"]:
        week = f"неделя с {row['cohort']}: {row['n']:>4}"
        print(f"  {week}  D7 {share(row, 7):<14} D30 {share(row, 30)}")
    return 0


def cmd_bot_channels(args) -> int:
    """Каналы из telegram_tems.md: по имени их называют в --channel."""
    from narrator import telegram

    try:
        known = telegram.channels()
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    usual = {
        telegram.channel_name(telegram.ARTICLE_CHANNEL): "озвученные статьи",
        telegram.channel_name(telegram.NEWS_CHANNEL): "дайджест новостей",
    }
    for name, chat in known.items():
        print(f"  {name:<16} {chat:<22} {usual.get(name, '')}".rstrip())
    missing = [name for name in usual if name not in known]
    if missing:
        return fail(f"в {telegram.channels_path()} нет каналов: {', '.join(missing)}", 4)
    print(f"файл: {telegram.channels_path()}")
    return 0


def send_article(bot, state, args, d: Path, meta: Entry, engine: str) -> int:
    from narrator import telegram

    tag = telegram.sent_tag(meta, args.channel)  # заодно проверит имя канала
    if not args.no_sync:
        telegram.pull(bot, state)
    targets = None
    if args.to:
        targets = [s for s in state.subscribers if s.chat_id in args.to]
        missing = set(args.to) - {s.chat_id for s in targets}
        if missing:
            return fail(f"нет таких подписчиков: {sorted(missing)}")
    elif not telegram.recipients(state, meta, args.channel):
        print("телеграм: подписчиков нет — рассылать некому")
        return 0
    if not args.force and not telegram.is_fresh(state, d, engine, tag):
        print(f"телеграм: {library.article_id(d)} ({engine}) уже отправлена, повтор — --force")
        return 0
    annotation = getattr(args, "annotation", False)
    report_delivery(
        telegram.broadcast(bot, state, d, meta, engine, targets, args.channel, annotation)
    )
    return 0


def send_folder(args, d: Path) -> int:
    """Папка без meta.json — не статья: mp3, картинка и *post.md уходят в названный канал.

    Как дайджест новостей: mp3 и картинка склеиваются в видео, пост — подпись
    под ним. --as photo — картинка с постом, затем аудио.
    """
    from narrator import registry, telegram
    from narrator.news.cli import audio_seconds
    from narrator.news.video import VideoError, make_frames, make_video

    if not args.channel:
        return fail(f"{d}: нет {META_NAME} — такую папку шлют в названный канал: --channel <имя>")
    parcel = telegram.find_parcel(d)
    found = {"аудио": parcel.audio, "картинка": parcel.image, "пост": parcel.post}
    for label, file in found.items():
        print(f"{label}: {file.name if file else 'нет'}")
    try:
        tag = telegram.folder_tag(args.channel)
        bot, state, path = open_bot()
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    if not args.force and not telegram.is_fresh(state, d, telegram.FOLDER_ENGINE, tag):
        print(f"телеграм: {d.name} уже отправлена в этот канал, повтор — --force")
        return 0
    seconds = audio_seconds(parcel.audio) if parcel.audio else 0
    video = covers = None
    if args.as_ != "photo" and parcel.audio and parcel.image:
        try:
            notes = registry.notes(parcel.text(), registry.load())  # пометки — в кадре
            poster, thumb, frame = make_frames(parcel.image, notes)
            video = make_video(poster, frame, parcel.audio, d / f"{d.name}.mp4", seconds)
        except VideoError as e:
            return fail(str(e), 5)
        covers = (poster, thumb)
        print(f"видео: {video.name} ({video.stat().st_size / 2**20:.1f} МБ)")
    elif args.as_ != "photo":
        print("видео не собрать: нужны и mp3, и картинка — шлю что есть")
    try:
        done = telegram.send_folder(bot, state, d, args.channel, seconds, video, covers)
        report_delivery(done)
        code = 0
    except telegram.TelegramError as e:
        code = fail(str(e), 4)
    return save_state(state, path) or code


def cmd_bot_send(args) -> int:
    from narrator import telegram

    folder = Path(args.dir).expanduser()
    if folder.is_dir() and not (folder / META_NAME).exists():
        return send_folder(args, folder.resolve())
    d = resolve_dir(args.dir)
    meta = Entry.load(d / META_NAME)
    engine = args.engine or meta.engine or (sorted(meta.versions) or [None])[0]
    if not engine:
        return fail(f"{d}: статья ещё не озвучена")
    try:
        bot, state, path = open_bot()
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    try:
        code = send_article(bot, state, args, d, meta, engine)
    except telegram.TelegramError as e:
        code = fail(str(e), 4)
    return save_state(state, path) or code


def cmd_bot_unsent(args) -> int:
    """Догоняет рассылку: всё, что озвучено, но в телеграм не ушло."""
    from narrator import telegram

    try:
        bot, state, path = open_bot()
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    todo = telegram.unsent(state, library.library_root())
    if not todo:
        print("телеграм: неотправленных озвучек нет")
        return 0
    print(f"не отправлено {len(todo)}:")
    for d, meta, engine in todo:
        print(f"  {library.article_id(d)} ({engine}) {meta.title}")
    if args.list:
        return 0
    if args.mark:
        for d, _, engine in todo:
            telegram.mark_sent(state, d, engine)
        print(f"отмечено отправленными {len(todo)}, в телеграм ничего не ушло")
        return save_state(state, path)
    code = 0
    try:
        telegram.pull(bot, state)
        for k, (d, meta, engine) in enumerate(todo, start=1):
            if not telegram.recipients(state, meta):
                print(f"телеграм: {library.article_id(d)} — рассылать некому")
                continue
            if meta.kind == "article" and meta.license not in library.PUBLISHABLE:
                print(f"телеграм: {library.article_id(d)} — license={meta.license}, пропущена")
                continue  # без права на переработку — только локально или аннотацией вручную
            print(f"{k}/{len(todo)} {library.article_id(d)}", flush=True)
            done = telegram.broadcast(bot, state, d, meta, engine)
            report_delivery(done)
            save_state(state, path)  # отметки не пропадут, если сеть отвалится посреди
            if done and not any(x.ok for x in done):
                code = fail("не дошло — остальное не шлю, повтори позже", 4)
                break
    except telegram.TelegramError as e:
        code = fail(f"{e} — повтори позже, отправленное уже отмечено", 4)
    return save_state(state, path) or code


def cmd_bot_serve(args) -> int:
    """Долгий опрос: принимает /start и /stop, пока не остановят Ctrl+C."""
    from narrator import telegram

    try:
        bot, state, path = open_bot()
        me = bot.call("getMe") or {}
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    # getUpdates — у одного процесса: второй serve выходит, остальные команды pull пропускают.
    try:
        with telegram.serving():
            print(f"бот @{me.get('username', '?')} слушает подписки, выход — Ctrl+C")
            return _serve(bot, state, path, args)
    except telegram.TelegramError as e:  # _serve свои ошибки ловит сам — это занятый serve
        return fail(str(e), 4)


def _serve(bot, state, path: Path, args) -> int:
    from narrator import telegram

    code = 0
    on_update = None
    if getattr(args, "updates", False):
        import json

        def on_update(update: dict) -> None:
            # Платформа читает вывод serve построчно: JSON без переводов строк.
            print("update " + json.dumps(update, ensure_ascii=False), flush=True)

    try:
        while True:
            timeout = 0 if args.once else telegram.POLL_TIMEOUT
            for sub in telegram.pull(bot, state, timeout=timeout, on_update=on_update):
                print(f"подписался: {sub.chat_id} {sub.name}", flush=True)
            if save_state(state, path):
                return 4
            if args.once:
                break
    except KeyboardInterrupt:
        print("\nостановлен")
    except telegram.TelegramError as e:
        code = fail(str(e), 4)
    return save_state(state, path) or code


def speed_arg(value: str) -> float:
    speed = float(value)
    if not 0.7 <= speed <= 2.0:
        raise argparse.ArgumentTypeError("скорость — от 0.7 до 2.0")
    return speed


def add_engine_args(s: argparse.ArgumentParser) -> None:
    import os

    from narrator.synth import DEFAULT_ENGINE, DEFAULT_VOICES, ENGINE_VOICES, ENGINES

    s.add_argument(
        "--engine",
        choices=ENGINES,
        default=os.environ.get("NARRATOR_ENGINE", DEFAULT_ENGINE),
        help="espeech — лучшее качество, нужна видеокарта; silero — очень быстро на CPU, "
        "лицензия CC BY-NC; supertonic — быстро на CPU (по умолчанию $NARRATOR_ENGINE "
        "или espeech)",
    )
    voices = "; ".join(
        f"{e}: {', '.join(v)} (по умолчанию {DEFAULT_VOICES[e]})" for e, v in ENGINE_VOICES.items()
    )
    s.add_argument("--voice", help=f"голос — {voices}")
    s.add_argument(
        "--speed",
        type=speed_arg,
        help="по умолчанию: espeech 1.0, supertonic 1.05; у silero не меняется",
    )
    s.add_argument("--steps", type=int, help="по умолчанию: espeech 32, supertonic 8")


def build_parser() -> argparse.ArgumentParser:
    from narrator.verify import DEFAULT_THRESHOLD

    p = argparse.ArgumentParser(prog="narrator", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="скачать модели (нужен доступ к huggingface.co)")
    s.add_argument("--verify", action="store_true", help="и модель распознавания для verify")
    s.add_argument(
        "--mlx",
        nargs="+",
        metavar="ДВИЖОК",
        help="скачать модели для сравнения: higgs, fish, moss, qwen3, chatterbox или all",
    )
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser(
        "voice",
        help="записать образец голоса для клонирования: charon — голос Charon из Gemini TTS "
        "(нужны сеть и $GEMINI_API_KEY), shem и shem-v2 — из своей записи через --from и --text",
    )
    s.add_argument("name", help="charon, shem или shem-v2")
    s.add_argument(
        "--from",
        dest="source",
        metavar="ФАЙЛ",
        help="готовая запись фразы образца этим голосом (wav, mp3) вместо запроса к Gemini",
    )
    s.add_argument(
        "--text",
        help="что сказано в образце, слово в слово, цифры словами; по умолчанию — фраза образца M1",
    )
    s.set_defaults(func=cmd_voice)

    s = sub.add_parser("fetch", help="скачать статью: каталог в библиотеке + source.md")
    s.add_argument("url")
    s.add_argument("--slug", help="имя каталога латиницей; по умолчанию из заголовка")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("new", help="каталог статьи без скачивания (текст прислали или WebFetch)")
    s.add_argument("--title", required=True)
    s.add_argument("--url")
    s.add_argument("--slug")
    s.add_argument("--quality", choices=library.SOURCE_QUALITIES, default="pasted")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("lint", help="проверить script.txt по правилам озвучки")
    s.add_argument("target", help="каталог статьи или путь к файлу")
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser(
        "synth", help="озвучить script.txt в <каталог>_<движок>.mp3 и обновить реестр"
    )
    s.add_argument("dir")
    add_engine_args(s)
    s.add_argument("--no-send", action="store_true", help="не отправлять озвучку в телеграм")
    s.set_defaults(func=cmd_synth)

    s = sub.add_parser(
        "say", help="озвучить любой текстовый файл как есть: цифры и латиницу переведёт сам"
    )
    s.add_argument("file", help="файл с текстом: .md, .txt")
    add_engine_args(s)
    s.add_argument("--out", help="куда положить mp3 (по умолчанию tmp/say/<имя>/)")
    s.add_argument("--no-verify", action="store_true", help="не проверять распознаванием")
    s.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    s.add_argument(
        "--check-json", metavar="ФАЙЛ", help="отчёт проверки ещё и JSON-ом (для страниц)"
    )
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("normalize", help="показать текст так, как он уйдёт в модель")
    s.add_argument("file", help="файл с текстом или - для stdin")
    s.set_defaults(func=cmd_normalize)

    s = sub.add_parser("verify", help="распознать озвучку и найти искажённые места")
    s.add_argument("dir")
    s.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser(
        "render",
        help="озвучить и проверить все статьи, где script.txt новее аудио или аудио нет",
    )
    add_engine_args(s)
    s.add_argument("--all", action="store_true", help="переозвучить все статьи")
    s.add_argument("--no-verify", action="store_true", help="без проверки распознаванием")
    s.add_argument("--no-send", action="store_true", help="не отправлять озвучку в телеграм")
    s.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("index", help="пересобрать library/index.md")
    s.set_defaults(func=cmd_index)

    from narrator.news.cli import add_parser as add_news_parser

    add_news_parser(sub)

    from narrator.bridge import add_parser as add_bridge_parser

    add_bridge_parser(sub)

    s = sub.add_parser("bot", help="телеграм-бот: подписчики и рассылка озвучек")
    bot = s.add_subparsers(dest="bot_cmd", required=True)

    b = bot.add_parser("subscribers", help="кто подписан на бота")
    b.add_argument("--no-sync", action="store_true", help="не забирать новые сообщения")
    b.set_defaults(func=cmd_bot_subscribers)

    b = bot.add_parser(
        "audience", help="подписки по источнику и retention D7/D30 — агрегаты без ПДн"
    )
    b.add_argument("--json", action="store_true", help="отчёт в JSON")
    b.set_defaults(func=cmd_bot_audience)

    b = bot.add_parser("send", help="отправить озвучку статьи в канал (или подписчикам)")
    b.add_argument("dir")
    b.add_argument(
        "--annotation",
        action="store_true",
        help="вместо озвучки — аннотация со ссылкой (статья без права на переработку)",
    )
    b.add_argument("--engine", help="какую версию слать; по умолчанию последняя озвученная")
    where = b.add_mutually_exclusive_group()
    where.add_argument(
        "--to", type=int, action="append", help="только этому chat_id, можно повторять"
    )
    where.add_argument(
        "--channel", help="в этот канал вместо обычного: имя из telegram_tems.md или id"
    )
    b.add_argument(
        "--as",
        dest="as_",
        choices=("video", "photo"),
        help="папка без meta.json: video — mp4 с постом в подписи одним постом (по"
        " умолчанию); photo — картинка с постом, затем аудио",
    )
    b.add_argument("--force", action="store_true", help="даже если эту озвучку уже слали")
    b.add_argument("--no-sync", action="store_true", help="не забирать новые сообщения")
    b.set_defaults(func=cmd_bot_send)

    b = bot.add_parser("channels", help="каналы из telegram_tems.md: имена для --channel")
    b.set_defaults(func=cmd_bot_channels)

    b = bot.add_parser("unsent", help="отправить все озвучки, что ещё не ушли в телеграм")
    mode = b.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="только показать, не отправлять")
    mode.add_argument("--mark", action="store_true", help="отметить отправленными, не посылая")
    b.set_defaults(func=cmd_bot_unsent)

    b = bot.add_parser("serve", help="принимать подписки: /start и /stop")
    b.add_argument("--once", action="store_true", help="один заход вместо долгого опроса")
    b.add_argument(
        "--updates",
        action="store_true",
        help="нажатия кнопок и ответы боту — строкой `update {json}` (для платформы)",
    )
    b.set_defaults(func=cmd_bot_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    # Падение в нативном коде (torch/MPS, onnxruntime) — с питоновским стеком в выводе.
    try:
        faulthandler.enable()
    except (OSError, ValueError, AttributeError):  # stderr без дескриптора, как под pytest
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
