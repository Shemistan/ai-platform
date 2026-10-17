"""`narrate news …` — сбор, разбор, отбор и дайджест. Регламент — CLAUDE.md,
раздел «Дайджест новостей»."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from narrator import library, registry
from narrator.library import META_NAME, Entry
from narrator.news import designs, digest, feeds, sources
from narrator.news.store import RETENTION_DAYS, Store, db_path, now_utc

EVENTS = "events.md"
POST = "post.md"
READING = "text.md"  # дайджест для чтения: обычный текст, не для озвучки
NARRATION = "narration.md"  # пересказ для озвучки в Gemini TTS, mp3 делает человек
AUDIO_ENGINE = "gemini"  # mp3 дайджеста — <день>-digest_gemini.mp3
DIGEST_SLUG = "digest"
TELEPROMPTER = "teleprompter.md"  # обзор недели или месяца: текст, который ведущий читает в кадре
MATERIALS = "materials.md"  # что вставить при монтаже по меткам [M1] из суфлёра
# Вид поста дайджеста: video — mp4 одним постом, photo — обложка, затем аудио.
POST_FORMATS = ("video", "photo")
POST_ENV = "NARRATOR_NEWS_POST"
MONTHS = (
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
).split()
MONTHS_NOM = (
    "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь"
).split()


def fail(msg: str, code: int = 1) -> int:
    print(f"narrator news: {msg}", file=sys.stderr)
    return code


def open_store() -> Store:
    store = Store(db_path(library.library_root()))
    if store.memo().get("scoring") != digest.SCORING_VERSION:
        rescore(store, sources.by_id(load_sources()))
    return store


def rescore(store: Store, srcs: dict) -> int:
    """Пересчитать балл и важность всех оценок по текущей формуле."""
    n = 0
    for row in store.assessed():
        src = srcs.get(row["source"])
        factors = digest.with_insight(json.loads(row["factors"] or "{}"), row["kind"] or "")
        if any(k not in factors for k in digest.FACTORS):
            continue
        authority = src.authority if src else (row["authority"] or 0.5)
        value = digest.score(factors, authority)
        level = digest.final_importance(
            value,
            bool(src and src.secondary),
            row["confirmed_by"],
            row["kind"] or "",
            factors,
            src.max_importance if src else digest.SCALE,
        )
        store.rescore(row["id"], factors, value, level)
        n += 1
    store.save_memo({"scoring": digest.SCORING_VERSION})
    return n


def recent_vendors(store: Store, srcs: dict, day: date) -> set[str]:
    """Поставщики главного в выпусках за VENDOR_RECENT_DAYS дней до day."""
    root = library.library_root()
    marks = [
        library.article_id(library.day_dir(root, day - timedelta(days=n)) / DIGEST_SLUG)
        for n in range(1, digest.VENDOR_RECENT_DAYS + 1)
    ]
    out = set()
    for source in store.issue_sources(marks):
        src = srcs.get(source)
        if src and not src.secondary:
            out.add(src.vendor_key)
    return out


def agent_of(src: sources.Source, entries: list[registry.Entry]) -> str | None:
    """id иноагента из реестра, если источник — его канал (CMP-012)."""
    places = [src.url, src.feed or "", src.channel or "", src.handle or ""]
    found = registry.source_agent(places, src.name, entries)
    return found.id if found else None


def load_sources() -> list[sources.Source]:
    try:
        return sources.load()
    except sources.ConfigError as e:
        raise SystemExit(fail(str(e), 2)) from e


def cmd_collect(args) -> int:
    day = parse_day(args.date)
    if day is not None:  # материал нужного дня не должен уйти в архив как старый
        args.days = max(args.days, (date.today() - day).days + 2)
    srcs = [s for s in load_sources() if s.enabled]
    if args.only:
        unknown = set(args.only) - {s.id for s in srcs}
        if unknown:
            return fail(f"нет таких включённых источников: {sorted(unknown)}")
        srcs = [s for s in srcs if s.id in args.only]
    total_new, failed, blocked = 0, [], []
    agents = registry.load()
    with open_store() as store:
        memo = store.memo()
        for src in srcs:
            now = now_utc()
            try:
                items = feeds.fetch(src, memo=memo)
            except feeds.FetchError as e:
                if feeds.SANDBOX_BLOCK in str(e):  # сеть закрыл sandbox, а не источник
                    blocked.append(src.id)
                    failed.append(src.id)
                    continue
                store.log_run(src.id, now, ok=False, error=str(e))
                failed.append(src.id)
                print(f"  сбой  {src.id:<26} {e}", flush=True)
                continue
            fresh = store.add(src, items, now, args.days, agent=agent_of(src, agents))
            store.log_run(src.id, now, ok=True, found=len(items), new=fresh)
            total_new += fresh
            print(f"  ок    {src.id:<26} материалов {len(items):>3}, новых {fresh}", flush=True)
        store.save_memo(memo)
        pruned = store.prune(now_utc())  # ретенция в ежедневном сборе (CMP-016)
    if pruned:
        print(f"  в архив старше {RETENTION_DAYS} дней: {pruned}")
    if blocked:
        print(
            f"  закрыто sandbox {len(blocked)} — в журнал не пишется, чтобы не затереть"
            " последний настоящий сбор"
        )
    print(f"\nисточников {len(srcs)}, со сбоем {len(failed)}, новых материалов {total_new}")
    if failed and len(failed) == len(srcs):
        return fail("ни один источник не ответил — нет сети? В sandbox агента она закрыта", 2)
    if len(failed) > len(blocked):
        print("упавшие источники агент добирает сам: narrate news failed → news add")
    return 0


def cmd_failed(args) -> int:
    """Включённые источники со сбоем последнего захода — агенту на добор."""
    srcs = [s for s in load_sources() if s.enabled]
    with open_store() as store:
        failed = store.failed()
    rows = [
        {
            "source": s.id,
            "name": s.name,
            "kind": s.kind,
            "url": s.url,
            "feed": s.feed,
            "at": failed[s.id]["at"],
            "error": failed[s.id]["error"],
        }
        for s in srcs
        if s.id in failed
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("источников со сбоем нет: добирать нечего")
        return 0
    print(f"# Источники со сбоем: {len(rows)}\n")
    print("Добор — CLAUDE.md, «Дайджест новостей», шаг «Добор». Материалы → news add.\n")
    for r in rows:
        print(f"- `{r['source']}` [{r['name']}, {r['kind']}] {r['url']}")
        if r["feed"]:
            print(f"  лента: {r['feed']}")
        print(f"  {r['at'][:16].replace('T', ' ')}: {r['error']}")
    return 0


def parse_rescue(data) -> tuple[dict[str, list[feeds.Item]], list[str], list[str]]:
    """JSON добора → материалы по источникам, проверенные без находок и ошибки."""
    if not isinstance(data, dict):
        return {}, [], ['ждём объект {"items": [...], "checked": [...]}']
    found: dict[str, list[feeds.Item]] = {}
    errors = []
    for n, raw in enumerate(data.get("items") or []):
        where = f"items[{n}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: ждём объект")
            continue
        missing = [k for k in ("source", "url", "title") if not str(raw.get(k) or "").strip()]
        if missing:
            errors.append(f"{where}: нет {', '.join(missing)}")
            continue
        url = str(raw["url"]).strip()
        if not url.startswith(("http://", "https://")):
            errors.append(f"{where}: url не http(s): {url}")
            continue
        published = raw.get("published")
        iso_date = feeds.to_iso(published) if published else None
        if published and iso_date is None:
            errors.append(f"{where}: published {published!r} — ждём ГГГГ-ММ-ДД или ISO 8601")
            continue
        item = feeds.Item(
            url=url,
            title=feeds.clean(str(raw["title"]), 300),
            published=iso_date,
            summary=feeds.clean(raw.get("summary")),
        )
        found.setdefault(str(raw["source"]).strip(), []).append(item)
    checked = [str(s) for s in data.get("checked") or []]
    return found, checked, errors


def cmd_add(args) -> int:
    """Материалы упавших источников, что агент добыл сам (WebFetch, WebSearch)."""
    day = parse_day(args.date)
    if day is not None:
        args.days = max(args.days, (date.today() - day).days + 2)
    srcs = sources.by_id(load_sources())
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return fail(f"{args.file}: {e}")
    found, checked, errors = parse_rescue(data)
    unknown = sorted((set(found) | set(checked)) - set(srcs))
    for key in unknown:
        errors.append(f"{key}: нет такого источника в конфиге")
        found.pop(key, None)
    checked = [c for c in checked if c in srcs and c not in found]
    now = now_utc()
    total = 0
    agents = registry.load()
    with open_store() as store:
        failed = store.failed()
        for key, items in found.items():
            agent = agent_of(srcs[key], agents)
            fresh = store.add(srcs[key], items, now, args.days, baseline=False, agent=agent)
            error = failed.get(key, {}).get("error", "")
            store.log_rescue(key, now, error, found=len(items), new=fresh)
            total += fresh
            print(f"  добор {key:<26} материалов {len(items):>3}, новых {fresh}")
        for key in checked:
            error = failed.get(key, {}).get("error", "")
            store.log_rescue(key, now, error, found=0, new=0)
            print(f"  добор {key:<26} свежего нет")
        store.commit()
        left = sorted(set(store.failed()) & {s for s, v in srcs.items() if v.enabled})
    print(f"новых материалов {total}; без добора осталось {len(left)}")
    if left:
        print("  " + ", ".join(left))
    for e in errors:
        print(f"  ошибка: {e}", file=sys.stderr)
    return 1 if errors else 0


def cmd_where(args) -> int:
    day = parse_day(args.date) or date.today()
    d = library.day_dir(library.library_root(), day) / DIGEST_SLUG
    if not (d / NARRATION).exists():
        print(f"Дайджеста за {day.isoformat()} нет")
        return 0
    review = d / digest.REVIEW
    if review.exists():
        verdict = digest.review_verdict(review.read_text(encoding="utf-8")) or "нет вердикта"
        print(f"Судья:    {verdict} — {review}")
    else:
        print(f"Судья:    не смотрел — make news-judge DAY={day.isoformat()}")
    print(f"Читать:   {d / READING}")
    print(f"Озвучить: {d / NARRATION} → mp3 в {d}/")
    print(f"Картинка: промпт {d / 'image_prompt.md'} → ChatGPT → картинку в {d}/")
    print("Затем:    make news-publish")
    return 0


def cmd_sources(args) -> int:
    srcs = load_sources()
    with open_store() as store:
        stats = store.source_stats()
    print(f"{'источник':<26} {'вид':<8} {'вес':>4}  {'сбор':<24} {'важных/разобрано':>17}")
    for s in srcs:
        st = stats.get(s.id, {})
        last = st.get("last")
        if not s.enabled:
            state = "выключен"
        elif not last:
            state = "ещё не собирали"
        elif last["ok"]:
            state = f"ок {last['at'][:10]}, новых {last['new']}"
        else:
            state = f"СБОЙ {last['at'][:10]}"
        processed = st.get("processed", 0)
        value = f"{st.get('important', 0)}/{processed}" if processed else "—"
        print(f"{s.id:<26} {s.kind:<8} {s.authority:>4.2f}  {state:<24} {value:>17}")
        if not s.enabled and s.note:
            print(f"    {s.note}")
        elif last and not last["ok"]:
            print(f"    {last['error']}")
        if s.enabled and useless(st):
            print(
                f"    мало пользы: разобрано {processed}, в выпуски не попало ничего"
                " — понизить вес или выключить"
            )
    return 0


USELESS_AFTER = 15  # столько разобранных без единого попадания в выпуск — повод пересмотреть


def useless(stat: dict) -> bool:
    return (
        stat.get("processed", 0) >= USELESS_AFTER
        and not stat.get("important")
        and not stat.get("issued")
    )


def cmd_pending(args) -> int:
    srcs = sources.by_id(load_sources())
    with open_store() as store:
        rows = [dict(r) for r in store.pending()]
    if not rows:
        print("новых материалов нет: всё разобрано или сбор не запускали (narrate news collect)")
        return 0

    def order(row):
        src = srcs.get(row["source"])
        return (-(src.priority if src else 0), row["source"], row["published"] or "")

    rows.sort(key=order)
    shown = rows[: args.limit]
    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=1))
        return 0
    print(f"# Новые материалы: {len(shown)} из {len(rows)}\n")
    print("Формат оценки — CLAUDE.md, «Дайджест новостей». Вторичные (2°) без")
    print(f"confirmed_by получат не больше {digest.UNCONFIRMED_CAP}.\n")
    for r in shown:
        src = srcs.get(r["source"])
        name = src.name if src else r["source"]
        mark = " 2°" if src and src.secondary else ""
        weight = f"{src.authority:.2f}" if src else "?"
        when = (r["published"] or "дата неизвестна")[:16].replace("T", " ")
        print(f"- `{r['id']}` [{name}, вес {weight}{mark}] {when}")
        print(f"  {r['title']}")
        print(f"  {r['url']}")
        if r["summary"]:
            print(f"  > {r['summary'][:400]}")
    if len(rows) > len(shown):
        print(f"\nещё {len(rows) - len(shown)} — после assess снова narrate news pending")
    return 0


def cmd_assess(args) -> int:
    srcs = sources.by_id(load_sources())
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return fail(f"{args.file}: {e}")
    items, skip, errors = digest.parse_assessments(data)
    now = now_utc()
    saved = 0
    with open_store() as store:
        for a in items:
            row = store.get(a.id)
            if row is None:
                errors.append(f"{a.id}: нет такого материала")
                continue
            src = srcs.get(row["source"])
            authority = src.authority if src else 0.5
            value = digest.score(a.factors, authority)
            level = digest.final_importance(
                value,
                bool(src and src.secondary),
                a.confirmed_by,
                a.kind,
                a.factors,
                src.max_importance if src else digest.SCALE,
            )
            store.save_assessment(
                a.id,
                {
                    "kind": a.kind,
                    "event": a.event,
                    "title_ru": a.title,
                    "why": a.why,
                    "factors": a.factors,
                    "authority": authority,
                    "score": value,
                    "importance": level,
                    "confirmed_by": a.confirmed_by,
                },
                now,
            )
            saved += 1
        skipped = store.skip(skip, now)
        store.commit()
        left = len(store.pending())
    print(f"оценено {saved}, пропущено {skipped}, ждут разбора {left}")
    for e in errors:
        print(f"  ошибка: {e}", file=sys.stderr)
    return 1 if errors else 0


def cmd_rescore(args) -> int:
    with open_store() as store:
        n = rescore(store, sources.by_id(load_sources()))
        store.commit()
    print(f"пересчитано оценок: {n}, формула версии {digest.SCORING_VERSION}")
    return 0


def parse_day(value: str | None) -> date | None:
    """--date: ГГГГ-ММ-ДД, today/сегодня, yesterday/вчера; None — не задан."""
    if not value:
        return None
    word = value.strip().lower()
    if word in ("today", "сегодня"):
        return date.today()
    if word in ("yesterday", "вчера"):
        return date.today() - timedelta(days=1)
    try:
        return date.fromisoformat(word)
    except ValueError as e:
        msg = f"--date {value!r}: ждём ГГГГ-ММ-ДД, today или yesterday"
        raise SystemExit(fail(msg, 2)) from e


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Сутки day по местному времени: полночь — полночь."""
    start = datetime(day.year, day.month, day.day)
    return start.astimezone(), (start + timedelta(days=1)).astimezone()


def issue_rows(
    store: Store, hours: float, mark: str | None, column="digest", day: date | None = None
) -> list[dict]:
    """Материалы выпуска: опубликованные в день day или, без него, собранные за hours."""
    if day is not None:
        return [dict(r) for r in store.assessed_on(*day_bounds(day), mark, column)]
    since = now_utc() - timedelta(hours=hours)
    return [dict(r) for r in store.assessed_since(since, mark, column)]


def window(args, day: date | None) -> str:
    return f"за {day.isoformat()}" if day else f"за {args.hours:g} ч"


def cmd_top(args) -> int:
    srcs = sources.by_id(load_sources())
    day = parse_day(args.date)
    with open_store() as store:
        events = digest.group_events(issue_rows(store, args.hours, None, day=day))
    if not events:
        print(f"{window(args, day)} оценённых событий нет")
        return 0
    for ev in events[: args.limit]:
        lead = ev.lead
        names = sorted({getattr(srcs.get(i["source"]), "name", i["source"]) for i in ev.items})
        print(f"{ev.importance:>2}/10 {ev.score:.2f}  {lead['title_ru']}")
        print(f"          {ev.key} · {lead['kind']} · {', '.join(names)}")
    return 0


def digest_title(day: date) -> str:
    return f"Дайджест разработчика, {day.day} {MONTHS[day.month - 1]}"


def cmd_digest(args) -> int:
    """Выпуск за день --date (по дате публикации) или, без него, за --hours сбора."""
    by_day = parse_day(args.date)
    day = by_day or date.today()
    root = library.library_root()
    path = library.day_dir(root, day) / DIGEST_SLUG
    digest_id = library.article_id(path)
    srcs = sources.by_id(load_sources())
    with open_store() as store:
        sections = digest.select_sections(
            digest.group_events(issue_rows(store, args.hours, digest_id, day=by_day)),
            srcs,
            top=args.top,
            recent=recent_vendors(store, srcs, day),
        )
        events = [ev for _, part in sections for ev in part]
        if not events:
            print(f"дайджеста нет: {window(args, by_day)} нет событий для выпуска")
            return 3
        if not (path / META_NAME).exists():
            entry = Entry(title=digest_title(day), added=day.isoformat(), kind="digest")
            path = library.create_entry(root, entry, DIGEST_SLUG)
        text = digest.render_events(events, srcs, sections=sections)
        (path / EVENTS).write_text(text, encoding="utf-8")
        store.mark_issue([i["id"] for ev in events for i in ev.items], digest_id)
    library.write_index(root)
    from narrator.news.cover import PROMPT

    if not (path / PROMPT).exists():  # заполненный агентом промпт не трогаем
        (path / PROMPT).write_text(cover_prompt(day), encoding="utf-8")
    print(f"обложка: {designs.design_for(day).name} — заготовка {PROMPT}")
    print(f"событий {len(events)}:")
    for name, part in sections:
        print(f"  {name}: " + "; ".join(ev.lead["title_ru"] for ev in part))
    print(path)
    return 0


def cover_prompt(day: date) -> str:
    """Заготовка image_prompt.md с оформлением дня."""
    return designs.render_prompt(day, f"{day.day} {MONTHS[day.month - 1]}")


def cmd_cover(args) -> int:
    """Оформление обложки дня; --write кладёт заготовку заново в каталог дайджеста."""
    from narrator.news.cover import PROMPT

    day = parse_day(args.date) or date.today()
    text = cover_prompt(day)
    if not args.write:
        print(f"# {day.isoformat()}: {designs.design_for(day).name}\n")
        print(text)
        return 0
    d = library.day_dir(library.library_root(), day) / DIGEST_SLUG
    if not (d / META_NAME).exists():
        return fail(f"{d}: дайджеста нет — сначала news digest --date {day.isoformat()}")
    (d / PROMPT).write_text(text, encoding="utf-8")
    print(f"обложка: {designs.design_for(day).name} — {d / PROMPT}")
    return 0


def weekly_title(day: date, days: int) -> str:
    start = day - timedelta(days=days - 1)
    if start.month == day.month:
        span = f"{start.day}–{day.day} {MONTHS[day.month - 1]}"
    else:
        span = f"{start.day} {MONTHS[start.month - 1]} – {day.day} {MONTHS[day.month - 1]}"
    return f"Главное в разработке за неделю, {span}"


WEEK_END = 4  # неделя обзора — суббота…пятница: видео снимают в выходные


def last_full_week_end(today: date) -> date:
    """Последняя пятница строго до today: конец последней завершённой недели.

    В субботу и воскресенье — вчерашняя или позавчерашняя пятница, в пятницу —
    прошлая: текущая неделя ещё не кончилась.
    """
    return today - timedelta(days=(today.weekday() - WEEK_END) % 7 or 7)


def monthly_title(first: date) -> str:
    return f"Главное в разработке за {MONTHS_NOM[first.month - 1]} {first.year}"


def span_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    """Дни first…last включительно по местному времени."""
    return day_bounds(first)[0], day_bounds(last)[1]


def issue_dir(root: Path, day: date, name: str, entry: Entry) -> Path:
    """library/youtube_news/<год>/<месяц day>/<name>/ с meta.json; уже есть — как есть."""
    path = library.youtube_dir(root, day) / name
    if not (path / META_NAME).exists():
        path.mkdir(parents=True, exist_ok=True)
        entry.save(path / META_NAME)
    return path


def write_video_issue(
    path: Path, column: str, sections: list[tuple[str, list[digest.Event]]], title: str, head: str
) -> list[digest.Event]:
    """events.md обзора для YouTube и отметки выпуска у его материалов."""
    srcs = sources.by_id(load_sources())
    events = [ev for _, part in sections for ev in part]
    text = digest.render_events(
        events, srcs, title, head, sections=sections if len(sections) > 1 else None
    )
    (path / EVENTS).write_text(text, encoding="utf-8")
    with open_store() as store:
        keys = [i["id"] for ev in events for i in ev.items]
        store.mark_issue(keys, library.article_id(path), column)
    library.write_index(library.library_root())
    return events


def cmd_weekly(args) -> int:
    """Обзор недели для YouTube: все важные события за days дней по дате публикации.

    Без --date — последняя завершённая неделя суббота…пятница.
    """
    last = date.fromisoformat(args.date) if args.date else last_full_week_end(date.today())
    first = last - timedelta(days=args.days - 1)
    root = library.library_root()
    name = f"week_{first.isoformat()}_{last.isoformat()}"
    mark = library.article_id(library.youtube_dir(root, last) / name)
    with open_store() as store:
        rows = [dict(r) for r in store.assessed_on(*span_bounds(first, last), mark, "weekly")]
    events = digest.select_weekly(digest.group_events(rows))
    if not events:
        print(f"обзора нет: за {args.days} дн. нет событий важностью от {digest.NOTABLE}")
        return 3
    entry = Entry(title=weekly_title(last, args.days), added=last.isoformat(), kind="weekly")
    path = issue_dir(root, last, name, entry)
    sections = [("", events)]
    write_video_issue(path, "weekly", sections, "# События недели", digest.WEEKLY_HEAD)
    strong = sum(ev.importance >= digest.IMPORTANT for ev in events)
    print(f"событий {len(events)}, из них важных (от {digest.IMPORTANT}) {strong}:")
    for ev in events:
        print(f"  {ev.importance:>2}/10  {ev.lead['title_ru']}")
    print(path)
    return 0


def cmd_monthly(args) -> int:
    """Обзор месяца для YouTube: важное для разработчика и громкое в индустрии."""
    today = date.today()
    try:
        first = (
            datetime.strptime(args.month, "%Y-%m").date() if args.month else today.replace(day=1)
        )
    except ValueError:
        return fail(f"--month {args.month!r}: ждём ГГГГ-ММ")
    following = (first + timedelta(days=32)).replace(day=1)
    last = min(following - timedelta(days=1), today)
    root = library.library_root()
    name = f"month_{first:%Y-%m}"
    mark = library.article_id(library.youtube_dir(root, first) / name)
    with open_store() as store:
        rows = [dict(r) for r in store.assessed_on(*span_bounds(first, last), mark, "monthly")]
    sections = digest.select_monthly(digest.group_events(rows))
    if not sections:
        print(f"обзора нет: за {first:%Y-%m} нет событий для видео")
        return 3
    entry = Entry(title=monthly_title(first), added=last.isoformat(), kind="monthly")
    path = issue_dir(root, first, name, entry)
    write_video_issue(path, "monthly", sections, "# События месяца", digest.MONTHLY_HEAD)
    for title, part in sections:
        print(f"{title}: {len(part)}")
        for ev in part:
            print(f"  {ev.importance:>2}/10  {ev.lead['title_ru']}")
    print(path)
    return 0


def find_audio(d: Path, source: Path | None = None) -> Path:
    """mp3 дайджеста, сделанный человеком из narration.md: <день>-digest_gemini.mp3.

    source — файл откуда угодно, его копируют в каталог. Без source берётся
    самый свежий mp3 каталога (кроме старых озвучек espeech, silero, supertonic)
    и переименовывается. FileNotFoundError — mp3 нет.
    """
    from narrator.synth import ENGINES

    target = d / library.audio_name(d, AUDIO_ENGINE)
    if source is not None:
        if not source.is_file():
            raise FileNotFoundError(f"нет файла {source}")
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return target
    old = {library.audio_name(d, e) for e in ENGINES}
    found = [p for p in d.glob("*.mp3") if p.name not in old]
    if not found:
        raise FileNotFoundError(
            f"в {d} нет mp3: озвучь {NARRATION} и положи файл туда (имя любое, .mp3)"
            " или укажи его: --audio <файл>"
        )
    newest = max(found, key=lambda p: p.stat().st_mtime)
    if newest != target:
        newest.replace(target)
        print(f"аудио: {newest.name} → {target.name}")
    return target


def audio_seconds(path: Path) -> float:
    """Длительность mp3 через ffprobe; 0 — не узнать."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0"]
    try:
        out = subprocess.run(cmd + [str(path)], capture_output=True, text=True, check=True)
        return float(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0.0


def prepare_release(args) -> tuple[Path, Entry, Path, Path] | int:
    """Каталог, карточка, mp3 и картинка дайджеста — или код ошибки.

    Находит mp3 и картинку, что положил человек, и пишет длительность в meta.json.
    """
    day = parse_day(args.date) or date.today()
    root = library.library_root()
    d = resolve(args.dir) if args.dir else library.day_dir(root, day) / DIGEST_SLUG
    if not (d / META_NAME).exists():
        return fail(f"{d}: дайджеста нет — сначала make news")
    meta = Entry.load(d / META_NAME)
    if meta.kind != "digest":
        return fail(f"{d}: это не дайджест дня")
    if not (d / POST).exists():
        return fail(f"{d}: нет {POST} — агент не дописал дайджест")
    review = d / digest.REVIEW
    if not review.exists():
        print(f"внимание: судья выпуск не смотрел — make news-judge DAY={meta.added}")
    elif digest.review_verdict(review.read_text(encoding="utf-8")) == "не готово":
        if not args.force:
            return fail(f"судья: не готово — см. {review}. Отправить всё равно — --force")
        print(f"внимание: судья сказал «не готово» ({review}), отправляю по --force")
    try:
        audio = find_audio(d, Path(args.audio).expanduser() if args.audio else None)
    except FileNotFoundError as e:
        return fail(str(e), 2)
    from narrator.news.cover import CoverError, find_cover

    try:  # без картинки в канал ничего не уходит
        cover = find_cover(d, Path(args.image).expanduser() if args.image else None)
    except CoverError as e:
        return fail(str(e), 2)
    print(f"обложка: {cover}")
    seconds = audio_seconds(audio) or meta.versions.get(AUDIO_ENGINE) or 0.0
    meta.engine, meta.audio, meta.duration_sec = AUDIO_ENGINE, audio.name, seconds
    meta.versions[AUDIO_ENGINE] = seconds
    meta.save(d / META_NAME)
    library.write_index(library.library_root())
    print(f"аудио: {audio} ({library.format_duration(seconds)})")
    return d, meta, audio, cover


def post_format(value: str | None) -> str:
    """Вид поста: --as, иначе $NARRATOR_NEWS_POST, иначе видео."""
    chosen = (value or os.environ.get(POST_ENV) or "video").strip().lower()
    if chosen not in POST_FORMATS:
        raise SystemExit(fail(f"вид поста {chosen!r}: ждём {' или '.join(POST_FORMATS)}", 2))
    return chosen


def cmd_publish(args) -> int:
    """Дайджест в канал новостей: видео с подписью post.md одним постом (по умолчанию)
    или двумя — обложка с post.md, затем аудио.

    Отметка об отправке общая: выпуск, ушедший в одном виде, в другом без
    --force не уходит. --channel — другой канал вместо канала новостей, у него
    отметка своя.
    """
    from narrator import cli as app
    from narrator import telegram

    fmt = post_format(args.as_)
    try:  # имя канала проверяем до сборки видео
        if args.channel:
            telegram.resolve_channel(args.channel)
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    ready = prepare_release(args)
    if isinstance(ready, int):
        return ready
    d, meta, audio, cover = ready
    tag = telegram.sent_tag(meta, args.channel)
    video = covers = None
    if fmt == "video":
        from narrator.news.video import VideoError, make_frames, make_video

        try:
            # Видео могут переслать без подписи — пометки реестра и в кадре.
            notes = registry.notes(telegram.spoken_text(d, meta), registry.load())
            poster, thumb, frame = make_frames(cover, notes)
            out = d / audio.name.replace("_gemini.mp3", ".mp4")
            video = make_video(poster, frame, audio, out, meta.duration_sec)
        except VideoError as e:
            return fail(str(e), 5)
        covers = (poster, thumb)
        print(f"видео: {video} ({video.stat().st_size / 2**20:.1f} МБ), обложка: {poster}")
    if args.no_send:
        return 0
    try:
        bot, state, path = app.open_bot()
    except telegram.TelegramError as e:
        return fail(str(e), 4)
    code = 0
    try:
        if not args.force and not telegram.is_fresh(state, d, AUDIO_ENGINE, tag):
            print("дайджест уже в канале; mp3 не менялся. Повтор — --force")
        elif video is not None:
            done = telegram.send_digest_video(
                bot, state, d, meta, AUDIO_ENGINE, video, covers, args.channel
            )
            app.report_delivery(done)
        else:
            done = telegram.broadcast(bot, state, d, meta, AUDIO_ENGINE, channel=args.channel)
            app.report_delivery(done)
    except telegram.TelegramError as e:
        code = fail(str(e), 4)
    return app.save_state(state, path) or code


def cmd_video(args) -> int:
    """То же, что publish --as video."""
    args.as_ = "video"
    return cmd_publish(args)


def resolve(value: str) -> Path:
    from narrator.cli import resolve_dir

    return resolve_dir(value)


def cmd_check(args) -> int:
    from collections import Counter

    d = resolve(args.dir)
    meta = Entry.load(d / META_NAME)
    if meta.kind in ("weekly", "monthly"):
        return check_video(d, meta)
    problems = []
    if meta.kind != "digest":
        problems.append("это не выпуск новостей: kind в meta.json не digest, weekly, monthly")
    if not meta.summary.strip():
        problems.append("meta.json: пустой summary")
    from narrator.news.cover import PROMPT, check_image_prompt

    post = d / POST
    image_prompt = d / PROMPT
    if image_prompt.exists():
        problems += check_image_prompt(
            image_prompt.read_text(encoding="utf-8"),
            post.read_text(encoding="utf-8") if post.exists() else "",
            date.fromisoformat(meta.added[:10]) if meta.added else None,
        )
    else:
        problems.append(f"нет {PROMPT} — промпта для картинки в ChatGPT")
    if post.exists():
        problems += digest.check_post(post.read_text(encoding="utf-8"))
        problems += check_footnotes(post.read_text(encoding="utf-8"))
    else:
        problems.append(f"нет {POST}")
    narration = d / NARRATION
    if narration.exists():
        text = narration.read_text(encoding="utf-8")
        words = len(text.split())
        length = digest.minutes(text, digest.WORDS_PER_MIN_SPEAKER)
        low, high = digest.DIGEST_TARGET_MINUTES
        print(f"{NARRATION}: {words} слов, озвучка ~{length:.0f} мин (цель {low}–{high})")
        if words > digest.DIGEST_MAX_WORDS:
            problems.append(
                f"{NARRATION}: {words} слов — это дольше {digest.DIGEST_MAX_MINUTES} мин"
                f" (предел {digest.DIGEST_MAX_WORDS} слов), сократи подробности"
            )
        terms = digest.jargon(text)
        if words and len(terms) * 100 / words > digest.JARGON_PER_100:
            top = ", ".join(f"{t} ×{n}" for t, n in Counter(terms).most_common(12))
            problems.append(
                f"{NARRATION}: {len(terms)} аббревиатур и технических обозначений на"
                f" {words} слов — больше {digest.JARGON_PER_100:g} на сто, тяжело слушать."
                f" Перескажи общими словами: {top}"
            )
        if "](" in text or "`" in text:
            problems.append(f"{NARRATION}: ссылки и код не для слуха — они в {READING}")
    else:
        problems.append(f"нет {NARRATION}")
    reading = d / READING
    if not reading.exists() or not reading.read_text(encoding="utf-8").strip():
        problems.append(f"нет {READING} — версии дайджеста для чтения")
    review = d / digest.REVIEW
    if review.exists() and digest.review_verdict(review.read_text(encoding="utf-8")) is None:
        problems.append(
            f"{digest.REVIEW}: нет строки «Вердикт: …» — одно из: " + ", ".join(digest.VERDICTS)
        )
    for p in problems:
        print(f"ошибка: {p}")
    if post.exists() and not problems:
        size = digest.visible_length(digest.post_html(post.read_text(encoding="utf-8")))
        print(f"post.md: {size} из {digest.POST_LIMIT} знаков")
    print(
        f"готово: озвучь {NARRATION}, затем make news-publish"
        if not problems
        else "исправь и повтори"
    )
    return 1 if problems else 0


def check_footnotes(post: str) -> list[str]:
    """Упоминание из реестра запрещённых организаций без пометки в post.md — ошибка."""

    return [
        f"{POST}: упоминание без пометки реестра — допиши в конец строку «* {n}»"
        for n in registry.missing(post, post, registry.load())
    ]


def check_video(d: Path, meta: Entry) -> int:
    """Обзор недели или месяца: текст для суфлёра и пост, сколько идёт видео.

    Предела длины нет: хронометраж по словам, которые читают вслух.
    """
    problems = []
    if not meta.summary.strip():
        problems.append("meta.json: пустой summary")
    post = d / POST
    if post.exists():
        problems += digest.check_post(post.read_text(encoding="utf-8"))
        problems += check_footnotes(post.read_text(encoding="utf-8"))
    else:
        problems.append(f"нет {POST}")
    prompter = d / TELEPROMPTER
    if prompter.exists():
        text = prompter.read_text(encoding="utf-8")
        words = len(digest.spoken(text).split())
        length = digest.minutes(digest.spoken(text), digest.WORDS_PER_MIN_SPEAKER)
        print(f"{TELEPROMPTER}: {words} слов вслух, в кадре ~{length:.0f} мин")
        problems += [f"{TELEPROMPTER}: {p}" for p in digest.check_teleprompter(text)]
        materials = d / MATERIALS
        if materials.exists():
            listed = materials.read_text(encoding="utf-8")
            problems += [f"{MATERIALS}: {p}" for p in digest.check_materials(text, listed)]
        else:
            problems.append(f"нет {MATERIALS} — описания вставок и плана блоков")
    else:
        problems.append(f"нет {TELEPROMPTER} — текста, который читают в кадре")
    for p in problems:
        print(f"ошибка: {p}")
    print("обзор готов" if not problems else "исправь и повтори")
    return 1 if problems else 0


def cmd_prune(args) -> int:
    """Ретенция: материалы старше --days — в архив, база сжимается (CMP-016)."""
    with open_store() as store:
        n = store.prune(now_utc(), args.days)
    print(f"в архив старше {args.days:g} дней: {n}")
    return 0


def add_parser(sub) -> None:
    s = sub.add_parser("news", help="дайджест новостей: сбор, разбор, отбор, публикация")
    news = s.add_subparsers(dest="news_cmd", required=True)

    n = news.add_parser("collect", help="обойти источники и сохранить новые материалы (сеть)")
    n.add_argument("--only", action="append", help="только этот источник, можно повторять")
    n.add_argument(
        "--days", type=float, default=3, help="материал старше — сразу в архив (по умолч. 3)"
    )
    n.add_argument("--date", help="нужен выпуск за этот день: окно архива дотянется до него")
    n.set_defaults(func=cmd_collect)

    n = news.add_parser("prune", help="старые материалы — в архив, сжать базу (зовёт и collect)")
    n.add_argument(
        "--days", type=float, default=RETENTION_DAYS, help=f"старше — в архив ({RETENTION_DAYS})"
    )
    n.set_defaults(func=cmd_prune)

    n = news.add_parser("where", help="пути к выпуску дня: что читать и что озвучить")
    n.add_argument("--date", help="ГГГГ-ММ-ДД, today или yesterday; по умолчанию сегодня")
    n.set_defaults(func=cmd_where)

    n = news.add_parser("sources", help="источники: сбор, сбои, полезность")
    n.set_defaults(func=cmd_sources)

    n = news.add_parser("failed", help="источники со сбоем сбора — агенту на добор")
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_failed)

    n = news.add_parser("add", help="материалы упавших источников, добытые агентом (JSON)")
    n.add_argument("file")
    n.add_argument(
        "--days", type=float, default=3, help="материал старше — сразу в архив (по умолч. 3)"
    )
    n.add_argument("--date", help="нужен выпуск за этот день: окно архива дотянется до него")
    n.set_defaults(func=cmd_add)

    n = news.add_parser("pending", help="новые материалы для разбора агентом")
    n.add_argument("--limit", type=int, default=150)
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_pending)

    n = news.add_parser("assess", help="записать оценки агента из JSON-файла")
    n.add_argument("file")
    n.set_defaults(func=cmd_assess)

    n = news.add_parser("rescore", help="пересчитать важность всех оценок по текущей формуле")
    n.set_defaults(func=cmd_rescore)

    n = news.add_parser("top", help="события по важности за день или последние часы")
    n.add_argument("--hours", type=float, default=30)
    n.add_argument("--date", help="опубликованные в этот день: ГГГГ-ММ-ДД, today, yesterday")
    n.add_argument("--limit", type=int, default=15)
    n.set_defaults(func=cmd_top)

    n = news.add_parser("digest", help="каталог дайджеста дня с events.md")
    n.add_argument("--hours", type=float, default=30, help="без --date: окно сбора, ч")
    n.add_argument(
        "--top",
        type=int,
        help="важных событий не больше (обязательные — все); по умолчанию предела нет,"
        " выпуск держит длительность",
    )
    n.add_argument(
        "--date",
        help="новости, опубликованные в этот день: ГГГГ-ММ-ДД, today, yesterday;"
        " без него — собранные за --hours, выпуск сегодняшний",
    )
    n.set_defaults(func=cmd_digest)

    n = news.add_parser(
        "weekly", help="обзор недели для YouTube: library/youtube_news/week_…/events.md"
    )
    n.add_argument("--days", type=int, default=7, help="за сколько дней (по умолч. 7)")
    n.add_argument(
        "--date",
        help="последний день обзора, ГГГГ-ММ-ДД; по умолчанию последняя пятница"
        " (неделя суббота…пятница)",
    )
    n.set_defaults(func=cmd_weekly)

    n = news.add_parser(
        "monthly", help="обзор месяца для YouTube: library/youtube_news/month_…/events.md"
    )
    n.add_argument("--month", help="ГГГГ-ММ; по умолчанию текущий месяц по сегодня")
    n.set_defaults(func=cmd_monthly)

    for name, help_, func in (
        ("publish", "mp3 и картинка готовы — дайджест в канал новостей", cmd_publish),
        ("video", "то же, что publish --as video: mp4 одним постом", cmd_video),
    ):
        n = news.add_parser(name, help=help_)
        n.add_argument("dir", nargs="?", help="каталог дайджеста; по умолчанию — сегодняшний")
        n.add_argument("--date", help="день дайджеста: ГГГГ-ММ-ДД, today, yesterday")
        n.add_argument("--audio", help="mp3 откуда угодно — скопировать в каталог дайджеста")
        n.add_argument("--image", help="картинка откуда угодно — скопировать в каталог дайджеста")
        if name == "publish":
            n.add_argument(
                "--as",
                dest="as_",
                choices=POST_FORMATS,
                help="video — mp4 с подписью post.md одним постом; photo — обложка с"
                f" post.md, затем аудио. По умолчанию ${POST_ENV} или video",
            )
        n.add_argument(
            "--no-send", action="store_true", help="только найти файлы (и собрать mp4), не слать"
        )
        n.add_argument("--force", action="store_true", help="отправить повторно")
        n.add_argument(
            "--channel", help="в этот канал вместо канала новостей: имя из telegram_tems.md или id"
        )
        n.set_defaults(func=func, as_=None)

    n = news.add_parser("cover", help="оформление обложки дня: сезон месяца и дизайн дня")
    n.add_argument("--date", help="ГГГГ-ММ-ДД, today или yesterday; по умолчанию сегодня")
    n.add_argument("--write", action="store_true", help="записать заготовку image_prompt.md заново")
    n.set_defaults(func=cmd_cover)

    n = news.add_parser("check", help="проверить дайджест дня или обзор недели")
    n.add_argument("dir")
    n.set_defaults(func=cmd_check)
