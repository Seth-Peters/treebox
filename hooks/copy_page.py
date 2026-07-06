"""MkDocs hook: embed each page's raw Markdown source so the docs can offer a
"Copy page" button that hands an agent-ready copy of the page to the clipboard.

The whole site ships self-contained (no external requests), so rather than
convert rendered HTML back to Markdown in the browser, we carry the *original*
`.md` source into the page at build time — base64-encoded inside a hidden
``<script type="text/markdown">`` element. ``javascripts/treebox.js`` decodes
it and wires up the button. Base64 keeps the payload in the safe [A-Za-z0-9+/=]
alphabet, so it can never break out of the script tag or trip UTF-8 escaping.
"""

from __future__ import annotations

import base64
from html import escape
from typing import Any


def on_page_content(html: str, *, page: Any, config: Any, files: Any) -> str:
    """Append the raw-Markdown carrier to each rendered page's content."""
    markdown: str | None = getattr(page, "markdown", None)
    if not markdown:
        return html

    payload = base64.b64encode(markdown.encode("utf-8")).decode("ascii")
    title = escape(page.title or "", quote=True)
    url = escape(page.canonical_url or "", quote=True)
    carrier = (
        '<script type="text/markdown" class="tx-page-md" '
        f'data-title="{title}" data-url="{url}">{payload}</script>'
    )
    return html + carrier
