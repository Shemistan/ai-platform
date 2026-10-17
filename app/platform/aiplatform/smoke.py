"""Короткий прогон на настоящих моделях: одна картинка, одна фраза озвучки, черновик статьи до видео.

    make smoke                 # настоящие модели и LLM этой машины (local.mk подхватывается)
    python -m aiplatform.smoke --fake   # то же на фейках — так его гоняют тесты

Платформа поднимается в этом процессе на временном каталоге данных
(настоящие данные не трогаются), запросы идут через её HTTP API, как со
страниц. В конце — таблица: шаг, время, итог. Код выхода 0 — всё прошло.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from aiplatform import config as config_module
from aiplatform import server as server_module

ARTICLE = """Проверка платформы

Это короткая статья для проверки: платформа рисует постер, делит текст на слайды и озвучивает его.

Если вы видите это видео, значит модели, озвучка и сборка работают на этой машине."""
STEP_TIMEOUT_S = 3600


class Smoke:
    def __init__(self, base: str, out=sys.stdout) -> None:
        self.base = base
        self.out = out
        self.rows: list[tuple[str, float, bool, str]] = []

    def call(self, method: str, path: str, body: object = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        headers = {"X-AIP": "1"}  # без него и без Origin сервер POST отклонит
        if os.environ.get("AIP_AUTH_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['AIP_AUTH_TOKEN']}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def wait(self, path: str, done: Callable[[dict], bool]) -> dict:
        deadline = time.time() + STEP_TIMEOUT_S
        while time.time() < deadline:
            _, body = self.call("GET", path)
            if done(body):
                return body
            time.sleep(0.5)
        raise TimeoutError(f"не дождались {path}")

    def step(self, name: str, fn: Callable[[], str]) -> bool:
        print(f"→ {name}…", file=self.out, flush=True)
        started = time.time()
        try:
            result, ok = fn(), True
        except Exception as exc:  # noqa: BLE001 — в отчёт, а не трассировкой
            result, ok = str(exc), False
        self.rows.append((name, time.time() - started, ok, result))
        return ok

    def job(self, tab: str, payload: dict) -> str:
        status, job = self.call("POST", f"/api/{tab}/jobs", payload)
        if status != 201:
            raise RuntimeError(job.get("error") or f"HTTP {status}")
        final = self.wait(f"/api/{tab}/jobs/{job['id']}", lambda j: j["status"] not in ("queued", "running"))
        if final["status"] != "done":
            raise RuntimeError(final.get("error") or final["status"])
        return f"{final.get('model_label') or final.get('engine_label') or 'готово'}"

    def image(self) -> str:
        _, cfg = self.call("GET", "/api/studio/config")
        model = cfg["defaults"]["image_model"]
        return self.job("photo", {"mode": "txt2img", "prompt": "a lighthouse at dawn, watercolor", "model": model})

    def voice(self) -> str:
        return self.job("audio", {"text": "Проверка озвучки платформы."})

    def article(self) -> str:
        payload = {"scenario": "article", "text": ARTICLE, "settings": {"draft": True}, "run": True}
        status, project = self.call("POST", "/api/studio/projects", payload)
        if status != 201:
            raise RuntimeError(project.get("error") or f"HTTP {status}")
        done = self.wait(f"/api/studio/projects/{project['id']}", lambda p: not p["running"])
        steps = {s["id"]: s for s in done["steps"]}
        if steps["render"]["status"] != "done":
            failed = next((s for s in done["steps"] if s["status"] == "failed"), steps["render"])
            raise RuntimeError(f"{failed['label']}: {failed.get('error') or failed['status']}")
        return f"видео {steps['render'].get('media_s', 0):.0f} с, слайдов {len(done['slides'])}"

    def report(self) -> int:
        print("\nШаг                          Время     Итог", file=self.out)
        for name, seconds, ok, result in self.rows:
            mark = "✓" if ok else "✗"
            print(f"{mark} {name:<26} {seconds:>6.0f} с  {result}", file=self.out)
        return 0 if all(ok for _, _, ok, _ in self.rows) else 1


def main(argv: list[str] | None = None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="фейковые модели и LLM — проверить сам прогон")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="aip-smoke-") as tmp:
        env = dict(config_module.load().env) | {"AIP_DATA_DIR": tmp, "AIP_PORT": "0", "AIP_SCHEDULE_TICK_S": "0"}
        if args.fake:
            env["AIP_BACKEND"] = "fake"
        server, app = server_module.make_server(config_module.load(env))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        smoke = Smoke(f"http://{host}:{port}", out)
        print(f"smoke: {'фейки' if args.fake else 'настоящие модели'}, данные — {tmp}", file=out)
        for problem in app.problems():
            print(f"⚠ {problem}", file=out)
        try:
            smoke.step("Картинка («Фото»)", smoke.image)
            smoke.step("Озвучка фразы («Аудио»)", smoke.voice)
            smoke.step("Черновик статьи до видео", smoke.article)
        finally:
            server.shutdown()
            server.server_close()
            app.close()
        return smoke.report()


if __name__ == "__main__":
    sys.exit(main())
