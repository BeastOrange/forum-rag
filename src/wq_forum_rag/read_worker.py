"""Shared bounded workers for cancellable forum reads."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import threading

_slots = threading.BoundedSemaphore(2)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='forum-read')
_cancel_event = ContextVar('forum_read_cancel_event', default=None)


class ReadBusy(Exception):
    """Both forum read workers are occupied."""


class ReadCancelled(Exception):
    """The client cancelled the read."""


def cancelled():
    event = _cancel_event.get()
    return event is not None and event.is_set()


def check_cancelled():
    if cancelled():
        raise ReadCancelled('Forum read cancelled')


async def run_read(function, *args, **kwargs):
    if not _slots.acquire(blocking=False):
        raise ReadBusy('Forum read capacity is busy')
    event = threading.Event()
    def invoke():
        token = _cancel_event.set(event)
        try:
            check_cancelled()
            return function(*args, **kwargs)
        finally:
            _cancel_event.reset(token)
            _slots.release()
    try:
        future = _executor.submit(invoke)
    except BaseException:
        _slots.release()
        raise
    wrapped = asyncio.wrap_future(future)
    # Consume a detached worker's exception after its awaiting task is cancelled.
    def consume(done):
        if not done.cancelled():
            done.exception()
    wrapped.add_done_callback(consume)
    try:
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        event.set()
        raise
