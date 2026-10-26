"""In-app update checks against the official GitHub Releases channel.

The release must carry two assets: InviterStudioPortable.zip and SHA256.txt
("hex  filename" per line, as produced by tools.distribution). The portable
archive is downloaded to a temp folder and its SHA-256 is verified before the
user is offered to open it; nothing is ever opened with an unknown checksum.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path

import aiohttp

from core.version import UPDATE_REPO, VERSION

API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
PORTABLE_ASSET = "InviterStudioPortable.zip"
# Compatibility alias for older callers.
SETUP_ASSET = PORTABLE_ASSET
CHECKSUM_ASSET = "SHA256.txt"
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(tag: str) -> tuple[int, int, int] | None:
    match = _TAG_RE.match(str(tag).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(tag: str, current: str = VERSION) -> bool:
    candidate = parse_version(tag)
    running = parse_version(current)
    if candidate is None or running is None:
        return False
    return candidate > running


def expected_checksum(checksum_text: str, filename: str = PORTABLE_ASSET) -> str | None:
    """Return the recorded hex digest for the portable asset, if present."""
    for line in str(checksum_text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] and Path(parts[1]).name == filename:
            digest = parts[0].lower()
            return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None
    return None


def hash_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _client(timeout: float) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))


async def fetch_latest_release() -> dict | None:
    """Return release metadata when a newer version exists, else None."""
    try:
        async with _client(15) as session:
            async with session.get(API_URL, headers={"Accept": "application/vnd.github+json"}) as response:
                if response.status == 404:
                    raise RuntimeError("Обновления не найдены: в репозитории ещё нет релизов")
                if response.status != 200:
                    raise RuntimeError(f"GitHub ответил кодом {response.status}; попробуйте позже")
                data = await response.json(content_type=None)
    except aiohttp.ClientError:
        raise RuntimeError("Не удалось связаться с GitHub. Проверьте интернет и попробуйте позже") from None

    tag = str(data.get("tag_name") or "")
    if not is_newer(tag):
        return None
    assets = {
        str(item.get("name") or ""): str(item.get("browser_download_url") or "") for item in data.get("assets", [])
    }
    if PORTABLE_ASSET not in assets:
        raise RuntimeError("В последнем релизе нет portable-архива; откройте страницу релиза вручную")
    return {
        "version": tag.lstrip("v"),
        "notes": str(data.get("body") or "").strip()[:4000],
        "portable_url": assets[PORTABLE_ASSET],
        "checksum_url": assets.get(CHECKSUM_ASSET, ""),
        "page_url": str(data.get("html_url") or ""),
    }


async def fetch_checksum(url: str) -> str | None:
    if not url:
        return None
    try:
        async with _client(15) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.text()
    except aiohttp.ClientError:
        return None


async def download_installer(
    url: str,
    destination: str | Path,
    expected: str | None = None,
    progress=None,
) -> Path:
    """Stream a release asset to destination, verify SHA-256, return the path.

    Raises RuntimeError on network failure or checksum mismatch; the partial
    file is always removed.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temporary = destination.with_name(destination.name + ".part")
    try:
        async with _client(None) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"GitHub отдал файл с кодом {response.status}")
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with open(temporary, "wb") as handle:
                    async for chunk in response.content.iter_chunked(1 << 20):
                        handle.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        actual = digest.hexdigest()
        if expected and actual != expected.lower():
            raise RuntimeError("Контрольная сумма portable-архива не совпала. Файл удалён; скачайте позже")
        os.replace(temporary, destination)
        return destination
    except aiohttp.ClientError:
        raise RuntimeError("Загрузка прервана. Проверьте интернет и попробуйте снова") from None
    except asyncio.CancelledError:
        raise
    finally:
        temporary.unlink(missing_ok=True)


async def download_update(portable_url: str, checksum_url: str, folder: str | Path, progress=None) -> Path:
    """Download portable archive + checksum, verify, and return the checked ZIP."""
    expected = expected_checksum(await fetch_checksum(checksum_url) or "") if checksum_url else None
    if checksum_url and expected is None:
        raise RuntimeError("Не удалось получить контрольную сумму из релиза; обновление отменено")
    destination = Path(folder) / PORTABLE_ASSET
    return await download_installer(portable_url, destination, expected, progress)
