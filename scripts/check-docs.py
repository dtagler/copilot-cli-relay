"""Offline documentation links, examples, and release metadata checks (stdlib)."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent


def anchors(text: str) -> set[str]:
    found = set(re.findall(r'<a\s+id="([^"]+)"', text))
    used: dict[str, int] = {}
    for title in re.findall(r"^#{1,6}\s+(.+)$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
        count = used.get(slug, 0)
        used[slug] = count + 1
        found.add(slug if not count else f"{slug}-{count}")
    return found


def main() -> None:
    files = [
        ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "GLOSSARY.md",
        *sorted((ROOT / "docs").rglob("*.md")), *sorted((ROOT / "skill").rglob("*.md")),
    ]
    links = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        for raw in re.findall(r"\[[^\]\n]*\]\(([^)\s]+)\)", text):
            url = urlsplit(raw)
            if url.scheme or url.netloc:
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            assert target.is_relative_to(ROOT) and target.exists(), f"Broken local link in {path.name}: {raw}"
            if url.fragment and target.suffix == ".md":
                assert unquote(url.fragment) in anchors(target.read_text(encoding="utf-8")), f"Broken anchor: {raw}"
            links += 1
        for kind, snippet in re.findall(r"^```(toml|json)\n(.*?)^```", text, re.MULTILINE | re.DOTALL):
            (tomllib.loads if kind == "toml" else json.loads)(snippet)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == project["name"])
    source = (ROOT / "src/copilot_cli_relay/__init__.py").read_text()
    assert package["version"] == project["version"]
    assert f'__version__ = "{project["version"]}"' in source
    print(f"Documentation examples, {links} local links, and package/lock versions passed.")


if __name__ == "__main__":
    main()
