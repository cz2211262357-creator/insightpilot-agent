"A process-local loop factory compatible with psycopg on Windows and Linux."

import asyncio


def new_event_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()
