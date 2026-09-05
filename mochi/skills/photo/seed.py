"""Download character refs and harvest real-world Wikimedia scenes."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from mochi.skills.photo.queries import (
    count_scene_refs,
    insert_photo_ref,
    source_url_exists,
)

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
PHOTO_REFS_DIR = DATA_DIR / "photo_refs"

_USER_AGENT = (
    "MochiBot/1.0 (photo-ref seeder; +https://github.com/KrvyFT/mochibot) "
    "python-httpx"
)
_PIXIV_REFERER = "https://www.pixiv.net/"
_WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
_SCENE_PER_REGION = 100
_MIN_EDGE = 800
_SKIP_NAME_RE = re.compile(
    r"map|diagram|logo|icon|flag|svg|chart|plan\b",
    re.IGNORECASE,
)
_LH3_RE = re.compile(r"https://lh3\.googleusercontent\.com/[^\s\"'\\>]+")
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_REV_RE = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)
_FILE_LINK_RE = re.compile(
    r'id=["\']file["\'][^>]*>\s*<a[^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

CHARACTER_URLS = ()
# Former Pixiv / Google Photos self albums removed; Elma refs are seeded from
# local operator-provided files under data/photo_refs (kind=self).
GOOGLE_PHOTOS_ALBUM = ""


JAPAN_CATEGORIES = (
    ("Category:Shinto shrines in Japan", "神社"),
    ("Category:Streets in Tokyo", "街头"),
    ("Category:Streets in Kyoto", "街头"),
    ("Category:Cafés in Japan", "咖啡店"),
    ("Category:Parks in Tokyo", "公园"),
    ("Category:Convenience stores in Japan", "便利店"),
    ("Category:Train stations in Tokyo", "车站"),
    ("Category:Train interiors in Japan", "车内"),
    ("Category:Night in Tokyo", "夜景"),
    ("Category:Rain in Tokyo", "雨天"),
    ("Category:Bedrooms in Japan", "卧室"),
    ("Category:Bookstores in Japan", "书店"),
    ("Category:Beaches of Japan", "海边"),
)
CHINA_CATEGORIES = (
    ("Category:Streets in Beijing", "街头"),
    ("Category:Streets in Shanghai", "街头"),
    ("Category:Hutongs of Beijing", "胡同"),
    ("Category:Parks in Beijing", "公园"),
    ("Category:West Lake", "西湖"),
    ("Category:Temples in China", "寺庙"),
    ("Category:Cafés in China", "咖啡店"),
    ("Category:Night in Shanghai", "夜景"),
    ("Category:Train stations in China", "车站"),
    ("Category:Convenience stores in China", "便利店"),
    ("Category:Classical gardens of Suzhou", "园林"),
    ("Category:Universities and colleges in China", "校园"),
    ("Category:Libraries in China", "图书馆"),
    ("Category:Night markets in China", "夜市"),
)

_seed_lock = threading.Lock()
_seed_started = False


def seed_allowed() -> bool:
    return (
        "PYTEST_CURRENT_TEST" not in os.environ
        and os.environ.get("MOCHI_DISABLE_PHOTO_SEED") != "1"
    )


def start_seed_thread() -> None:
    """Kick off background seeding once. Safe to call from init_schema."""
    global _seed_started
    if not seed_allowed():
        return
    with _seed_lock:
        if _seed_started:
            return
        _seed_started = True
    thread = threading.Thread(target=run_seed, name="photo-ref-seed", daemon=True)
    thread.start()


def run_seed() -> None:
    PHOTO_REFS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _seed_character_urls()
    except Exception:
        log.exception("Character photo-ref seed failed")
    try:
        _seed_google_photos()
    except Exception:
        log.exception("Google Photos photo-ref seed failed")
    try:
        _seed_wikimedia_scenes()
    except Exception:
        log.exception("Wikimedia photo-ref seed failed")


def parse_moegirl_file_page(html: str, page_url: str = "") -> str:
    """Extract the original image URL from a Moegirl File: page."""
    for pattern in (_OG_IMAGE_RE, _OG_IMAGE_REV_RE, _FILE_LINK_RE):
        match = pattern.search(html or "")
        if match:
            href = match.group(1).strip()
            if href.startswith("//"):
                href = "https:" + href
            elif page_url and href.startswith("/"):
                href = urljoin(page_url, href)
            if href.startswith("http"):
                return href
    return ""


def parse_google_photos_album(html: str) -> list[str]:
    """Extract image URLs from a shared Google Photos album page."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in _LH3_RE.finditer(html or ""):
        raw = match.group(0).rstrip(".,);")
        base = raw.split("=", 1)[0]
        url = base + "=w1600"
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _headers_for(url: str) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("pximg.net"):
        headers["Referer"] = _PIXIV_REFERER
    return headers


def _http_get(url: str, *, client: httpx.Client) -> httpx.Response:
    return client.get(url, headers=_headers_for(url), follow_redirects=True)


def _suffix_for(url: str, data: bytes) -> str:
    path = urlsplit(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if path.endswith(ext) or f"{ext}!" in path:
            return ".jpg" if ext == ".jpeg" else ext
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    return ".jpg"


def _filename_for(source_url: str, data: bytes) -> str:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    return digest + _suffix_for(source_url, data)


def _save_ref(
    *,
    data: bytes,
    source_url: str,
    kind: str,
    region: str = "",
    tags: str = "",
    caption: str = "",
) -> bool:
    if source_url_exists(source_url):
        return False
    PHOTO_REFS_DIR.mkdir(parents=True, exist_ok=True)
    filename = _filename_for(source_url, data)
    dest = PHOTO_REFS_DIR / filename
    if not dest.exists():
        dest.write_bytes(data)
    row_id = insert_photo_ref(
        filename=filename,
        kind=kind,
        region=region,
        tags=tags,
        caption=caption,
        source_url=source_url,
    )
    return row_id is not None


def _download_and_save(
    url: str,
    *,
    client: httpx.Client,
    kind: str,
    region: str = "",
    tags: str = "",
    caption: str = "",
) -> bool:
    if source_url_exists(url):
        return False
    try:
        response = _http_get(url, client=client)
        response.raise_for_status()
        ctype = (response.headers.get("content-type") or "").lower()
        if "text/html" in ctype and kind == "self":
            resolved = parse_moegirl_file_page(response.text, str(response.url))
            if not resolved:
                log.warning("Could not resolve image URL from %s", url)
                return False
            return _download_and_save(
                resolved,
                client=client,
                kind=kind,
                region=region,
                tags=tags,
                caption=caption or url,
            )
        data = response.content
        if len(data) < 1024:
            log.warning("Skipping tiny download from %s", url)
            return False
        return _save_ref(
            data=data,
            source_url=url,
            kind=kind,
            region=region,
            tags=tags,
            caption=caption or url,
        )
    except Exception as exc:
        log.warning("Photo-ref download failed for %s: %s", url, exc)
        return False


def _seed_character_urls() -> None:
    if not CHARACTER_URLS:
        return
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in CHARACTER_URLS:
            _download_and_save(
                url,
                client=client,
                kind="self",
                tags="self,外观",
                caption=url,
            )
            time.sleep(0.15)


def _seed_google_photos() -> None:
    if not GOOGLE_PHOTOS_ALBUM:
        return
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        try:
            response = _http_get(GOOGLE_PHOTOS_ALBUM, client=client)
            response.raise_for_status()
        except Exception as exc:
            log.warning("Google Photos album fetch failed: %s", exc)
            return
        urls = parse_google_photos_album(response.text)
        if not urls:
            log.warning("Google Photos album contained no image URLs")
            return
        for url in urls:
            _download_and_save(
                url,
                client=client,
                kind="self",
                tags="self,外观,google_photos",
                caption=GOOGLE_PHOTOS_ALBUM,
            )
            time.sleep(0.15)


def _acceptable_commons_file(title: str, info: dict) -> bool:
    if _SKIP_NAME_RE.search(title or ""):
        return False
    mime = str(info.get("mime") or "").lower()
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        return False
    width = int(info.get("thumbwidth") or info.get("width") or 0)
    height = int(info.get("thumbheight") or info.get("height") or 0)
    return min(width, height) >= _MIN_EDGE or (width == 0 and height == 0)


def harvest_wikimedia_category(payload: dict, *, limit: int) -> list[dict]:
    """Pick downloadable files from a Commons API query payload."""
    pages = (payload.get("query") or {}).get("pages") or {}
    picked: list[dict] = []
    for page in pages.values():
        if len(picked) >= limit:
            break
        title = page.get("title") or ""
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        if not _acceptable_commons_file(title, info):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        picked.append({
            "title": title,
            "url": url.split("?", 1)[0],
            "descriptionurl": info.get("descriptionurl") or "",
        })
    return picked


def _seed_wikimedia_scenes() -> None:
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        _fill_region(client, "japan", JAPAN_CATEGORIES)
        _fill_region(client, "china", CHINA_CATEGORIES)


def _fill_region(
    client: httpx.Client,
    region: str,
    categories: tuple[tuple[str, str], ...],
) -> None:
    remaining = _SCENE_PER_REGION - count_scene_refs(region)
    if remaining <= 0:
        return
    per_category = max(8, remaining // max(len(categories), 1) + 2)
    for category, tag in categories:
        remaining = _SCENE_PER_REGION - count_scene_refs(region)
        if remaining <= 0:
            return
        try:
            payload = _commons_category(client, category, min(per_category, remaining))
        except Exception as exc:
            log.warning("Wikimedia category %s failed: %s", category, exc)
            time.sleep(0.4)
            continue
        files = harvest_wikimedia_category(payload, limit=min(per_category, remaining))
        for item in files:
            remaining = _SCENE_PER_REGION - count_scene_refs(region)
            if remaining <= 0:
                return
            saved = _download_and_save(
                item["url"],
                client=client,
                kind="scene",
                region=region,
                tags=f"scene,{region},{tag}",
                caption=item.get("descriptionurl") or item["title"],
            )
            if saved:
                remaining -= 1
            time.sleep(0.2)
        time.sleep(0.35)


def _commons_category(client: httpx.Client, category: str, limit: int) -> dict:
    params = {
        "action": "query",
        "format": "json",
        "generator": "categorymembers",
        "gcmtitle": category,
        "gcmtype": "file",
        "gcmlimit": str(max(1, min(limit * 2, 50))),
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
        "iiurlwidth": "1280",
    }
    response = client.get(
        _WIKIMEDIA_API,
        params=params,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()
