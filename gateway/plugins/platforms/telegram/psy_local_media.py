"""Disk-backed Telegram local Bot API media ingestion for the PSY deployment."""
import asyncio
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

BLOCK = 1024 * 1024


def cache_local_file(source, *, filename='', mime_type='', default_kind=None):
    from hermes_constants import get_hermes_home
    from gateway.platforms.base import (
        CachedMedia, SUPPORTED_VIDEO_TYPES, SUPPORTED_IMAGE_DOCUMENT_TYPES,
        _AUDIO_MIME_TYPES, _resolve_media_ext, cache_media_bytes,
    )
    home = get_hermes_home().resolve()
    root = home / 'telegram-bot-api'
    src = Path(source).resolve(strict=True)
    if not src.is_relative_to(root.resolve()) or not src.is_file():
        raise ValueError('Local Telegram attachment is outside its private data directory')
    size = src.stat().st_size
    if not 0 < size <= 2 * 1024**3:
        raise ValueError('Local attachment must be between 1 byte and 2 GiB')
    name = filename or src.name
    ext = _resolve_media_ext(name, mime_type)
    mime = (mime_type or '').lower()
    if mime.startswith('image/') or ext in SUPPORTED_IMAGE_DOCUMENT_TYPES or default_kind == 'image':
        if size > 20 * 1024**2:
            raise ValueError('Image attachment exceeds the safe image decoding limit')
        return cache_media_bytes(src.read_bytes(), filename=name, mime_type=mime, default_kind='image')
    kind = 'document'
    for candidate, table, fallback in [('video', SUPPORTED_VIDEO_TYPES, '.mp4'), ('audio', _AUDIO_MIME_TYPES, '.ogg')]:
        if mime.startswith(candidate + '/') or ext in table or default_kind == candidate:
            kind = candidate
            ext = ext if ext in table else fallback
            mime = mime or table[ext]
            break
    if not re.fullmatch(r'\.[a-zA-Z0-9]{1,10}', ext or ''):
        ext = '.bin'
    folder = home / 'media-vault' / 'incoming' / kind
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < size + 512 * 1024**2:
        raise OSError('Not enough free disk space to retain this attachment safely')
    target = folder / (uuid.uuid4().hex + ext)
    before = src.stat()
    fd, temp = tempfile.mkstemp(dir=folder, suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as out, src.open('rb') as inp:
            shutil.copyfileobj(inp, out, length=BLOCK)
            out.flush()
            os.fsync(out.fileno())
        after = src.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or Path(temp).stat().st_size != size:
            raise OSError('Telegram attachment changed while copying')
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    display = re.sub(r'[^\w.\- ]', '_', name)
    return CachedMedia(str(target), mime or 'application/octet-stream', kind, display)


async def download_local_media(source, **kwargs):
    # getFile can wait for a multi-gigabyte Telegram download; never use PTB's short default here.
    try:
        file = await source.get_file(read_timeout=1800, connect_timeout=20, pool_timeout=30)
        if not file.file_path or not Path(file.file_path).is_absolute():
            raise ValueError('Local Bot API did not return an absolute attachment path')
        return await asyncio.to_thread(cache_local_file, file.file_path, **kwargs)
    except Exception as error:
        # Local Bot API paths can contain the bot token. Suppress the chained raw exception,
        # because upstream adapter loggers include exc_info=True even after redacting str(error).
        raise RuntimeError('Local Telegram attachment download/storage failed: ' + type(error).__name__) from None
