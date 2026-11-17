"""Pass a validated recording ID to the local ASR service, never a Telegram credential."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

SOCKET = '/run/psy-asr/transcribe.sock'
MAX_TEXT = 16 * 1024 * 1024
# The service stops an attempt after 6 hours and reports "deadline"; allow for the snapshot.
ATTEMPT_TIMEOUT = 6 * 3600 + 1800


class ASRStalled(RuntimeError):
    """The dedicated ASR process stopped after a checkpoint timeout."""


class ASRDeadline(RuntimeError):
    """One attempt used its time slot. The service kept the journal, so the next attempt resumes."""


async def transcribe_recording(job, destination, saved=None):
    """Write the transcript to destination, then await saved(header) and acknowledge it so the
    service can drop its private copy. Without an acknowledgement it keeps the resumable journal."""
    async with asyncio.timeout(ATTEMPT_TIMEOUT):
        reader, writer = await asyncio.open_unix_connection(SOCKET)
        try:
            request = {key: job[key] for key in ('id', 'ext', 'language','size','content_id')}
            writer.write(json.dumps(request).encode() + b'\n')
            await writer.drain()
            header = json.loads(await reader.readline())
            if header.get('complete') is False and header.get('error') == 'stalled':
                raise ASRStalled('ASR progress timed out')
            if header.get('complete') is False and header.get('error') == 'deadline':
                raise ASRDeadline('ASR attempt reached its time slot')
            size = header.pop('text_bytes', None)
            if header.get('complete') is not True or type(size) is not int or not 0 < size <= MAX_TEXT:
                raise RuntimeError('Local transcription unavailable')
            destination = Path(destination)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as out:
                temp = Path(out.name)
                try:
                    remaining = size
                    while remaining:
                        chunk = await reader.readexactly(min(65536, remaining))
                        out.write(chunk)
                        remaining -= len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                    out.close()
                    os.chmod(temp, 0o600)
                    os.replace(temp, destination)
                finally:
                    temp.unlink(missing_ok=True)
            if saved is not None:
                await saved(header)
                try:
                    writer.write(b'{"saved":true}\n')
                    await writer.drain()
                except (ConnectionError, OSError):
                    # The result is stored. An older service closes early and keeps its copy.
                    pass
            return header
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def forget(ident):
    """Ask the service to stop any decode of this recording and remove its private files."""
    async with asyncio.timeout(90):
        reader, writer = await asyncio.open_unix_connection(SOCKET)
        try:
            writer.write(json.dumps({'action': 'delete', 'id': ident}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await reader.readline() or b'{}')
            if reply.get('deleted') is not True:
                raise RuntimeError('ASR files not removed')
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
