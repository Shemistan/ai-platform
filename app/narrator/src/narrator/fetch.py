"""Скачивание статьи и извлечение основного текста (trafilatura), дословно."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Article:
    url: str
    title: str | None
    author: str | None
    published: str | None
    text: str

    def to_markdown(self) -> str:
        head = [f"# {self.title or 'Без заголовка'}", "", f"- ссылка: {self.url}"]
        if self.author:
            head.append(f"- автор: {self.author}")
        if self.published:
            head.append(f"- дата: {self.published}")
        return "\n".join(head) + "\n\n" + self.text.strip() + "\n"


class FetchError(RuntimeError):
    pass


def extract(html: str, url: str) -> Article:
    import trafilatura

    doc = trafilatura.bare_extraction(
        html,
        url=url,
        output_format="python",
        include_comments=False,
        include_tables=True,
        include_formatting=True,
        include_links=False,
        with_metadata=True,
    )
    if doc is None or not (doc.text or "").strip():
        raise FetchError(f"{url}: не удалось выделить текст статьи")
    return Article(
        url=url,
        title=doc.title,
        author=doc.author,
        published=doc.date,
        text=doc.text,
    )


def fetch(url: str) -> Article:
    import trafilatura

    html = trafilatura.fetch_url(url)
    if not html:
        raise FetchError(
            f"{url}: страница не скачалась (нет сети, домен закрыт sandbox'ом "
            "или сайт отдаёт страницу только браузеру)"
        )
    return extract(html, url)
