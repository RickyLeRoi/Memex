"""Safe download and storage of images found in external pages.

The URL comes from content we do not control, so every fetch is hardened:
https only, standard port, every address the host resolves to must be public (SSRF), redirects are followed by hand and
re-checked at each hop, size is capped while streaming, and the format is decided from the magic bytes (never from the
URL or the Content-Type). SVG is refused on purpose: it can carry scripts.
Known limit: the check resolves the name before connecting, so a DNS-rebinding race is not fully closed.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_REDIRECTS = 3
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MEDIA_NAME = re.compile(r"^[0-9a-f]{40}\.(jpg|png|gif|webp)$")
CONTENT_TYPE_BY_EXT = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
MIME_FOR_VISION = CONTENT_TYPE_BY_EXT


class UnsafeUrl(Exception):
    pass


class ImageRejected(Exception):
    pass


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrl("only https URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrl("URL without host or with credentials")
    if parsed.port not in (None, 443):
        raise UnsafeUrl("non-standard port")
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeUrl(f"cannot resolve {parsed.hostname}") from e
    if not infos or not all(_is_public(info[4][0]) for info in infos):
        raise UnsafeUrl(f"{parsed.hostname} does not resolve to a public address")


def sniff_extension(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def fetch_image(http: httpx.Client, url: str, max_bytes: int) -> tuple[bytes, str]:
    """Return (bytes, extension) or raise UnsafeUrl / ImageRejected / httpx.HTTPError."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(current)
        with http.stream("GET", current, follow_redirects=False, headers={"Accept": "image/*"}) as response:
            if response.is_redirect:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in IMAGE_TYPES:
                raise ImageRejected(f"not an accepted image type: {content_type or 'unknown'}")
            try:
                declared = int(response.headers.get("content-length") or 0)
            except ValueError:
                declared = 0
            if declared > max_bytes:
                raise ImageRejected("image too large")
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise ImageRejected("image too large")
        extension = sniff_extension(bytes(data))
        if not extension:
            raise ImageRejected("content is not a supported image format")
        return bytes(data), extension
    raise ImageRejected("too many redirects")


def try_fetch_image(http: httpx.Client, url: str, max_bytes: int) -> tuple[bytes, str] | None:
    try:
        return fetch_image(http, url, max_bytes)
    except (UnsafeUrl, ImageRejected, httpx.HTTPError) as e:
        log.info("Image skipped %s: %s", url, e)
        return None


def store_image(media_dir: Path, data: bytes, extension: str) -> str:
    name = f"{hashlib.sha1(data).hexdigest()}.{extension}"
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / name
    if not target.exists():
        tmp = target.with_name(name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return name


UPLOAD_EXTENSIONS = {"jpg", "png", "webp"}  # screenshots; GIF and SVG are not accepted for uploads


def store_upload(media_dir: Path, data: bytes, max_bytes: int) -> str:
    """Validate and store a manually uploaded screenshot. The format comes from the bytes, never from the client."""
    if not data:
        raise ImageRejected("empty file")
    if len(data) > max_bytes:
        raise ImageRejected(f"file too large (max {max_bytes // 1_000_000} MB)")
    extension = sniff_extension(data)
    if extension not in UPLOAD_EXTENSIONS:
        raise ImageRejected("only PNG, JPEG and WebP images are accepted")
    return store_image(media_dir, data, extension)


def _jsonld_images(html: str) -> list[str]:
    found: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            for entry in value:
                collect(entry)
        elif isinstance(value, dict):
            collect(value.get("url") or value.get("contentUrl"))

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "image" in node:
                collect(node["image"])
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for script in BeautifulSoup(html, "lxml").find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or script.get_text() or ""))
        except json.JSONDecodeError:
            continue
    return found


def cover_candidates(html: str, page_url: str, meta: dict[str, str]) -> list[str]:
    ordered = [*_jsonld_images(html), meta.get("og:image", ""), meta.get("twitter:image", "")]
    out: list[str] = []
    for candidate in ordered:
        absolute = urljoin(page_url, candidate.strip()) if candidate and candidate.strip() else ""
        if absolute and absolute not in out:
            out.append(absolute)
    return out
