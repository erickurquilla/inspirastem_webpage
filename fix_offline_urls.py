#!/usr/bin/env python3
"""Rewrite absolute inspirastem.com URLs in an offline HTTrack/WordPress mirror.

Safely updates HTML attributes (including srcset / lazy-load variants), CSS
url(...), inline styles, Elementor lightbox hashes, and JS/JSON config URLs.
Only rewrites a URL when the corresponding local file (or directory) exists.

Usage:
  python3 fix_offline_urls.py [/path/to/inspirastem.com]
  python3 fix_offline_urls.py --dry-run [/path/to/inspirastem.com]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urlparse

SITE_NETLOCS = {"inspirastem.com", "www.inspirastem.com"}

URL_ATTRS = {
    "src",
    "href",
    "poster",
    "content",
    "data-src",
    "data-lazy-src",
    "data-bg",
    "data-background",
    "data-background-image",
    "data-large_image",
    "data-thumb",
    "cite",
    "action",
    "formaction",
}

SRCSET_ATTRS = {
    "srcset",
    "data-srcset",
    "data-lazy-srcset",
    "imagesrcset",
}

TEXT_EXTENSIONS = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".json",
    ".svg",
    ".xml",
    ".txt",
    ".php",
    ".map",
}

SKIP_DIR_NAMES = {"hts-cache", ".git", "backups", "__pycache__"}

# Match absolute site URLs (plain).
ABS_URL_RE = re.compile(
    r"https?://(?:www\.)?inspirastem\.com(?:/[^\s\"'<>)\\]*)?",
    re.IGNORECASE,
)

# Match JS/JSON-escaped absolute site URLs: https:\/\/inspirastem.com\/...
# Only path segments (not JSON \" escapes) so we never overrun string boundaries.
ABS_ESCAPED_URL_RE = re.compile(
    r"https?:\\/\\/(?:www\.)?inspirastem\.com"
    r"(?:\\\/[A-Za-z0-9._~%-]+)*"
    r"(?:\\\/)?",
    re.IGNORECASE,
)

CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.IGNORECASE | re.DOTALL)

E_ACTION_HASH_RE = re.compile(
    r'(data-e-action-hash\s*=\s*)(["\'])([^"\']+)\2',
    re.IGNORECASE,
)

ATTR_RE = re.compile(
    r'(\s)('
    + "|".join(
        sorted(
            {re.escape(a) for a in (URL_ATTRS | SRCSET_ATTRS | {"style", "data-e-action-hash", "data-settings"})},
            key=len,
            reverse=True,
        )
    )
    + r')(\s*=\s*)(["\'])(.*?)\4',
    re.IGNORECASE | re.DOTALL,
)


class Stats:
    def __init__(self) -> None:
        self.files_scanned = 0
        self.files_modified = 0
        self.urls_rewritten = 0
        self.srcset_entries_removed = 0
        self.srcset_attrs_removed = 0
        self.missing_local: dict[str, set[str]] = {}
        self.modified_paths: list[str] = []

    def note_missing(self, file_path: Path, ref: str) -> None:
        self.missing_local.setdefault(str(file_path), set()).add(ref)


def normalize_for_parse(url: str) -> str:
    """Return a parseable URL, unescaping JS backslash-slashes."""
    u = url.strip()
    if "\\/" in u:
        u = u.replace("\\/", "/")
    if u.startswith("//"):
        u = "https:" + u
    return u


def is_site_url(url: str) -> bool:
    if not url:
        return False
    u = normalize_for_parse(url)
    try:
        host = (urlparse(u).netloc or "").lower()
    except Exception:
        return False
    return host in SITE_NETLOCS


def site_path_from_url(url: str) -> str | None:
    """Absolute site URL -> path relative to site root (no leading slash)."""
    if not is_site_url(url):
        return None
    u = normalize_for_parse(url)
    parsed = urlparse(u)
    path = unquote(parsed.path or "/")
    return path.lstrip("/")


def resolve_local(root: Path, site_path: str, *, prefer_dir: bool = False) -> Path | None:
    """Find a local file/dir for a site-root path."""
    if not site_path:
        candidate = root / "index.html"
        return candidate if candidate.is_file() else root

    clean = site_path.split("#", 1)[0]
    no_query = clean.split("?", 1)[0]
    trials = [no_query]
    if clean != no_query:
        trials.append(clean.replace("?", ""))

    root_resolved = root.resolve()
    for trial in trials:
        if not trial:
            continue
        p = (root / trial).resolve()
        try:
            p.relative_to(root_resolved)
        except ValueError:
            continue
        if p.is_file():
            return p
        if p.is_dir():
            if prefer_dir:
                return p
            for index_name in ("index.html", "index.htm"):
                idx = p / index_name
                if idx.is_file():
                    return idx
            return p  # directory asset roots (uploads/, assets/)
        # Try as page slug -> directory/index.html
        if "." not in Path(trial).name:
            for suffix in ("/index.html", ".html"):
                alt = (root / (trial.rstrip("/") + suffix)).resolve()
                try:
                    alt.relative_to(root_resolved)
                except ValueError:
                    continue
                if alt.is_file():
                    return alt
    return None


def to_relative(from_file: Path, target: Path) -> str:
    rel = os.path.relpath(target.resolve(), from_file.parent.resolve())
    return rel.replace(os.sep, "/")


def rewrite_url(
    url: str,
    root: Path,
    from_file: Path,
    stats: Stats,
) -> str:
    """Rewrite one absolute site URL if a local copy exists; else leave it."""
    if not url or url.startswith(("#", "data:", "mailto:", "tel:", "javascript:", "blob:")):
        return url
    if not is_site_url(url):
        return url

    was_escaped = "\\/" in url
    site_path = site_path_from_url(url)
    if site_path is None:
        return url

    prefer_dir = url.rstrip().endswith("/") or site_path in {
        "wp-content/uploads",
        "wp-content/plugins/elementor/assets",
        "wp-content/plugins/elementor/assets/",
    } or site_path.endswith("/")
    # uploadUrl / assets often omit trailing slash in JSON
    if site_path.rstrip("/") in {
        "wp-content/uploads",
        "wp-content/plugins/elementor/assets",
        "wp-includes",
        "wp-content",
    }:
        prefer_dir = True

    local = resolve_local(root, site_path, prefer_dir=prefer_dir)
    if local is None:
        stats.note_missing(from_file, url)
        return url

    new = to_relative(from_file, local)
    # Directories in JSON configs should keep a trailing slash feel when original had one
    if local.is_dir() and url.rstrip().endswith(("/", "\\/")):
        if not new.endswith("/"):
            new += "/"
    if was_escaped:
        new = new.replace("/", "\\/")
    stats.urls_rewritten += 1
    return new


def rewrite_srcset(value: str, root: Path, from_file: Path, stats: Stats) -> str | None:
    """Return rewritten srcset, or None to drop the attribute."""
    if not value.strip():
        return None

    kept: list[str] = []
    for part in re.split(r"\s*,\s*", value.strip()):
        if not part:
            continue
        tokens = part.split()
        url = tokens[0]
        descriptors = tokens[1:]

        if not is_site_url(url):
            # Keep third-party entries; also keep already-local entries if file exists.
            if url.startswith(("http://", "https://", "//")):
                kept.append(part.strip())
                continue
            # Relative entry: keep only if present
            clean = url.split("?", 1)[0].split("#", 1)[0]
            target = (from_file.parent / clean).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                stats.srcset_entries_removed += 1
                continue
            if target.is_file():
                kept.append(part.strip())
            else:
                stats.srcset_entries_removed += 1
                stats.note_missing(from_file, url)
            continue

        site_path = site_path_from_url(url) or ""
        local = resolve_local(root, site_path)
        if local is None or not local.is_file():
            stats.srcset_entries_removed += 1
            stats.note_missing(from_file, url)
            continue
        new_url = to_relative(from_file, local)
        stats.urls_rewritten += 1
        kept.append(new_url + ((" " + " ".join(descriptors)) if descriptors else ""))

    if not kept:
        stats.srcset_attrs_removed += 1
        return None
    return ", ".join(kept)


def rewrite_css_urls(text: str, root: Path, from_file: Path, stats: Stats) -> str:
    def repl(match: re.Match[str]) -> str:
        q = match.group(1) or ""
        url = match.group(2).strip()
        if not url or url.startswith(("data:", "#")):
            return match.group(0)
        if is_site_url(url):
            return f"url({q}{rewrite_url(url, root, from_file, stats)}{q})"
        return match.group(0)

    return CSS_URL_RE.sub(repl, text)


def rewrite_elementor_hash(value: str, root: Path, from_file: Path, stats: Stats) -> str:
    if "inspirastem.com" not in value.lower() and "inspirastem" not in unquote(value).lower():
        # Still try decode — URL may be only inside base64
        pass
    try:
        full = unquote(value)
        if "settings=" not in full:
            return value
        prefix, b64 = full.split("settings=", 1)
        # Trim anything after base64
        b64 = b64.split("&", 1)[0]
        pad = "=" * (-len(b64) % 4)
        data = json.loads(base64.b64decode(b64 + pad).decode("utf-8"))
    except Exception:
        return value

    changed = False
    for key in ("url", "image", "src"):
        if isinstance(data.get(key), str) and is_site_url(data[key]):
            new = rewrite_url(data[key], root, from_file, stats)
            if new != data[key]:
                data[key] = new
                changed = True
    if not changed:
        return value

    new_b64 = base64.b64encode(
        json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    new_full = prefix + "settings=" + new_b64
    if value.startswith("#"):
        body = new_full[1:] if new_full.startswith("#") else new_full
        return "#" + quote(body, safe="")
    return quote(new_full, safe="")


def rewrite_text_urls(text: str, root: Path, from_file: Path, stats: Stats) -> str:
    """Rewrite absolute site URLs in free text / JS / JSON."""

    def repl_escaped(m: re.Match[str]) -> str:
        return rewrite_url(m.group(0), root, from_file, stats)

    def repl_plain(m: re.Match[str]) -> str:
        return rewrite_url(m.group(0), root, from_file, stats)

    text = ABS_ESCAPED_URL_RE.sub(repl_escaped, text)
    text = ABS_URL_RE.sub(repl_plain, text)
    return text


def rewrite_html(content: str, root: Path, from_file: Path, stats: Stats) -> str:
    def attr_repl(m: re.Match[str]) -> str:
        pre, name, eq, quote_ch, value = (
            m.group(1),
            m.group(2),
            m.group(3),
            m.group(4),
            m.group(5),
        )
        lname = name.lower()

        if lname in SRCSET_ATTRS:
            new_val = rewrite_srcset(value, root, from_file, stats)
            if new_val is None:
                return ""  # remove attribute entirely
            return f"{pre}{name}{eq}{quote_ch}{new_val}{quote_ch}"

        if lname == "style":
            new_val = rewrite_css_urls(value, root, from_file, stats)
            return f"{pre}{name}{eq}{quote_ch}{new_val}{quote_ch}"

        if lname == "data-e-action-hash":
            new_val = rewrite_elementor_hash(value, root, from_file, stats)
            return f"{pre}{name}{eq}{quote_ch}{new_val}{quote_ch}"

        if lname in URL_ATTRS or lname in {"data-settings"}:
            if is_site_url(value) or "inspirastem.com" in value.lower():
                if is_site_url(value):
                    new_val = rewrite_url(value, root, from_file, stats)
                else:
                    new_val = rewrite_text_urls(value, root, from_file, stats)
                return f"{pre}{name}{eq}{quote_ch}{new_val}{quote_ch}"

        return m.group(0)

    # Attribute-level updates first (preserves surrounding markup).
    updated = ATTR_RE.sub(attr_repl, content)

    # Catch Elementor hashes even if attribute regex missed unusual quoting.
    def hash_repl(m: re.Match[str]) -> str:
        new_val = rewrite_elementor_hash(m.group(3), root, from_file, stats)
        return f"{m.group(1)}{m.group(2)}{new_val}{m.group(2)}"

    updated = E_ACTION_HASH_RE.sub(hash_repl, updated)

    # Inline <style> blocks
    def style_block_repl(m: re.Match[str]) -> str:
        return m.group(1) + rewrite_css_urls(m.group(2), root, from_file, stats) + m.group(3)

    updated = re.sub(
        r"(<style\b[^>]*>)(.*?)(</style>)",
        style_block_repl,
        updated,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Inline <script> blocks (JS config objects)
    def script_block_repl(m: re.Match[str]) -> str:
        body = m.group(2)
        if "inspirastem.com" not in body.lower() and r"inspirastem.com" not in body:
            return m.group(0)
        return m.group(1) + rewrite_text_urls(body, root, from_file, stats) + m.group(3)

    updated = re.sub(
        r"(<script\b[^>]*>)(.*?)(</script>)",
        script_block_repl,
        updated,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return updated


def should_scan(path: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return False
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    # Extensionless oEmbed payloads etc.
    if "wp-json" in path.parts and path.name.startswith("embed"):
        return True
    return False


def process_file(path: Path, root: Path, stats: Stats, dry_run: bool) -> None:
    stats.files_scanned += 1
    try:
        original = path.read_text(encoding="utf-8", errors="surrogateescape")
    except Exception as exc:
        print(f"  skip (read error): {path}: {exc}", file=sys.stderr)
        return

    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        updated = rewrite_html(original, root, path, stats)
    elif suffix == ".css":
        updated = rewrite_css_urls(original, root, path, stats)
        updated = rewrite_text_urls(updated, root, path, stats)
    else:
        updated = rewrite_text_urls(original, root, path, stats)

    if updated != original:
        stats.files_modified += 1
        stats.modified_paths.append(str(path.relative_to(root)))
        if not dry_run:
            path.write_text(updated, encoding="utf-8", errors="surrogateescape")


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            if should_scan(path):
                yield path


def scan_missing_local_refs(root: Path, stats: Stats) -> None:
    """Report local asset references in HTML that do not exist on disk."""
    asset_suffixes = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".mp4",
        ".webm",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".css",
        ".js",
    )
    attr_re = re.compile(
        r"""(?:src|href|poster|data-src|data-lazy-src)\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )
    root_resolved = root.resolve()
    for path in root.rglob("*.html"):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for ref in attr_re.findall(text):
            if ref.startswith(("http://", "https://", "//", "data:", "mailto:", "tel:", "#", "javascript:")):
                continue
            clean = ref.split("?", 1)[0].split("#", 1)[0]
            if not clean:
                continue
            if not clean.lower().endswith(asset_suffixes) and "/wp-content/" not in clean and "/wp-includes/" not in clean:
                continue
            target = (path.parent / clean).resolve()
            try:
                target.relative_to(root_resolved)
            except ValueError:
                continue
            if not target.exists():
                stats.note_missing(path, ref)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mirror",
        nargs="?",
        default=str(Path(__file__).resolve().parent / "inspirastem.com"),
        help="Path to the offline mirror root (default: ./inspirastem.com)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not write")
    args = parser.parse_args()

    root = Path(args.mirror).resolve()
    if not root.is_dir():
        print(f"Error: mirror directory not found: {root}", file=sys.stderr)
        return 1

    # Sanity-check escaped regex against a known sample.
    sample = "https:\\/\\/inspirastem.com\\/wp-content\\/uploads"
    if not ABS_ESCAPED_URL_RE.search(sample):
        print("Error: escaped URL regex failed self-check", file=sys.stderr)
        print(" pattern:", ABS_ESCAPED_URL_RE.pattern, file=sys.stderr)
        return 1

    stats = Stats()
    print(f"Processing mirror: {root}")
    if args.dry_run:
        print("Mode: dry-run (no writes)")

    for path in sorted(iter_files(root)):
        process_file(path, root, stats, dry_run=args.dry_run)

    scan_missing_local_refs(root, stats)

    print("\n===== SUMMARY =====")
    print(f"Files scanned:                 {stats.files_scanned}")
    print(f"Files modified:                {stats.files_modified}")
    print(f"URLs rewritten:                {stats.urls_rewritten}")
    print(f"Broken srcset entries removed: {stats.srcset_entries_removed}")
    print(f"Empty srcset attrs removed:    {stats.srcset_attrs_removed}")
    missing_count = sum(len(v) for v in stats.missing_local.values())
    print(f"Missing local asset refs:      {missing_count}")

    if stats.modified_paths:
        print("\nModified files:")
        for p in stats.modified_paths:
            print(f"  {p}")

    if stats.missing_local:
        print("\nMissing local assets (sample):")
        shown = 0
        for fpath, refs in sorted(stats.missing_local.items()):
            for ref in sorted(refs):
                rel = Path(fpath)
                try:
                    rel = rel.relative_to(root)
                except Exception:
                    pass
                print(f"  {rel} -> {ref}")
                shown += 1
                if shown >= 80:
                    break
            if shown >= 80:
                break
        if missing_count > shown:
            print(f"  ... and {missing_count - shown} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
