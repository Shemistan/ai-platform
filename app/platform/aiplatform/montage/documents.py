"""Материалы проекта (MASTER_SPEC §5.3): конспекты и статьи → текст → фрагменты с адресом цитаты.

Форматы: .md и .txt — как есть; .docx — zip с word/document.xml (stdlib); .pdf — через `pdftotext`
(poppler), без него документ хранится со статусом `unavailable`. Ссылки как источники не
поддерживаются (сети нет, ADR 0011). Текст режется на фрагменты ~CHUNK_CHARS знаков по абзацам с
заголовком раздела и номером страницы (у PDF — по form feed) — это адрес цитаты в KnowledgeClaim.
Материалы — недоверенный ввод: их содержимое идёт в промпт как данные, не как инструкции.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import schemas

FILE = "documents.json"
DIR = "documents"
MAX_DOCS = 20
MAX_BYTES = 20 * 2**20
MAX_NAME = 120
CHUNK_CHARS = 800
MAX_CHUNKS_IN_PROMPT = 120  # ~60–90 тыс. знаков контекста
EXTS = ("md", "txt", "docx", "pdf")
PDFTOTEXT_TIMEOUT_S = 120
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"^#{1,6}\s+(.+)$")


class DocumentError(ValueError):
    pass


def sniff(name: str, data: bytes) -> str:
    """Расширение по сигнатуре и имени: pdf, docx, md, txt; иначе DocumentError."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        if b"word/document.xml" in data[:200_000] or b"word/" in data:
            return "docx"
        raise DocumentError("Архив не похож на DOCX.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError("Поддерживаются текст (.md, .txt в UTF-8), .docx и .pdf.") from exc
    return "md" if name.lower().endswith(".md") else "txt"


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    paragraphs = []
    for p in root.iter(f"{_W_NS}p"):
        style = p.find(f"{_W_NS}pPr/{_W_NS}pStyle")
        text = "".join(t.text or "" for t in p.iter(f"{_W_NS}t")).strip()
        if not text:
            continue
        name = (style.get(f"{_W_NS}val") or "") if style is not None else ""
        if name.lower().startswith("heading"):
            level = "".join(c for c in name if c.isdigit()) or "1"
            text = "#" * min(6, int(level)) + " " + text
        paragraphs.append(text)
    return "\n\n".join(paragraphs)


def pdf_text(path: Path) -> str:
    cmd = shutil.which("pdftotext")
    if cmd is None:
        raise DocumentError("PDF не читается: нет pdftotext (brew install poppler).")
    proc = subprocess.run([cmd, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, text=True,
                          timeout=PDFTOTEXT_TIMEOUT_S)  # fmt: skip
    if proc.returncode:
        raise DocumentError(f"pdftotext не справился: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def text_of(path: Path, ext: str) -> tuple[str, int | None]:
    """(текст, страниц или None). PDF без pdftotext → DocumentError."""
    if ext == "docx":
        return docx_text(path), None
    if ext == "pdf":
        text = pdf_text(path)
        return text, max(1, text.count("\f") + (0 if text.endswith("\f") else 1))
    return path.read_text(encoding="utf-8"), None


def chunk(text: str, doc_id: str, size: int = CHUNK_CHARS) -> list[dict]:
    """Фрагменты по абзацам: {doc, n, heading, page, text}; страница — по form feed PDF."""
    out: list[dict] = []
    heading = ""
    page: int | None = 1 if "\f" in text else None  # страницы — только у PDF (form feed между ними)
    buf: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal buf, length
        body = "\n".join(buf).strip()
        if body:
            out.append({"doc": doc_id, "n": len(out), "heading": heading, "page": page, "text": body})
        buf, length = [], 0

    for raw in text.split("\n"):
        for k, line in enumerate(raw.split("\f")):
            if k:
                flush()
                page = (page or 1) + 1
            line = line.rstrip()
            if found := _HEADING.match(line):
                flush()
                heading = found[1].strip()[:120]
                continue
            if not line:
                if length >= size:
                    flush()
                elif buf and buf[-1] != "":
                    buf.append("")
                continue
            if length + len(line) > size * 1.5 and buf:
                flush()
            buf.append(line)
            length += len(line) + 1
    flush()
    return out


def load(folder: Path) -> list[dict]:
    try:
        data = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("documents") or [] if isinstance(data, dict) else []


def _save_index(folder: Path, items: list[dict]) -> None:
    schemas.check("source_document", items, many=True)
    data = {"schema_version": schemas.SCHEMA_VERSION, "documents": items}
    atomic_write(folder / FILE, json.dumps(data, ensure_ascii=False, indent=2))


def add(folder: Path, name: str, data: bytes) -> dict:
    """Сохранить материал: файл, текст рядом (`<id>.txt`), запись в documents.json."""
    name = " ".join((name or "материал").split())[:MAX_NAME] or "материал"
    if not data:
        raise DocumentError("Пустой файл.")
    if len(data) > MAX_BYTES:
        raise DocumentError(f"Материал больше {MAX_BYTES // 2**20} МБ.")
    items = load(folder)
    if len(items) >= MAX_DOCS:
        raise DocumentError(f"Не больше {MAX_DOCS} материалов на проект.")
    ext = sniff(name, data)
    n = 1 + max((int(i["id"].split("_")[1]) for i in items), default=0)
    doc_id = f"doc_{n}"
    root = folder / DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{doc_id}.{ext}"
    path.write_bytes(data)
    record = {
        "id": doc_id, "name": name, "ext": ext, "sha256": hashlib.sha256(data).hexdigest(), "chars": 0,
        "pages": None, "status": "ready", "error": None, "chunks": 0, "added_at": round(time.time(), 3),
    }  # fmt: skip
    try:
        text, pages = text_of(path, ext)
    except DocumentError as exc:
        record.update(status="unavailable", error=str(exc))
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, subprocess.SubprocessError) as exc:
        record.update(status="failed", error=f"не прочитался: {type(exc).__name__}")
    else:
        text = text.strip()
        if not text:
            record.update(status="failed", error="в материале нет текста")
        else:
            (root / f"{doc_id}.txt").write_text(text, encoding="utf-8")
            pieces = chunk(text, doc_id)
            record.update(chars=len(text), pages=pages, chunks=len(pieces))
    items.append(record)
    _save_index(folder, items)
    return record


def remove(folder: Path, doc_id: str) -> bool:
    items = load(folder)
    keep = [i for i in items if i["id"] != doc_id]
    if len(keep) == len(items):
        return False
    for path in (folder / DIR).glob(f"{doc_id}.*"):
        path.unlink(missing_ok=True)
    _save_index(folder, keep)
    return True


def chunks_of(folder: Path) -> list[dict]:
    """Фрагменты всех готовых материалов (chunks.json пишет конвейер)."""
    out: list[dict] = []
    for item in load(folder):
        if item["status"] != "ready":
            continue
        path = folder / DIR / f"{item['id']}.txt"
        if path.is_file():
            out += chunk(path.read_text(encoding="utf-8"), item["id"])
    return out


def pack(chunks: list[dict], limit: int = MAX_CHUNKS_IN_PROMPT) -> str:
    """Фрагменты для промпта: `[doc_1#3] (раздел, с. 2) текст`; хвост за лимитом отбрасывается с пометкой."""
    lines = []
    for item in chunks[:limit]:
        where = ", ".join(x for x in (item.get("heading") or "", f"с. {item['page']}" if item.get("page") else "") if x)
        lines.append(f"[{item['doc']}#{item['n']}]" + (f" ({where})" if where else "") + f" {item['text']}")
    if len(chunks) > limit:
        lines.append(f"(ещё {len(chunks) - limit} фрагментов не поместилось)")
    return "\n\n".join(lines)
