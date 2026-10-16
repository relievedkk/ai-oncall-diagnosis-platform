"""Critical rendering dependencies must work without a third-party CDN."""

import base64
import hashlib
import re
from pathlib import Path


def test_rendering_dependencies_are_local_and_integrity_checked():
    static = Path(__file__).resolve().parents[1] / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([^"]+)"\s+integrity="([^"]+)"', html)
    assert len(scripts) >= 3
    for src, integrity in scripts:
        assert src.startswith("/static/vendor/"), src
        asset = static / src.removeprefix("/static/")
        assert asset.is_file()
        actual = "sha384-" + base64.b64encode(hashlib.sha384(asset.read_bytes()).digest()).decode()
        assert actual == integrity
    highlighter = (static / "vendor/highlight@11.9.0/highlight.min.js").read_text(encoding="utf-8")
    assert "var hljs=" in highlighter
