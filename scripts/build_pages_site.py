#!/usr/bin/env python3
"""Assemble the GitHub Pages site and refuse anything that is not the intended, static content.

    python scripts/build_pages_site.py --console-dist apps/console/dist --out site
    python scripts/build_pages_site.py --verify-only --out site

Layout produced (served under /PexisOS/):

    site/index.html, brain.js, brain-texture.webp, brain-mask.webp   the PETRON Brain, visual only
    site/console/...                                                 the Smart Fan Console (Vite build)
    site/.nojekyll

The public Brain is a fixed allowlist of four files. Nothing else from ``apps/petron`` is copied: not
``live.js``, not the bridge, commands, runtime, tests or documentation. ``verify`` then checks the result
instead of trusting the copy: the file set is exact, the pages carry no operational capability, every
relative reference resolves, and a Content-Security-Policy that forbids network access is present.
Any failure stops the build (non-zero exit); nothing is weakened to get a deploy.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRAIN_SOURCE = REPO / "apps" / "petron" / "brain"
BRAIN_FILES = ("index.html", "brain.js", "brain-texture.webp", "brain-mask.webp")   # the whole public Brain
ROOT_ENTRIES = frozenset(BRAIN_FILES) | {"console", ".nojekyll"}

# A second, independent barrier behind the code checks: the browser itself refuses any request but
# same-origin images and scripts. The inline <style> of the page is the only inline allowance.
BRAIN_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
             "connect-src 'none'; base-uri 'none'; form-action 'none'")
# The public page has no runtime to drive the adaptive colors (they follow mission state and would sit at calm
# silver), so it declares the full-color palette as its default. ?palette=adaptive|mono still overrides it.
PUBLIC_PALETTE = "spectrum"
# An empty inline icon: the browser asks for /favicon.ico on its own, which would be a 404 on Pages.
BRAIN_ICON = '<link rel="icon" href="data:,">'
_HEAD_MARKER = '<meta charset="utf-8">'

# Capabilities that must never reach the public Brain, in the page or in its script.
_FORBIDDEN_BRAIN = {
    "live.js": re.compile(r"live\.js"),
    "bridge input element": re.compile(r"petron-input"),
    "live mode marker": re.compile(r"petron-mode"),
    "event stream": re.compile(r"EventSource"),
    "network call": re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|importScripts)\b"),
    "api path": re.compile(r"/api/"),
    "request header or key": re.compile(r"X-Petron-Key|scope_sha256|brain\.draft", re.I),
    "dynamic import": re.compile(r"\bimport\s*\("),
}
_REFERENCE = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#?]+)|url\(\s*['"]?([^'")#?]+)""", re.I)
_SCRIPT_TAG = re.compile(r"<script\b", re.I)
_ABSOLUTE_ASSET = re.compile(r"""(?:src|href)\s*=\s*["']/(?!/)""", re.I)


class SiteError(Exception):
    """The site is not what it is meant to be; the message names the first violated rule."""


def _clean(out: Path, inputs: tuple[Path, ...]) -> None:
    """Empty ``out`` for a fresh build. ``out`` and ``inputs`` are already resolved; nothing is deleted before
    every rule holds: never the checkout or a parent of it, never a directory that overlaps an input, and never
    a non-empty directory that is not the output of an earlier build (exactly the entries of a built site)."""
    if out == REPO or out in REPO.parents:
        raise SiteError(f"refusing to clean {out}: it is the repository or a parent of it")
    for source in inputs:
        if out == source or out in source.parents or source in out.parents:
            raise SiteError(f"refusing to clean {out}: it overlaps the build input {source}")
    if out.exists():
        if not out.is_dir() or (any(out.iterdir()) and {path.name for path in out.iterdir()} != ROOT_ENTRIES):
            raise SiteError(f"refusing to clean {out}: it is not the output of an earlier build "
                            f"(expected exactly {sorted(ROOT_ENTRIES)}); remove it by hand if it is yours")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def _with_csp(html: str) -> str:
    if html.count(_HEAD_MARKER) != 1 or "Content-Security-Policy" in html:
        raise SiteError("brain index.html: expected exactly one charset marker and no existing CSP")
    meta = f'<meta http-equiv="Content-Security-Policy" content="{BRAIN_CSP}">'
    palette = f'<meta name="petron-palette" content="{PUBLIC_PALETTE}">'
    return html.replace(_HEAD_MARKER, f"{_HEAD_MARKER}\n    {meta}\n    {palette}\n    {BRAIN_ICON}", 1)


def build(console_dist: Path, out: Path, brain_source: Path = BRAIN_SOURCE) -> None:
    console_dist, out, brain_source = (Path(path).resolve() for path in (console_dist, out, brain_source))
    if not (console_dist / "index.html").is_file():
        raise SiteError(f"console build not found at {console_dist} (run `npm run build` in apps/console)")
    for name in BRAIN_FILES:
        if not (brain_source / name).is_file():
            raise SiteError(f"approved Brain file missing: {brain_source / name}")
    _clean(out, (console_dist, brain_source))
    for name in BRAIN_FILES:
        if name == "index.html":
            (out / name).write_text(_with_csp((brain_source / name).read_text(encoding="utf-8")), encoding="utf-8")
        else:
            shutil.copyfile(brain_source / name, out / name)
    shutil.copytree(console_dist, out / "console")
    (out / ".nojekyll").write_text("")
    verify(out)


def _references(html: str) -> list[str]:
    """Every src/href/url() target in the page except inline ``data:`` URIs (which carry no request)."""
    found = []
    for match in _REFERENCE.finditer(html):
        target = match.group(1) or match.group(2)
        if target and not re.match(r"^data:", target, re.I):
            found.append(target)
    return found


def _check_references(root: Path, page: Path, html: str, label: str) -> None:
    for target in _references(html):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", target, re.I):
            raise SiteError(f"{label}: external reference {target!r}; the site may load only its own files")
        if target.startswith("/"):
            raise SiteError(f"{label}: absolute path {target!r} breaks under the /PexisOS/ subpath")
        resolved = (page.parent / target).resolve()
        if not resolved.is_relative_to(root):
            raise SiteError(f"{label}: reference {target!r} points outside the site")
        if not resolved.is_file():
            raise SiteError(f"{label}: reference {target!r} does not resolve to a file in the site")


def verify(out: Path) -> None:
    out = Path(out).resolve()
    if not out.is_dir():
        raise SiteError(f"site directory not found: {out}")
    entries = {path.name for path in out.iterdir()}
    if entries != ROOT_ENTRIES:
        raise SiteError(f"site root must hold exactly {sorted(ROOT_ENTRIES)}; "
                        f"extra {sorted(entries - ROOT_ENTRIES)}, missing {sorted(ROOT_ENTRIES - entries)}")
    # --- the Brain: static and visual only
    page = (out / "index.html").read_text(encoding="utf-8")
    script = (out / "brain.js").read_text(encoding="utf-8")
    if BRAIN_CSP not in page or BRAIN_ICON not in page:
        raise SiteError("brain index.html: the network-denying Content-Security-Policy or the inline icon is missing")
    if f'<meta name="petron-palette" content="{PUBLIC_PALETTE}">' not in page:
        raise SiteError("brain index.html: the public default palette is not declared")
    for label, pattern in _FORBIDDEN_BRAIN.items():
        for name, text in (("index.html", page), ("brain.js", script)):
            if pattern.search(text):
                raise SiteError(f"public Brain {name} carries an operational capability: {label}")
    if len(_SCRIPT_TAG.findall(page)) != 1 or page.count('<script src="./brain.js" defer></script>') != 1:
        raise SiteError("brain index.html: exactly one script element is allowed, and it must be ./brain.js")
    _check_references(out, out / "index.html", page, "brain index.html")
    # --- the Console: a built single-page app at /console/
    console = out / "console"
    if not (console / "index.html").is_file() or not (console / "assets").is_dir():
        raise SiteError("console: index.html and assets/ must be present under console/")
    console_page = (console / "index.html").read_text(encoding="utf-8")
    if _ABSOLUTE_ASSET.search(console_page):
        raise SiteError("console index.html: absolute asset path is incompatible with the /PexisOS/ subpath")
    _check_references(out, console / "index.html", console_page, "console index.html")
    # --- nothing operational anywhere in the published tree
    for path in out.rglob("*"):
        if path.is_file() and (path.suffix in (".py", ".db", ".sqlite3", ".pyc", ".env", ".pem", ".key")
                               or path.name in ("live.js", "brain_bridge.py", "brain_commands.py")):
            raise SiteError(f"forbidden file in the published tree: {path.relative_to(out)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--console-dist", type=Path, default=REPO / "apps" / "console" / "dist")
    parser.add_argument("--out", type=Path, default=REPO / "site")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            verify(args.out)
        else:
            build(args.console_dist, args.out)
    except SiteError as error:
        print(f"pages_site_refused: {error}", file=sys.stderr)
        return 1
    print(f"pages_site_ok: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
