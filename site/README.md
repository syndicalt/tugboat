# Tugboat Site

This directory contains the static marketing site for Tugboat.

Open `site/index.html` directly in a browser, or serve it with:

```bash
python -m http.server 8000 --directory site
```

Rendered documentation pages under `site/docs/` are generated from the Markdown
docs in the repository:

```bash
python site/render_docs.py
```

The site has no JavaScript framework or package-manager dependency.
