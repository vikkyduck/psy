"""Run the actual broker dispatcher with synthetic credentials and membership."""
import asyncio
import json
from pathlib import Path

import access_broker


async def start_broker(path, token, member):
    async def handle(reader, writer):
        try:
            request = json.loads(await reader.readline())
            result = access_broker.dispatch(request, token, member)
            writer.write(json.dumps(result).encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(Path(path)))
