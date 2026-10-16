"""Gemini TTS: образец голоса для клонирования локальными моделями.

Единственное место озвучки, где нужны сеть и ключ: `./narrate voice charon`
один раз просит у Gemini TTS фразу голосом Charon и кладёт её в папку
моделей. Дальше ESpeech и модели mlx-audio клонируют голос с этого образца
без сети. Ключ — $GEMINI_API_KEY (или $GOOGLE_API_KEY), модель —
$NARRATOR_GEMINI_TTS_MODEL.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request

import numpy as np

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"
# Gemini отдаёт PCM 16 бит моно; частота — в mimeType, обычно 24 кГц.
DEFAULT_RATE = 24000
TIMEOUT = 120


class GeminiError(RuntimeError):
    pass


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None


def model_name() -> str:
    return os.environ.get("NARRATOR_GEMINI_TTS_MODEL") or DEFAULT_MODEL


def _post(url: str, body: dict, key: str) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except (ValueError, OSError, KeyError, TypeError):
            detail = ""
        raise GeminiError(f"Gemini TTS: HTTP {e.code} {detail}".strip()) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GeminiError(f"Gemini TTS: {e}") from e


def speak(text: str, voice: str, key: str, post=_post) -> tuple[np.ndarray, int]:
    """Фраза голосом Gemini TTS: отсчёты float32 и частота."""
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    answer = post(API.format(model=model_name()), body, key)
    try:
        parts = answer["candidates"][0]["content"]["parts"]
        inline = next(p["inlineData"] for p in parts if "inlineData" in p)
        pcm = base64.b64decode(inline["data"])
    except (KeyError, IndexError, TypeError, StopIteration, ValueError) as e:
        raise GeminiError(f"Gemini TTS: в ответе нет аудио ({str(answer)[:200]})") from e
    rate = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
    return samples, int(rate[1]) if rate else DEFAULT_RATE
