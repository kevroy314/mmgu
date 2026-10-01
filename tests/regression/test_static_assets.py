"""Every stylesheet and script the pages reference is actually served."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mmgu.core.hall import hall

STATIC = Path(__file__).resolve().parents[2] / "src" / "mmgu" / "static"


async def test_page_assets_resolve(make_client):
    from tests.regression.world import enable_all_modules

    lead = await make_client()
    await enable_all_modules(lead)
    page = (await lead.get("/")).text
    assets = re.findall(r'(?:href|src)="(/static/[^"?]+)', page)
    assert any(a.endswith("hall.css") for a in assets) and any(a.endswith("app.js") for a in assets)
    for m in hall.enabled_modules():
        for css in m.stylesheets:
            assert css in page, f"{m.id} stylesheet not linked from the base page"
    for a in set(assets):
        r = await lead.get(a)
        assert r.status_code == 200, f"{a} → {r.status_code}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses():
    for js in STATIC.glob("js/*.js"):
        if js.name.endswith(".min.js"):
            continue
        res = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        assert res.returncode == 0, f"{js.name}: {res.stderr}"
