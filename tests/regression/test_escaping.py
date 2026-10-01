"""User-entered text containing HTML is shown as text everywhere, never run as script."""

from __future__ import annotations

from tests.regression.routes import routes
from tests.regression.world import XSS, fill, seed_world


async def test_script_tags_never_render_raw(app, make_client):
    lead = await make_client()
    ids = await seed_world(lead, hostile=True)
    leaks = []
    for path in routes(app, "GET"):
        if "{token}" in path or path.endswith("/stream"):
            continue
        url = fill(path, ids)
        if url is None:
            continue
        r = await lead.get(url, headers={"Accept": "text/html"})
        if XSS in r.text:
            leaks.append(path)
    assert not leaks, f"unescaped user text on: {leaks}"
    # and the text is still visible, escaped
    page = await lead.get(f"/archive/items/{ids['item_id']}")
    assert "&lt;script&gt;" in page.text
