"""Pass a validated recording ID to the local ASR service, never a Telegram credential."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

MAX_TEXT = 16 * 1024 * 1024


class ASRStalled(RuntimeError):
    """The dedicated ASR process stopped after a checkpoint timeout."""


async def transcribe_recording(job, destination):
    async with asyncio.timeout(6 * 3600 + 180):
        reader, writer = await asyncio.open_unix_connection('/run/psy-asr/transcribe.sock')
        try:
            request = {key: job[key] for key in ('id', 'ext', 'language')}
            writer.write(json.dumps(request).encode() + b'\n')
            await writer.drain()
            header = json.loads(await reader.readline())
            if header.get('complete') is False and header.get('error') == 'stalled':
                raise ASRStalled('ASR progress timed out')
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
                    # Give the gateway read access through its restricted shared group.
                    os.chmod(temp, 0o640)
                    os.replace(temp, destination)
                finally:
                    temp.unlink(missing_ok=True)
            return header
        finally:
            writer.close()
            await writer.wait_closed()
