"""Вкладка «Обновления»: API под /api/updates/.

Две проверки, обе в фоне (запустил → статус → результат):
- модели и пайплайны — сверка установленных версий с PyPI, GitHub и Hugging Face;
- реестр запрещённых организаций — LLM с веб-поиском сверяет его с перечнями
  Минюста и предлагает новую версию; применяет её человек, посмотрев разницу.
"""

from __future__ import annotations

import json
import shlex
import sys
import threading
import time
from collections.abc import Callable
from datetime import date
from http import HTTPStatus
from pathlib import Path

from aiplatform.config import Config, pick
from aiplatform.core.http import HttpError, Request, bad_request, not_found
from aiplatform.photo import config as photo_config
from aiplatform.studio.llm import StudioError, complete, extract_json
from aiplatform.updates import checks, registry
from aiplatform.video import config as video_config

PREFIX = "updates"
FAKES = Path(__file__).resolve().parent.parent / "fakes"
# Сверке нужен свежий веб: без поиска модель ответит по памяти.
DEFAULT_LLM_CMD = "claude -p --allowedTools WebSearch,WebFetch"
IDLE, RUNNING, DONE, FAILED = "idle", "running", "done", "failed"
MAX_BODY = 512 * 1024
TASK = "### task: registry"


def registry_prompt(entries: list[registry.Entry], today: str) -> str:
    current = json.dumps({"entries": [e.public() for e in entries]}, ensure_ascii=False, indent=2)
    statuses = ", ".join(f"{k} — {v}" for k, v in registry.STATUSES.items())
    return f"""{TASK}
Сегодня {today}. Ты — выпускающий редактор IT-канала в России. Сверь список
организаций ниже с официальными перечнями: экстремистских организаций
Минюста (minjust.gov.ru/ru/documents/7822/), нежелательных организаций
(minjust.gov.ru/ru/documents/7756/) и террористических организаций (ФСБ,
Росфинмониторинг). Найди решения судов и Генпрокуратуры, вышедшие после
даты последней сверки. Ищи в интернете — по памяти не отвечай.

В список входят только те, кто может попасть в новости или видео об IT:
технологические компании и их продукты, соцсети и мессенджеры, хакерские
группировки, IT-фонды и их основатели. Остальных не добавляй.

Верни ПОЛНЫЙ новый список: все записи, которые остаются в силе, плюс новые.
Убирай запись, только если её официально исключили из перечня. Поля записи:
- id — латиница, цифры и дефис, у старых записей — прежний;
- name — как организацию называют в новостях;
- status — {statuses};
- aliases — как её упоминают в тексте: латиницей и кириллицей, названия
  продуктов; `*` в конце слова — любое окончание («Фейсбук*»);
- disclaimer — стандартная пометка без точки в конце, как принято в СМИ.
  Пример: «Компания Meta (продукты Facebook и Instagram) признана
  экстремистской организацией и запрещена на территории РФ»;
- basis — номер в перечне и решение (суд, дата);
- source — ссылка на перечень или решение.

В notes одной-двумя фразами напиши, что изменилось и что проверить руками.
Ответь только JSON такого вида:
```json
{{"entries": [{{"id": "…", "name": "…", "status": "extremist", "aliases": ["…"],
  "disclaimer": "…", "basis": "…", "source": "…"}}], "notes": "…"}}
```

Текущий список:
```json
{current}
```
"""


class Job:
    """Фоновая проверка с состоянием на диске: переживает перезапуск сервиса."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.state = {"status": IDLE}
        if self.state.get("status") == RUNNING:
            self.state |= {"status": FAILED, "error": "Проверку прервал перезапуск сервиса — запусти снова."}

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def update(self, **values) -> None:
        with self.lock:
            self.state |= values
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def start(self, work: Callable[[], None], **initial) -> None:
        with self.lock:
            if self.state.get("status") == RUNNING:
                raise HttpError(HTTPStatus.CONFLICT, "Проверка уже идёт — дождись её.")
            self.state = {"status": RUNNING, "started": time.time(), **initial}

        def body() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001 — любая ошибка видна на вкладке, а не в логе сервера
                self.update(status=FAILED, error=str(exc) or type(exc).__name__, finished=time.time())

        self.thread = threading.Thread(target=body, daemon=True)
        self.thread.start()

    def join(self, timeout: float) -> None:
        if self.thread:
            self.thread.join(timeout)


class UpdatesModule:
    prefix = PREFIX
    label = "Обновления"

    def __init__(self, ctx) -> None:
        self.config: Config = ctx.config
        self.registry = registry.Registry(ctx.config.data_dir / registry.FILE_NAME)
        self.cancel = threading.Event()
        folder = ctx.config.data_dir / "updates"
        self.models_job = Job(folder / "models.json")
        self.registry_job = Job(folder / "registry-check.json")
        if ctx.config.fake:
            from aiplatform.fakes import fake_updates

            self.llm_cmd: tuple[str, ...] = (sys.executable, str(FAKES / "fake_llm.py"))
            self.fetch: checks.Fetch = fake_updates.fetch
            self.demo: dict[str, str] | None = fake_updates.INSTALLED
        else:
            self.llm_cmd = tuple(shlex.split(pick(ctx.config.env, "AIP_UPDATES_LLM_CMD", default=DEFAULT_LLM_CMD)))
            self.fetch, self.demo = checks.http_fetch, None

    # --- модели ---

    def components(self) -> list[checks.Component]:
        return checks.components(
            photo_config.load(self.config),
            video_config.load(self.config),
            demo=self.demo,
        )

    def check_models(self) -> None:
        todo = self.components()

        def work() -> None:
            results = []
            for n, component in enumerate(todo, start=1):
                if self.cancel.is_set():
                    return
                self.models_job.update(progress=f"{component.label} ({n} из {len(todo)})")
                results.append(checks.check(component, self.fetch).public())
                self.models_job.update(results=results)
            updates = sum(r["status"] == checks.UPDATE for r in results)
            self.models_job.update(status=DONE, finished=time.time(), progress="", updates=updates)

        self.models_job.start(work, results=[], progress="Готовлю список…")

    # --- реестр ---

    def registry_view(self) -> dict:
        entries, checked = self.registry.read()
        return {
            "entries": [e.public() for e in entries],
            "checked": checked,
            "customized": self.registry.customized(),
            "statuses": registry.STATUSES,
            "check": self.registry_job.snapshot(),
        }

    def check_registry(self) -> None:
        current = self.registry.entries()
        prompt = registry_prompt(current, date.today().isoformat())

        def work() -> None:
            lines: list[str] = []

            def on_line(line: str) -> None:
                if line.strip():
                    lines.append(line)
                    self.registry_job.update(progress=f"LLM пишет… ({len(lines)} строк)")

            answer = complete(prompt, cmd=self.llm_cmd, cancel=self.cancel, on_line=on_line)
            data = extract_json(answer)
            try:
                proposal = registry.parse(data)
            except ValueError as exc:
                raise StudioError(f"LLM вернула список не по формату: {exc}") from exc
            if not proposal:
                raise StudioError("LLM вернула пустой список — не применяю.")
            notes = str(data.get("notes") or "").strip() if isinstance(data, dict) else ""
            change = registry.diff(current, proposal)
            if not any(change.values()):
                self.registry.save(current)  # сверка прошла — запоминаем дату
                proposal_view = None
            else:
                proposal_view = [e.public() for e in proposal]
            self.registry_job.update(
                status=DONE, finished=time.time(), progress="", proposal=proposal_view, diff=change, notes=notes
            )

        self.registry_job.start(work, progress="LLM ищет решения судов…")

    def apply_proposal(self) -> None:
        state = self.registry_job.snapshot()
        if state.get("status") != DONE or not state.get("proposal"):
            raise bad_request("Нет предложенного списка — запусти проверку.")
        self.registry.save(registry.parse(state["proposal"]))
        self.registry_job.update(proposal=None, applied=time.time())

    def dismiss_proposal(self) -> None:
        self.registry_job.update(proposal=None)

    def save_registry(self, payload: object) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise bad_request("Нужно поле text с JSON списка.")
        try:
            entries = registry.parse(json.loads(payload["text"]))
        except (json.JSONDecodeError, ValueError) as exc:
            raise bad_request(f"Список не годится: {exc}") from exc
        if not entries:
            raise bad_request("Список пуст — пометки на видео пропадут. Удали записи по одной, если это нужно.")
        self.registry.save(entries, self.registry.read()[1])

    # --- платформа ---

    def describe(self) -> dict:
        return {"models": self.models_job.snapshot(), "registry": self.registry_view()}

    def problems(self) -> list[str]:
        return []

    def handle(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["state"] | ["config"]):
                return req.send_json(self.describe())
            case ("POST", ["models", "check"]):
                self.check_models()
            case ("POST", ["registry", "check"]):
                self.check_registry()
            case ("POST", ["registry", "apply"]):
                self.apply_proposal()
            case ("POST", ["registry", "dismiss"]):
                self.dismiss_proposal()
            case ("POST", ["registry", "reset"]):
                self.registry.reset()
            case ("POST", ["registry"]):
                self.save_registry(req.json(MAX_BODY))
            case _:
                raise not_found()
        return req.send_json(self.describe())

    def close(self) -> None:
        self.cancel.set()
        self.models_job.join(5)
        self.registry_job.join(5)
