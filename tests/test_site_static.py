from __future__ import annotations

import importlib.util
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
SPEC = importlib.util.spec_from_file_location("tugboat_site_render_docs", SITE / "render_docs.py")
assert SPEC is not None
render_docs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_docs)
DOCS = render_docs.DOCS
slug_for = render_docs.slug_for


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        if values.get("id"):
            self.ids.add(values["id"] or "")


def _parse(path: Path) -> LinkParser:
    parser = LinkParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def test_site_entrypoint_and_generated_docs_exist() -> None:
    assert (SITE / "index.html").exists()
    assert (SITE / "styles.css").exists()
    assert (SITE / "render_docs.py").exists()
    assert (SITE / "docs" / "index.html").exists()
    assert (SITE / "docs" / "docs.css").exists()
    for source, _title, _description in DOCS:
        assert (SITE / "docs" / f"{slug_for(source)}.html").exists()


def test_site_landing_page_contains_required_sections() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    required = [
        "Mission",
        "Workflow",
        "Capabilities",
        "Documentation",
        "proposal-only default",
        "local-first sidecar",
        "CODEX.md",
        "SKILL.md",
        "llmff",
        "Read the quickstart",
    ]
    for text in required:
        assert text in html


def test_site_internal_links_resolve() -> None:
    html_files = [SITE / "index.html", *sorted((SITE / "docs").glob("*.html"))]
    for path in html_files:
        parser = _parse(path)
        for href in parser.links:
            parsed = urlparse(href)
            if parsed.scheme or href.startswith("mailto:"):
                continue
            target_path = path.parent / unquote(parsed.path or path.name)
            target = target_path.resolve()
            assert target.exists(), f"{path.relative_to(ROOT)} links to missing {href}"
            assert SITE.resolve() in (target, *target.parents)


def test_rendered_docs_index_links_every_generated_doc() -> None:
    index = (SITE / "docs" / "index.html").read_text(encoding="utf-8")
    for source, title, _description in DOCS:
        assert f'{slug_for(source)}.html' in index
        assert title in index
