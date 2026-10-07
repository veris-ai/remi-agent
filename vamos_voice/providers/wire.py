"""Small pieces every websocket-speaking provider shares."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

import websockets

OPEN_TIMEOUT_S = 15

#: How a provider opens its vendor socket; tests replace it to capture frames offline.
Connect = Callable[[str, Mapping[str, str]], AbstractAsyncContextManager[Any]]


def connect_websocket(url: str, headers: Mapping[str, str]) -> AbstractAsyncContextManager[Any]:
    """The ``websockets`` asyncio client: the class the bench's trace adapters hook.

    Keys travel in headers, never in the URL, so they stay out of logs.
    """
    return websockets.asyncio.client.connect(
        url, additional_headers=dict(headers), open_timeout=OPEN_TIMEOUT_S, max_size=None
    )


async def run_until_first_done(**coroutines: Coroutine[Any, Any, None]) -> None:
    """Run the call's loops (caller→vendor, vendor→caller) until one ends; cancel the rest,
    and re-raise the first failure."""
    tasks = [asyncio.create_task(coro, name=name) for name, coro in coroutines.items()]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
