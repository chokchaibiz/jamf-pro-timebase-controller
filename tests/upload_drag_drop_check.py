#!/usr/bin/env python3
"""Static regression checks for the browser file drag-and-drop integration."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
template = (ROOT / "portal" / "templates" / "index.html").read_text(encoding="utf-8")
script_path = ROOT / "portal" / "static" / "upload.js"
script = script_path.read_text(encoding="utf-8")
styles = (ROOT / "portal" / "static" / "app.css").read_text(encoding="utf-8")

assert template.count("data-file-drop") == 2
assert template.count("data-file-input") == 2
assert template.count("data-file-error") == 2
assert '<script src="/static/upload.js" defer></script>' in template

for required in (
    'addEventListener("dragenter"',
    'addEventListener("dragover"',
    'addEventListener("dragleave"',
    'addEventListener("drop"',
    "new DataTransfer()",
    "input.files = transfer.files",
    'endsWith(".csv")',
    'dispatchEvent(new Event("change"',
):
    assert required in script, f"missing drag/drop behavior: {required}"

for required in (".dropzone.is-dragover", ".dropzone.has-file", ".dropzone.has-error"):
    assert required in styles, f"missing dropzone state style: {required}"

node = shutil.which("node")
if node:
    subprocess.run([node, "--check", str(script_path)], check=True)

print("Upload drag/drop checks: attendance + holidays: PASS")
