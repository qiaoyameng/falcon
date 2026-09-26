import asyncio

import pytest

import falcon
import falcon.asgi.app
from falcon import testing
from falcon.asgi import App
from falcon.asgi import SSEvent


HEARTBEAT = b': ping\n\n'

_INHERIT = object()


class ControllableClock:
    """A controllable clock that can stand in for ``asyncio.sleep()``.

    The clock only advances when :meth:`advance` is called, making it
    possible to deterministically test timeout-driven behavior.
    """

    def __init__(self):
        self.now = 0.0
        self.pending = []
        self.sleep_count = 0

    def sleep(self, delay):
        self.sleep_count += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending.append((self.now + delay, future))

        async def _wait():
            await future

        return _wait()

    def advance(self, seconds):
        self.now += seconds
        still_pending = []
        for deadline, future in self.pending:
            if future.done():
                continue
            if deadline <= self.now:
                future.set_result(None)
            else:
                still_pending.append((deadline, future))
        self.pending = still_pending

    @property
    def pending_count(self):
        self.pending = [entry for entry in self.pending if not entry[1].done()]
        return len(self.pending)


class GatedEmitter:
    """An SSE emitter factory gated by events, tracking its lifecycle."""

    def __init__(self, event_count, hang=True, error=None):
        self.gates = [asyncio.Event() for _ in range(event_count)]
        self.hang = hang
        self.error = error
        self.entered = 0
        self.finalized = 0
        self.gates_awaited = 0

    def __call__(self):
        return self._generate()

    async def _generate(self):
        self.entered += 1
        try:
            for index, gate in enumerate(self.gates):
                self.gates_awaited += 1
                await gate.wait()
                yield SSEvent(text=f'event-{index}')
            if self.error is not None:
                raise self.error
            if self.hang:
                await asyncio.Event().wait()
        finally:
            self.finalized += 1


class EventsResource:
    def __init__(self, emitter, heartbeat_interval=_INHERIT):
        self._emitter = emitter
        self._heartbeat_interval = heartbeat_interval

    async def on_get(self, req, resp):
        if self._heartbeat_interval is not _INHERIT:
            resp.sse_heartbeat_interval = self._heartbeat_interval
        resp.sse = self._emitter()


class SSEClient:
    """A minimal ASGI client driving an app via send/receive barriers."""

    def __init__(self, app):
        self._app = app
        self._request_sent = False
        self.disconnect = asyncio.Event()
        self.send_barrier = asyncio.Event()
        self.send_barrier.set()
        self.fail_sends = False
        self.messages = []
        self.send_attempts = 0
        self.task = None

    async def receive(self):
        if not self._request_sent:
            self._request_sent = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await self.disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(self, message):
        self.send_attempts += 1
        await self.send_barrier.wait()
        if self.fail_sends:
            raise RuntimeError('simulated send failure')
        self.messages.append(message)

    def start(self):
        scope = testing.create_scope(path='/events')
        self.task = asyncio.create_task(self._app(scope, self.receive, self.send))

    @property
    def started(self):
        return any(msg['type'] == 'http.response.start' for msg in self.messages)

    @property
    def bodies(self):
        return [
            msg.get('body', b'')
            for msg in self.messages
            if msg['type'] == 'http.response.body'
        ]


@pytest.fixture
def clock(monkeypatch):
    clock = ControllableClock()
    monkeypatch.setattr(falcon.asgi.app, '_sse_sleep', clock.sleep)
    return clock


def make_client(emitter, app_interval=None, resp_interval=_INHERIT):
    app = App()
    if app_interval is not None:
        app.resp_options.sse_heartbeat_interval = app_interval
    app.add_route('/events', EventsResource(emitter, resp_interval))
    client = SSEClient(app)
    client.start()
    return client


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError('Timed out waiting for condition')
        await asyncio.sleep(0)


async def flush(times=3):
    for _ in range(times):
        await asyncio.sleep(0)


async def assert_no_leftover_tasks():
    await flush()
    current = asyncio.current_task()
    leftover = [
        task
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]
    assert leftover == []


def test_heartbeat_sent_on_cadence_when_idle(clock):
    async def _test():
        emitter = GatedEmitter(event_count=0)
        client = make_client(emitter, app_interval=10)

        await wait_for(lambda: client.started)
        await wait_for(lambda: clock.pending_count == 1)

        clock.advance(9.9)
        await flush()
        assert client.bodies == []

        clock.advance(0.1)
        await wait_for(lambda: len(client.bodies) == 1)
        assert client.bodies == [HEARTBEAT]

        await wait_for(lambda: clock.pending_count == 1)
        clock.advance(10)
        await wait_for(lambda: len(client.bodies) == 2)
        assert client.bodies == [HEARTBEAT, HEARTBEAT]

        # The emitter was neither cancelled nor restarted by heartbeats.
        assert emitter.entered == 1
        assert emitter.finalized == 0

        client.disconnect.set()
        await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_business_event_takes_precedence_at_heartbeat_boundary(clock):
    async def _test():
        emitter = GatedEmitter(event_count=2)
        client = make_client(emitter, app_interval=10)

        await wait_for(lambda: clock.pending_count == 1)

        # The business event and the heartbeat deadline arrive together;
        #   the event must win and must not be dropped.
        emitter.gates[0].set()
        clock.advance(10)
        await wait_for(lambda: len(client.bodies) == 1)
        assert client.bodies == [b'data: event-0\n\n']

        # While idle again, the next heartbeat arrives on schedule.
        await wait_for(lambda: clock.pending_count == 1)
        clock.advance(10)
        await wait_for(lambda: len(client.bodies) == 2)
        assert client.bodies[1] == HEARTBEAT

        # Business events keep their original order relative to heartbeats.
        await wait_for(lambda: clock.pending_count == 1)
        emitter.gates[1].set()
        await wait_for(lambda: len(client.bodies) == 3)
        assert client.bodies == [
            b'data: event-0\n\n',
            HEARTBEAT,
            b'data: event-1\n\n',
        ]

        client.disconnect.set()
        await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_slow_consumer_does_not_accumulate_heartbeats(clock):
    async def _test():
        emitter = GatedEmitter(event_count=1)
        emitter.gates[0].set()
        client = make_client(emitter, app_interval=5)

        await wait_for(lambda: client.started)
        client.send_barrier.clear()

        # The first event send is now blocked downstream.
        await wait_for(lambda: client.send_attempts == 2)
        await wait_for(lambda: clock.pending_count == 0)
        tasks_before = set(asyncio.all_tasks())

        # Many heartbeat intervals elapse while send() is blocked; no
        #   heartbeat frames are queued, no new timers are created, and no
        #   further events are prefetched from the emitter.
        clock.advance(100)
        await flush()
        assert client.bodies == []
        assert clock.sleep_count == 1
        assert emitter.gates_awaited == 1
        assert emitter.entered == 1
        assert set(asyncio.all_tasks()) <= tasks_before

        # Unblocking the downstream delivers the pending event, and
        #   heartbeats then resume their cadence.
        client.send_barrier.set()
        await wait_for(lambda: len(client.bodies) == 1)
        assert client.bodies == [b'data: event-0\n\n']

        await wait_for(lambda: clock.pending_count == 1)
        clock.advance(5)
        await wait_for(lambda: len(client.bodies) == 2)
        assert client.bodies[1] == HEARTBEAT

        client.disconnect.set()
        await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_emitter_exhaustion_runs_finally(clock):
    async def _test():
        emitter = GatedEmitter(event_count=1, hang=False)
        emitter.gates[0].set()
        client = make_client(emitter, app_interval=10)

        await client.task
        assert emitter.entered == 1
        assert emitter.finalized == 1
        assert client.bodies == [b'data: event-0\n\n', b'']
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_emitter_error_propagates_and_runs_finally(clock):
    async def _test():
        emitter = GatedEmitter(event_count=1, error=RuntimeError('boom'))
        emitter.gates[0].set()
        client = make_client(emitter, app_interval=10)

        with pytest.raises(RuntimeError, match='boom'):
            await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_client_disconnect_runs_finally(clock):
    async def _test():
        emitter = GatedEmitter(event_count=0)
        client = make_client(emitter, app_interval=10)

        await wait_for(lambda: clock.pending_count == 1)
        client.disconnect.set()
        await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_request_cancellation_runs_finally(clock):
    async def _test():
        emitter = GatedEmitter(event_count=0)
        client = make_client(emitter, app_interval=10)

        await wait_for(lambda: clock.pending_count == 1)
        client.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_no_concurrent_anext_calls(clock):
    class TrackingEmitter:
        def __init__(self):
            self.concurrent = 0
            self.max_concurrent = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            try:
                await asyncio.Event().wait()
            finally:
                self.concurrent -= 1

    async def _test():
        emitter = TrackingEmitter()

        class Resource:
            async def on_get(self, req, resp):
                resp.sse = emitter

        app = App()
        app.resp_options.sse_heartbeat_interval = 10
        app.add_route('/events', Resource())
        client = SSEClient(app)
        client.start()

        # Several heartbeats fire while the single pending __anext__()
        #   call is left running.
        await wait_for(lambda: clock.pending_count == 1)
        for _ in range(3):
            clock.advance(10)
            await wait_for(lambda: clock.pending_count == 1)

        assert len(client.bodies) == 3
        assert emitter.max_concurrent == 1

        client.disconnect.set()
        await client.task
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_falsy_event_is_serialized_as_ping(clock):
    async def _test():
        finalized = 0

        async def emitter():
            nonlocal finalized
            try:
                yield
                await asyncio.Event().wait()
            finally:
                finalized += 1

        class Resource:
            async def on_get(self, req, resp):
                resp.sse = emitter()

        app = App()
        app.resp_options.sse_heartbeat_interval = 10
        app.add_route('/events', Resource())
        client = SSEClient(app)
        client.start()

        await wait_for(lambda: len(client.bodies) == 1)
        assert client.bodies == [HEARTBEAT]

        client.disconnect.set()
        await client.task
        assert finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_disconnect_right_after_event_sent(clock):
    async def _test():
        emitter = GatedEmitter(event_count=1)
        client = make_client(emitter, app_interval=10)

        await wait_for(lambda: clock.pending_count == 1)

        # The event and the disconnect arrive together; the event is still
        #   sent before the stream is wrapped up.
        emitter.gates[0].set()
        client.disconnect.set()
        await client.task

        assert client.bodies == [b'data: event-0\n\n', b'']
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_send_error_propagates_and_runs_finally(clock):
    async def _test():
        emitter = GatedEmitter(event_count=0)
        client = make_client(emitter, app_interval=10)

        await wait_for(lambda: clock.pending_count == 1)
        clock.advance(10)
        await wait_for(lambda: len(client.bodies) == 1)
        assert client.bodies == [HEARTBEAT]

        client.fail_sends = True
        await wait_for(lambda: clock.pending_count == 1)
        clock.advance(10)

        with pytest.raises(RuntimeError, match='simulated send failure'):
            await client.task
        assert emitter.finalized == 1
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_emitter_without_aclose(clock):
    class Emitter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def _test():
        class Resource:
            async def on_get(self, req, resp):
                resp.sse = Emitter()

        app = App()
        app.resp_options.sse_heartbeat_interval = 10
        app.add_route('/events', Resource())
        client = SSEClient(app)
        client.start()

        await client.task
        assert client.bodies == [b'']
        await assert_no_leftover_tasks()

    falcon.async_to_sync(_test)


def test_heartbeat_disabled_by_default(clock):
    async def _test():
        emitter = GatedEmitter(event_count=1, hang=False)
        client = make_client(emitter)

        await wait_for(lambda: client.started)

        # No heartbeats are emitted, and the heartbeat machinery is not
        #   even engaged when no interval has been configured.
        clock.advance(1000)
        await flush()
        assert client.bodies == []
        assert clock.sleep_count == 0

        emitter.gates[0].set()
        await client.task
        assert client.bodies == [b'data: event-0\n\n', b'']

    falcon.async_to_sync(_test)


def test_response_interval_overrides_app_default(clock):
    async def _test():
        emitter = GatedEmitter(event_count=0)
        client = make_client(emitter, app_interval=10, resp_interval=5)

        await wait_for(lambda: clock.pending_count == 1)
        clock.advance(5)
        await wait_for(lambda: len(client.bodies) == 1)
        assert client.bodies == [HEARTBEAT]

        client.disconnect.set()
        await client.task

    falcon.async_to_sync(_test)


def test_response_interval_disables_app_default(clock):
    async def _test():
        emitter = GatedEmitter(event_count=1, hang=False)
        client = make_client(emitter, app_interval=10, resp_interval=None)

        await wait_for(lambda: client.started)
        clock.advance(1000)
        await flush()
        assert client.bodies == []
        assert clock.sleep_count == 0

        emitter.gates[0].set()
        await client.task
        assert client.bodies == [b'data: event-0\n\n', b'']

    falcon.async_to_sync(_test)


def test_heartbeat_option_does_not_affect_regular_responses(clock):
    class TextResource:
        async def on_get(self, req, resp):
            resp.text = 'hello'

        async def on_head(self, req, resp):
            resp.text = 'hello'

    class StreamResource:
        async def on_get(self, req, resp):
            async def stream():
                yield b'chunk'

            resp.stream = stream()

    app = App()
    app.resp_options.sse_heartbeat_interval = 5
    app.add_route('/text', TextResource())
    app.add_route('/stream', StreamResource())

    client = testing.TestClient(app)

    result = client.simulate_get('/text')
    assert result.text == 'hello'

    result = client.simulate_get('/stream')
    assert result.content == b'chunk'

    result = client.simulate_head('/text')
    assert result.status_code == 200
    assert result.content == b''

    assert clock.sleep_count == 0


def test_heartbeat_integration_with_real_clock():
    async def _test():
        async def emitter():
            yield SSEvent(text='first')
            await asyncio.sleep(0.25)
            yield SSEvent(text='second')

        class Resource:
            async def on_get(self, req, resp):
                resp.sse = emitter()

        app = App()
        app.resp_options.sse_heartbeat_interval = 0.05
        app.add_route('/events', Resource())

        client = SSEClient(app)
        client.start()

        await wait_for(lambda: b'data: second\n\n' in client.bodies)
        client.disconnect.set()
        await client.task

        assert client.bodies[0] == b'data: first\n\n'
        assert client.bodies[-2] == b'data: second\n\n'
        heartbeats = client.bodies[1:-2]
        assert len(heartbeats) >= 2
        assert all(body == HEARTBEAT for body in heartbeats)

    falcon.async_to_sync(_test)


class TestIntervalValidation:
    @pytest.mark.parametrize('value', ['5', (5,), object(), True, False])
    def test_options_invalid_type(self, value):
        options = falcon.ResponseOptions()
        with pytest.raises(TypeError):
            options.sse_heartbeat_interval = value

    @pytest.mark.parametrize(
        'value', [0, 0.0, -1, -0.5, float('nan'), float('inf'), float('-inf')]
    )
    def test_options_invalid_value(self, value):
        options = falcon.ResponseOptions()
        with pytest.raises(ValueError):
            options.sse_heartbeat_interval = value

    def test_options_valid_values(self):
        options = falcon.ResponseOptions()
        assert options.sse_heartbeat_interval is None

        options.sse_heartbeat_interval = 5
        assert options.sse_heartbeat_interval == 5.0

        options.sse_heartbeat_interval = 0.25
        assert options.sse_heartbeat_interval == 0.25

        options.sse_heartbeat_interval = None
        assert options.sse_heartbeat_interval is None

    @pytest.mark.parametrize('value', ['5', True])
    def test_response_invalid_type(self, value):
        resp = falcon.asgi.Response()
        with pytest.raises(TypeError):
            resp.sse_heartbeat_interval = value

    @pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf')])
    def test_response_invalid_value(self, value):
        resp = falcon.asgi.Response()
        with pytest.raises(ValueError):
            resp.sse_heartbeat_interval = value

    def test_response_override_and_reset(self):
        resp = falcon.asgi.Response()
        assert resp.sse_heartbeat_interval is None

        resp.sse_heartbeat_interval = 2.5
        assert resp.sse_heartbeat_interval == 2.5

        resp.sse_heartbeat_interval = None
        assert resp.sse_heartbeat_interval is None

        del resp.sse_heartbeat_interval
        assert resp.sse_heartbeat_interval is None

    def test_response_getter_returns_inherited_app_default(self):
        options = falcon.ResponseOptions()
        options.sse_heartbeat_interval = 7.5

        resp = falcon.asgi.Response(options=options)
        assert resp.sse_heartbeat_interval == 7.5

        resp.sse_heartbeat_interval = None
        assert resp.sse_heartbeat_interval is None

        del resp.sse_heartbeat_interval
        assert resp.sse_heartbeat_interval == 7.5
