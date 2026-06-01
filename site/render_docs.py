from __future__ import annotations

import html
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
DOCS_OUT = SITE / "docs"

DOCS = [
    ("README.md", "Overview", "Product overview and install path."),
    ("docs/quickstart.md", "Quickstart", "Run Tugboat in proposal-only mode."),
    ("docs/cli-reference.md", "CLI Reference", "Commands, options, and exit behavior."),
    ("docs/architecture.md", "Architecture", "Boundaries, services, and data flow."),
    ("docs/apply-rollback.md", "Apply and Rollback", "Reviewed mutation and recovery workflows."),
    ("docs/auto-apply.md", "Auto-Apply", "Policy gates, shadow mode, and incident handling."),
    ("docs/mcp-guide.md", "MCP Guide", "Read-only agent integration surface."),
    ("docs/llmff-compatibility.md", "llmff Compatibility", "Pipeline runner contract matrix."),
    ("docs/integrations.md", "Integrations", "Trace sources and tool-specific setup."),
    (
        "docs/instruction-best-practices.md",
        "Instruction Best Practices",
        "Patterns for maintainable agent instructions.",
    ),
    ("docs/ops/quick-adoption-proposal-only.md", "Proposal-Only Adoption", "Team adoption path without credentials."),
    ("docs/ops/operating-runbook.md", "Operating Runbook", "Operational checks and routine maintenance."),
    ("docs/ops/release-checklist.md", "Release Checklist", "Evidence required for a release."),
    ("docs/daemon-guide.md", "Daemon Guide", "Local daemon operation and controls."),
    ("docs/threat-model.md", "Threat Model", "Security boundaries and mitigations."),
    ("docs/troubleshooting.md", "Troubleshooting", "Common failures and recovery guidance."),
    ("docs/migration-v1.md", "Migration to v1", "Upgrade path from 0.x sidecars."),
    ("docs/releases/1.0.0-draft.md", "Release Notes", "Tugboat 1.0.0 release notes."),
    (
        "docs/announcements/tugboat-production-release-article.md",
        "Release Article",
        "Comprehensive product announcement.",
    ),
    ("docs/roadmaps/v1.0.0-roadmap.md", "v1 Roadmap", "Roadmap and release criteria."),
]


def slug_for(source: str) -> str:
    return Path(source).stem


def strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :].lstrip()
    return text


def render_inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _render_link, escaped)
    return escaped


def _render_link(match: re.Match[str]) -> str:
    label = match.group(1)
    href = match.group(2)
    if href.endswith(".md") or ".md#" in href:
        base, _, fragment = href.partition("#")
        href = f"{Path(base).stem}.html"
        if fragment:
            href = f"{href}#{fragment}"
    return f'<a href="{html.escape(href, quote=True)}">{label}</a>'


def render_markdown(text: str) -> str:
    lines = strip_front_matter(text).splitlines()
    output: list[str] = []
    paragraph: list[str] = []
    in_code = False
    code_lines: list[str] = []
    list_open = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            joined = " ".join(line.strip() for line in paragraph)
            output.append(f"<p>{render_inline(joined)}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            output.append("</ul>")
            list_open = False

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                output.append(
                    "<pre><code>"
                    + html.escape("\n".join(code_lines))
                    + "</code></pre>"
                )
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                close_list()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not line.strip():
            flush_paragraph()
            close_list()
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            anchor = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            output.append(
                f'<h{level} id="{html.escape(anchor, quote=True)}">{render_inline(title)}</h{level}>'
            )
            continue

        if line.startswith("- "):
            flush_paragraph()
            if not list_open:
                output.append("<ul>")
                list_open = True
            output.append(f"<li>{render_inline(line[2:].strip())}</li>")
            continue

        if re.match(r"^\d+\. ", line):
            flush_paragraph()
            if not list_open:
                output.append("<ul>")
                list_open = True
            output.append(f"<li>{render_inline(re.sub(r'^\d+\. ', '', line).strip())}</li>")
            continue

        if line.startswith("|"):
            flush_paragraph()
            close_list()
            cells = [render_inline(cell.strip()) for cell in line.strip("|").split("|")]
            if all(set(cell.replace(":", "").replace("-", "").strip()) == set() for cell in cells):
                continue
            output.append(
                "<div class=\"table-row\">"
                + "".join(f"<span>{cell}</span>" for cell in cells)
                + "</div>"
            )
            continue

        paragraph.append(line)

    flush_paragraph()
    close_list()
    if in_code:
        output.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    return "\n".join(output)


def page_template(title: str, description: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)} — Tugboat Docs</title>
    <meta name="description" content="{html.escape(description, quote=True)}" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
    <link rel="stylesheet" href="../styles.css" />
    <link rel="stylesheet" href="./docs.css" />
  </head>
  <body>
    <header class="nav" aria-label="Documentation navigation">
      <a class="brand" href="../index.html"><span class="brand-mark" aria-hidden="true">T</span><span>Tugboat</span></a>
      <nav class="nav-links">
        <a href="../index.html#workflow">Workflow</a>
        <a href="../index.html#features">Features</a>
        <a href="./index.html">Docs</a>
        <a href="https://github.com/syndicalt/tugboat">GitHub</a>
      </nav>
    </header>
    <main class="doc-shell">
      <aside class="doc-sidebar">
        <p class="kicker">Docs</p>
        <nav>{nav_links()}</nav>
      </aside>
      <article class="doc-content">
        {body}
      </article>
    </main>
  </body>
</html>
"""


def nav_links() -> str:
    links = []
    for source, title, _description in DOCS:
        href = f"{slug_for(source)}.html"
        links.append(f'<a href="{href}">{html.escape(title)}</a>')
    return "\n".join(links)


def docs_index() -> str:
    cards = []
    for source, title, description in DOCS:
        cards.append(
            f'<a href="{slug_for(source)}.html"><strong>{html.escape(title)}</strong>'
            f"<span>{html.escape(description)}</span></a>"
        )
    return page_template(
        "Documentation",
        "Rendered Tugboat documentation index.",
        '<h1 id="documentation">Tugboat Documentation</h1>'
        "<p>Rendered HTML versions of the canonical Markdown documentation.</p>"
        f'<div class="doc-card-grid">{"".join(cards)}</div>',
    )


def main() -> None:
    DOCS_OUT.mkdir(parents=True, exist_ok=True)
    for source, title, description in DOCS:
        markdown_path = ROOT / source
        body = render_markdown(markdown_path.read_text(encoding="utf-8"))
        (DOCS_OUT / f"{slug_for(source)}.html").write_text(
            page_template(title, description, body),
            encoding="utf-8",
        )
    (DOCS_OUT / "index.html").write_text(docs_index(), encoding="utf-8")


if __name__ == "__main__":
    main()
