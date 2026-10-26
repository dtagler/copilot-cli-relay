"""Framing and resource-lifetime regressions, without live provider requests."""
from __future__ import annotations

import asyncio
import time
from contextlib import aclosing, suppress

import anyio
import httpx
import pytest
from starlette.requests import ClientDisconnect, Request

from copilot_cli_relay import claude_proxy, codex_proxy, proxy_shared
from copilot_cli_relay.config import Settings, reset_settings_for_tests


class Connected:
    async def is_disconnected(self):
        return False


def producer_tasks():
    return [
        task for task in asyncio.all_tasks()
        if task.get_name().startswith("copilot-relay-stream-") and not task.done()
    ]


@pytest.mark.parametrize(
    "parts,expected",
    [
        ([b"data: {}\n", b"\n"], [False, True]),
        ([b"event: result\n", b"data: {}\n\n"], [False, True]),
        ([b"data: {}\r", b"\n\r", b"\n"], [False, False, True]),
        ([b": comment\n", b"\n", b"data:"], [False, True, False]),
        ([b'data: {"text":"\xe2', b'\x82\xac"}\n\n'], [False, True]),
        ([b"\r", b"\n", b"data: x\r\r", b"\n"], [False, True, False, True]),
    ],
)
def test_sse_boundary_across_chunks(parts, expected):
    boundary = proxy_shared._SSEBoundary()
    assert boundary.can_ping
    for part, can_ping in zip(parts, expected, strict=True):
        boundary.feed(part)
        assert boundary.can_ping is can_ping


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        (
            b'event: response.function_call_arguments.delta\ndata: {"call_id":"call_1","delta":"hel',
            b'lo"}\n\n',
        ),
        (b"event: response.completed\n", b'data: {"type":"response.completed"}\n\n'),
        (b'data: {"done":true}\n', b"\n"),
        (b'data: {"done":true}\r\n\r', b"\n"),
        (b'data: {"text":"\xe2', b'\x82\xac"}\n\n'),
        (b": upstream comment\n", b"\n"),
    ],
)
async def test_no_ping_inside_fragmented_event(monkeypatch, prefix, suffix):
    monkeypatch.setattr(proxy_shared, "PING_INTERVAL_SECS", 0.002)
    release = asyncio.Event()
    polled_after_timeouts = asyncio.Event()

    class Source:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            yield prefix
            await release.wait()
            yield suffix

    class PollingRequest:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            if self.calls >= 4:
                polled_after_timeouts.set()
            return False

    async with aclosing(proxy_shared.chunks_with_keepalive(Source(), PollingRequest())) as chunks:
        assert await anext(chunks) == (prefix, None)
        pending = asyncio.create_task(anext(chunks))
        try:
            with anyio.fail_after(2):
                await polled_after_timeouts.wait()
            assert not pending.done(), "heartbeat bytes must not split an SSE event"
            release.set()
            assert await pending == (suffix, None)
            with pytest.raises(StopAsyncIteration):
                await anext(chunks)
        finally:
            release.set()
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
    assert not producer_tasks()


async def test_ping_after_complete_event_does_not_cancel_reader(monkeypatch):
    monkeypatch.setattr(proxy_shared, "PING_INTERVAL_SECS", 0.002)
    release = asyncio.Event()
    first = b"data: first\r\n\r\n"
    last = b"data: last\n\n"

    class Source:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            yield first
            await release.wait()
            yield last

    async with aclosing(proxy_shared.chunks_with_keepalive(Source(), Connected())) as chunks:
        assert await anext(chunks) == (first, None)
        with anyio.fail_after(2):
            assert await anext(chunks) == (b"", "ping")
        release.set()
        remaining = [chunk async for chunk in chunks]
    assert [chunk for chunk, sentinel in remaining if sentinel is None] == [last]
    assert not producer_tasks()


@pytest.mark.parametrize("fill_queue", [False, True])
async def test_early_close_joins_producer(monkeypatch, fill_queue):
    monkeypatch.setattr(proxy_shared, "PING_INTERVAL_SECS", 0.002)
    blocked = asyncio.Event()
    finalized = asyncio.Event()

    class Source:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            try:
                for index in range(30 if fill_queue else 1):
                    if index == 9:
                        blocked.set()
                    yield b"data: event\n\n"
                blocked.set()
                await asyncio.Event().wait()
            finally:
                finalized.set()

    async with aclosing(proxy_shared.chunks_with_keepalive(Source(), Connected())) as chunks:
        assert await anext(chunks) == (b"data: event\n\n", None)
        with anyio.fail_after(2):
            await blocked.wait()
    assert finalized.is_set()
    assert not producer_tasks()


async def test_disconnect_is_terminal_only_after_producer_finalizes():
    started = asyncio.Event()
    finalized = asyncio.Event()

    class Source:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            try:
                started.set()
                yield b"data: first\n\n"
                await asyncio.Event().wait()
            finally:
                finalized.set()

    class Disconnecting:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            await started.wait()
            return self.calls > 1

    async with aclosing(proxy_shared.chunks_with_keepalive(Source(), Disconnecting())) as chunks:
        assert await anext(chunks) == (b"data: first\n\n", None)
        assert await anext(chunks) == (b"", "disconnect")
        assert finalized.is_set()
        assert not producer_tasks()


async def test_immediate_disconnect_closes_both_channel_endpoints(monkeypatch):
    factory = anyio.create_memory_object_stream
    pairs = []

    def capture_channel(*args, **kwargs):
        pair = factory(*args, **kwargs)
        pairs.append(pair)
        return pair

    monkeypatch.setattr(proxy_shared.anyio, "create_memory_object_stream", capture_channel)

    class Disconnected:
        async def is_disconnected(self):
            return True

    class Source:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            yield b"data: unused\n\n"

    async with aclosing(proxy_shared.chunks_with_keepalive(Source(), Disconnected())) as chunks:
        assert await anext(chunks) == (b"", "disconnect")
        stats = pairs[0][0].statistics()
        assert stats.open_send_streams == 0
        assert stats.open_receive_streams == 0
        assert not producer_tasks()


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.started = asyncio.Event()
        self.finalized = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        try:
            yield b"data: first\n\n"
            await asyncio.Event().wait()
        finally:
            self.finalized.set()

    async def aclose(self):
        # Deliberately checkpoint: cleanup must survive cancellation.
        await anyio.sleep(0)
        self.closed = True


@pytest.fixture
def settings():
    reset_settings_for_tests(Settings(
        proxy_port=4141, github_token="gho_test", api_base="https://upstream.test",
        integration_id="copilot-developer-cli", editor_version="test",
        log_level="info", log_bodies=False,
    ))
    yield
    reset_settings_for_tests(None)


async def make_response(protocol, version, receive, client):
    path = "/codex/v1/responses" if protocol == "codex" else "/claude/v1/messages"
    scope = {
        "type": "http", "asgi": {"spec_version": version}, "method": "POST",
        "path": path, "headers": [], "query_string": b"",
    }
    request = Request(scope, receive)
    handler = (
        codex_proxy._codex_stream_response if protocol == "codex"
        else claude_proxy._stream_response
    )
    response = await handler(
        client=client, url="https://upstream.test/responses", body=b"{}",
        headers={}, request=request, model="test", request_id="test",
        started=time.monotonic(),
    )
    assert isinstance(response, proxy_shared.UpstreamStreamingResponse)
    return scope, response


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_downstream_send_failure_closes_acquired_upstream(settings, protocol, version, phase):
    stream = WaitingStream()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == ("http.response.start" if phase == "headers" else "http.response.body"):
            raise OSError("client disconnected")

    def upstream_handler(_request):
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler)) as client:
        scope, response = await make_response(protocol, version, receive, client)
        with anyio.fail_after(2), pytest.raises((OSError, ClientDisconnect)):
            await response(scope, receive, send)
    assert stream.closed
    assert not producer_tasks()


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_real_disconnect_or_cancellation_finishes_cleanup(
    settings, monkeypatch, protocol, version, cancel_task,
):
    monkeypatch.setattr(proxy_shared, "PING_INTERVAL_SECS", 0.002)
    stream = WaitingStream()
    disconnected = asyncio.Event()
    first_sent = asyncio.Event()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_sent.set()

    def upstream_handler(_request):
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler)) as client:
        scope, response = await make_response(protocol, version, receive, client)
        runner = asyncio.create_task(response(scope, receive, send))
        try:
            with anyio.fail_after(2):
                await first_sent.wait()
                if cancel_task:
                    runner.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await runner
                else:
                    disconnected.set()
                    await runner
            assert stream.closed
            assert stream.finalized.is_set()
            assert not producer_tasks()
        finally:
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner


@pytest.mark.parametrize("protocol", ["codex", "claude"])
async def test_task_cancellation_while_opening_upstream_propagates(settings, protocol):
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def upstream_handler(_request):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            finalized.set()

    async def receive():
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler)) as client:
        task = asyncio.create_task(make_response(protocol, "2.4", receive, client))
        try:
            with anyio.fail_after(2):
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert finalized.is_set()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_cancellation_during_producer_finalization_is_not_swallowed():
    closing = asyncio.Event()
    release = asyncio.Event()
    finalized = asyncio.Event()

    class Source:
        is_stream_consumed = False
        headers = {}

        async def aiter_raw(self):
            try:
                yield b"data: first\n\n"
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()
                finalized.set()

    async def consume_one():
        async with aclosing(proxy_shared.chunks_with_keepalive(Source(), Connected())) as chunks:
            await anext(chunks)

    runner = asyncio.create_task(consume_one())
    try:
        with anyio.fail_after(2):
            await closing.wait()
            for _ in range(2):
                runner.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not runner.done(), "cancellation must wait for producer finalization"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await runner
        assert finalized.is_set()
        assert not producer_tasks()
    finally:
        release.set()
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner


@pytest.mark.parametrize("protocol", ["codex", "claude"])
@pytest.mark.parametrize("version", ["2.0", "2.4"])
@pytest.mark.parametrize("fail_headers", [False, True])
async def test_cancellation_during_one_shot_upstream_close(
    settings, protocol, version, fail_headers,
):
    closing = asyncio.Event()
    release = asyncio.Event()
    finalized = asyncio.Event()
    close_calls = 0

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: complete\n\n"
            # Normal EOF makes HTTPX mark the response closed before it
            # awaits this stream's aclose, inside the producer task.

        async def aclose(self):
            nonlocal close_calls
            close_calls += 1
            if close_calls > 1:
                return
            closing.set()
            await release.wait()
            finalized.set()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if fail_headers and message["type"] == "http.response.start":
            raise OSError("downstream header send failed")

    stream = SlowClose()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream)),
    ) as client:
        scope, response = await make_response(protocol, version, receive, client)
        runner = asyncio.create_task(response(scope, receive, send))
        try:
            with anyio.fail_after(2):
                await closing.wait()
                for _ in range(2):
                    runner.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    assert not runner.done(), "transport close must not be interrupted"
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await runner
            assert finalized.is_set()
            assert close_calls == 1
            assert not producer_tasks()
        finally:
            release.set()
            runner.cancel()
            with suppress(asyncio.CancelledError, OSError, ClientDisconnect):
                await runner


async def test_cleanup_failure_is_propagated():
    async def fail():
        raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await proxy_shared._await_cleanup(proxy_shared._start_cleanup(fail))
    assert not producer_tasks()


async def test_cleanup_task_cancellation_does_not_spin():
    async def cancel():
        raise asyncio.CancelledError

    with anyio.fail_after(2), pytest.raises(asyncio.CancelledError):
        await proxy_shared._await_cleanup(proxy_shared._start_cleanup(cancel))
    assert not producer_tasks()


async def test_cleanup_task_cancelled_before_start_is_propagated():
    async def close():
        await asyncio.sleep(0)

    task = proxy_shared._start_cleanup(close)
    task.cancel()
    with anyio.fail_after(2), pytest.raises(asyncio.CancelledError):
        await proxy_shared._await_cleanup(task)
    assert not producer_tasks()


async def test_caller_cancellation_wins_over_cleanup_error_and_redacts_log(caplog):
    started = asyncio.Event()
    release = asyncio.Event()
    synthetic_secret = "gho_" + "A" * 36

    async def fail_after_release():
        started.set()
        await release.wait()
        raise RuntimeError("cleanup failed " + synthetic_secret)

    task = proxy_shared._start_cleanup(fail_after_release)
    caller = asyncio.create_task(proxy_shared._await_cleanup(task))
    try:
        with anyio.fail_after(2):
            await started.wait()
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
        assert "cleanup failed during cancellation" in caplog.text
        assert synthetic_secret not in caplog.text
        assert not producer_tasks()
    finally:
        release.set()
        caller.cancel()
        with suppress(asyncio.CancelledError):
            await caller


async def test_anyio_cancellation_during_cleanup_is_delivered_after_finalization():
    started = asyncio.Event()
    release = asyncio.Event()
    finalized = asyncio.Event()
    scope = anyio.CancelScope()
    continued = False

    async def close():
        started.set()
        await release.wait()
        finalized.set()

    async def caller():
        nonlocal continued
        with scope:
            await proxy_shared._await_cleanup(proxy_shared._start_cleanup(close))
            continued = True

    runner = asyncio.create_task(caller())
    try:
        with anyio.fail_after(2):
            await started.wait()
            scope.cancel()
            await asyncio.sleep(0)
            assert not runner.done()
            release.set()
            await runner
        assert finalized.is_set()
        assert scope.cancelled_caught
        assert not continued
        assert not producer_tasks()
    finally:
        release.set()
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner


async def test_transport_close_is_single_flight_for_multiple_callers():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Inner(httpx.AsyncByteStream):
        async def aclose(self):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

    stream = proxy_shared._CancellationSafeStream(Inner())
    first = asyncio.create_task(stream.aclose())
    second = None
    try:
        with anyio.fail_after(2):
            await started.wait()
            second = asyncio.create_task(stream.aclose())
            first.cancel()
            await asyncio.sleep(0)
            assert not first.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await second
        assert calls == 1
        assert not producer_tasks()
    finally:
        release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
