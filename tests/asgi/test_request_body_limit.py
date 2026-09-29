"""Tests for the RequestOptions.body_max_bytes limit (ASGI)."""

import json

import pytest

import falcon
import falcon.asgi
import falcon.testing as testing
from falcon.asgi.stream import BoundedStream


class ReceiveTracker:
    """Wrap an emitter, counting request events and body bytes consumed."""

    def __init__(self, emitter):
        self._emitter = emitter
        self.request_events = 0
        self.bytes_received = 0

    async def __call__(self):
        event = await self._emitter.emit()
        if event['type'] == 'http.request':
            self.request_events += 1
            self.bytes_received += len(event.get('body', b''))
        return event


class ReadingResource:
    """Records whether the responder ran and what it read."""

    def __init__(self):
        self.ran = False
        self.body = None

    async def on_post(self, req, resp):
        self.ran = True
        self.body = await req.stream.readall()
        resp.media = {'size': len(self.body)}


def make_app(limit, resource=None):
    app = falcon.asgi.App()
    app.req_options.body_max_bytes = limit
    if resource is not None:
        app.add_route('/', resource)
    return app


def make_emitter(body, chunk_size=4096, disconnect_at=None):
    emitter = testing.ASGIRequestEventEmitter(
        body, chunk_size=chunk_size, disconnect_at=disconnect_at
    )
    emitter._emit_empty_chunks = False
    return emitter


async def simulate(app, emitter, content_length=None):
    scope = testing.create_scope(method='POST', content_length=content_length)
    tracker = ReceiveTracker(emitter)
    collector = testing.ASGIResponseEventCollector()
    await app(scope, tracker, collector)
    return tracker, collector


def response_start_events(collector):
    return [e for e in collector.events if e['type'] == 'http.response.start']


class TestASGIContentLengthCheck:
    async def test_declared_length_exceeds_limit(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 100)
        tracker, collector = await simulate(app, emitter, content_length=100)

        assert collector.status == 413
        assert b'exceeds the configured limit of 10 bytes' in b''.join(
            collector.body_chunks
        )
        # NOTE: The responder must not run, and no body events beyond
        #   the mandatory first one may be consumed.
        assert not resource.ran
        assert tracker.request_events == 1
        assert len(response_start_events(collector)) == 1

    async def test_exact_limit_allowed(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 10)
        tracker, collector = await simulate(app, emitter, content_length=10)

        assert collector.status == 200
        assert resource.body == b'x' * 10

    async def test_empty_body_with_zero_limit(self):
        resource = ReadingResource()
        app = make_app(0, resource)

        emitter = make_emitter(b'')
        tracker, collector = await simulate(app, emitter, content_length=0)

        assert collector.status == 200
        assert resource.ran

    async def test_zero_limit_rejects_declared_body(self):
        resource = ReadingResource()
        app = make_app(0, resource)

        emitter = make_emitter(b'x')
        tracker, collector = await simulate(app, emitter, content_length=1)

        assert collector.status == 413
        assert not resource.ran

    async def test_custom_error_handler(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        async def handle_error(req, resp, ex, params):
            resp.status = falcon.HTTP_413
            resp.media = {'custom': True}

        app.add_error_handler(falcon.HTTPContentTooLarge, handle_error)

        emitter = make_emitter(b'x' * 11)
        tracker, collector = await simulate(app, emitter, content_length=11)

        assert collector.status == 413
        assert json.loads(b''.join(collector.body_chunks)) == {'custom': True}
        assert not resource.ran


class TestASGIChunkedEnforcement:
    async def test_single_byte_chunks(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        # NOTE: No Content-Length is declared, so the limit must be
        #   enforced based on the actual bytes consumed.
        emitter = make_emitter(b'x' * 20, chunk_size=1)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413
        assert resource.ran
        # NOTE: Consumption stops as soon as the next byte would exceed
        #   the limit; the remaining body is never pulled from the
        #   receive channel.
        assert tracker.bytes_received == 11
        assert len(response_start_events(collector)) == 1

    async def test_large_chunk_overshoot(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 2**16, chunk_size=2**16)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413
        assert tracker.bytes_received == 2**16

    async def test_chunked_exact_limit(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 10, chunk_size=3)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 200
        assert resource.body == b'x' * 10

    async def test_chunked_one_byte_over(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 11, chunk_size=1)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413
        assert tracker.bytes_received == 11

    async def test_read_with_size(self):
        class ChunkReadingResource:
            def __init__(self):
                self.chunks = []

            async def on_post(self, req, resp):
                while True:
                    chunk = await req.stream.read(1)
                    if not chunk:
                        break
                    self.chunks.append(chunk)

        resource = ChunkReadingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 20, chunk_size=4)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413
        # NOTE: The budget tracks bytes consumed from the receive
        #   channel; the third 4-byte event pushes the total to 12,
        #   which exceeds the limit of 10.
        assert b''.join(resource.chunks) == b'x' * 8
        assert tracker.bytes_received == 12

    async def test_async_iteration(self):
        class IteratingResource:
            def __init__(self):
                self.chunks = []

            async def on_post(self, req, resp):
                async for chunk in req.stream:
                    self.chunks.append(chunk)

        resource = IteratingResource()
        app = make_app(10, resource)

        emitter = make_emitter(b'x' * 20, chunk_size=4)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413
        # NOTE: The third 4-byte chunk exceeds the limit and is
        #   therefore never yielded.
        assert b''.join(resource.chunks) == b'x' * 8
        assert tracker.bytes_received == 12

    async def test_exhaust_enforces_limit(self):
        class ExhaustingResource:
            async def on_post(self, req, resp):
                await req.stream.exhaust()

        app = make_app(10, ExhaustingResource())

        emitter = make_emitter(b'x' * 100, chunk_size=8)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413
        assert tracker.bytes_received == 16

    async def test_disconnect_before_limit(self):
        events = iter(
            [
                {'type': 'http.request', 'body': b'hello', 'more_body': True},
                {'type': 'http.disconnect'},
            ]
        )

        async def receive():
            return next(events)

        resource = ReadingResource()
        app = make_app(10, resource)

        scope = testing.create_scope(method='POST')
        collector = testing.ASGIResponseEventCollector()
        await app(scope, receive, collector)

        # NOTE: A client disconnect simply ends the stream; it is not
        #   an error, and certainly not a 413.
        assert collector.status == 200
        assert resource.body == b'hello'

    async def test_response_already_committed(self):
        # NOTE: When the limit is exceeded while rendering a streaming
        #   response (i.e. after the response start event was sent), the
        #   error aborts the response; a second response start event
        #   must not be emitted.
        class StreamingResource:
            async def on_post(self, req, resp):
                async def content():
                    yield b'partial'
                    await req.stream.readall()
                    yield b'unreachable'

                resp.stream = content()

        app = make_app(10, StreamingResource())

        emitter = make_emitter(b'x' * 20, chunk_size=4)
        scope = testing.create_scope(method='POST')
        collector = testing.ASGIResponseEventCollector()

        with pytest.raises(falcon.HTTPContentTooLarge):
            await app(scope, emitter, collector)

        starts = response_start_events(collector)
        assert len(starts) == 1
        assert starts[0]['status'] == 200
        assert collector.body_chunks == [b'partial']


class TestASGIBoundedStreamLimit:
    async def test_first_event_exceeds_limit(self):
        emitter = make_emitter(b'x' * 100)
        first_event = await emitter.emit()

        with pytest.raises(falcon.HTTPContentTooLarge):
            BoundedStream(emitter, first_event=first_event, max_bytes=10)

    async def test_first_event_within_limit(self):
        emitter = make_emitter(b'x' * 10)
        first_event = await emitter.emit()
        stream = BoundedStream(emitter, first_event=first_event, max_bytes=10)

        assert await stream.readall() == b'x' * 10

    async def test_readall_after_limit_reports_eof(self):
        emitter = make_emitter(b'x' * 20, chunk_size=1)
        first_event = await emitter.emit()
        stream = BoundedStream(emitter, first_event=first_event, max_bytes=10)

        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.readall()

        assert stream.eof
        assert await stream.read() == b''

    async def test_oversized_event_truncated_by_content_length(self):
        # NOTE: When the server sends more data than the declared
        #   Content-Length, the stream is truncated; only the truncated
        #   bytes count towards the limit.
        emitter = make_emitter(b'x' * 8, chunk_size=4)
        first_event = await emitter.emit()
        stream = BoundedStream(
            emitter, first_event=first_event, content_length=5, max_bytes=10
        )

        assert await stream.readall() == b'x' * 5

    async def test_oversized_event_truncated_read_with_size(self):
        emitter = make_emitter(b'x' * 8, chunk_size=4)
        first_event = await emitter.emit()
        stream = BoundedStream(
            emitter, first_event=first_event, content_length=5, max_bytes=10
        )

        assert await stream.read(4) == b'x' * 4
        assert await stream.read(4) == b'x'

    async def test_oversized_event_truncated_iteration(self):
        emitter = make_emitter(b'x' * 8, chunk_size=4)
        first_event = await emitter.emit()
        stream = BoundedStream(
            emitter, first_event=first_event, content_length=5, max_bytes=10
        )

        chunks = [chunk async for chunk in stream]
        assert b''.join(chunks) == b'x' * 5


class TestASGIMediaAndMultipart:
    async def test_media_cache_not_double_counted(self):
        doc = {'a': 'bcdefg'}
        body = json.dumps(doc).encode()

        class MediaResource:
            async def on_post(self, req, resp):
                self.first = await req.get_media()
                self.second = await req.get_media()
                self.tail = await req.stream.read()
                resp.media = {'ok': True}

        resource = MediaResource()
        app = make_app(len(body), resource)

        emitter = make_emitter(body, chunk_size=4)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 200
        assert resource.first == doc
        assert resource.second is resource.first
        assert resource.tail == b''

    async def test_media_over_limit_chunked(self):
        doc = {'a': 'bcdefg'}
        body = json.dumps(doc).encode()

        class MediaResource:
            async def on_post(self, req, resp):
                await req.get_media()

        app = make_app(len(body) - 1, MediaResource())

        emitter = make_emitter(body, chunk_size=3)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 413

    async def test_media_error_is_cached(self):
        doc = {'a': 'bcdefg'}
        body = json.dumps(doc).encode()

        class MediaResource:
            async def on_post(self, req, resp):
                for _ in range(2):
                    with pytest.raises(falcon.HTTPContentTooLarge):
                        await req.get_media()

        app = make_app(len(body) - 1, MediaResource())

        emitter = make_emitter(body, chunk_size=3)
        tracker, collector = await simulate(app, emitter)

        assert collector.status == 200

    @staticmethod
    def _multipart_body(payload_size):
        return (
            b'--boundary\r\n'
            b'Content-Disposition: form-data; name="field"\r\n'
            b'\r\n' + b'x' * payload_size + b'\r\n'
            b'--boundary--\r\n'
        )

    @staticmethod
    def _multipart_scope(body):
        return testing.create_scope(
            method='POST',
            headers={'Content-Type': 'multipart/form-data; boundary=boundary'},
        )

    async def test_multipart_over_limit_chunked(self):
        class FormResource:
            async def on_post(self, req, resp):
                form = await req.get_media()
                async for part in form:
                    await part.stream.read()

        app = make_app(100, FormResource())

        body = self._multipart_body(200)
        emitter = make_emitter(body, chunk_size=16)
        scope = self._multipart_scope(body)
        tracker = ReceiveTracker(emitter)
        collector = testing.ASGIResponseEventCollector()
        await app(scope, tracker, collector)

        assert collector.status == 413

    async def test_multipart_within_limit(self):
        class FormResource:
            async def on_post(self, req, resp):
                form = await req.get_media()
                self.parts = {}
                async for part in form:
                    self.parts[part.name] = await part.stream.read()
                resp.media = {'ok': True}

        resource = FormResource()
        body = self._multipart_body(8)
        app = make_app(len(body), resource)

        emitter = make_emitter(body, chunk_size=16)
        scope = self._multipart_scope(body)
        tracker = ReceiveTracker(emitter)
        collector = testing.ASGIResponseEventCollector()
        await app(scope, tracker, collector)

        assert collector.status == 200
        assert resource.parts == {'field': b'x' * 8}

    async def test_multipart_own_limits_still_apply(self):
        class FormResource:
            async def on_post(self, req, resp):
                form = await req.get_media()
                async for part in form:
                    await part.stream.read()

        app = make_app(2**20, FormResource())
        handler = app.req_options.media_handlers[falcon.MEDIA_MULTIPART]
        handler.parse_options.max_body_part_count = 1

        body = (
            b'--boundary\r\n'
            b'Content-Disposition: form-data; name="a"\r\n\r\n1\r\n'
            b'--boundary\r\n'
            b'Content-Disposition: form-data; name="b"\r\n\r\n2\r\n'
            b'--boundary--\r\n'
        )
        emitter = make_emitter(body)
        scope = self._multipart_scope(body)
        tracker = ReceiveTracker(emitter)
        collector = testing.ASGIResponseEventCollector()
        await app(scope, tracker, collector)

        # NOTE: The parser's own part-count limit is independent of the
        #   request body byte limit.
        assert collector.status == 400
