import pytest

import falcon
import falcon.asgi
from falcon import media
from falcon import testing


class CountingReceive:
    """Wrap an ASGI receive callable and count the body bytes delivered."""

    def __init__(self, receive):
        self._receive = receive
        self.events = 0
        self.body_bytes = 0

    async def __call__(self):
        event = await self._receive()
        self.events += 1
        self.body_bytes += len(event.get('body') or b'')
        return event


class BodyReader:
    """Resource that consumes the request body via async iteration."""

    def __init__(self):
        self.called = False
        self.chunks = []

    async def on_post(self, req, resp):
        self.called = True
        async for chunk in req.stream:
            self.chunks.append(chunk)
        resp.media = {'size': sum(len(chunk) for chunk in self.chunks)}


def make_app(resource, limit=16, path='/thing'):
    app = falcon.asgi.App()
    app.req_options.max_request_body_size = limit
    app.add_route(path, resource)
    return app


async def simulate(
    app,
    body=b'',
    chunk_size=None,
    content_length=None,
    headers=None,
    method='POST',
    path='/thing',
):
    scope = testing.create_scope(
        method=method,
        path=path,
        headers=headers,
        content_length=content_length,
    )
    emitter = testing.ASGIRequestEventEmitter(body=body, chunk_size=chunk_size)
    emitter._emit_empty_chunks = False
    receive = CountingReceive(emitter)
    collector = testing.ASGIResponseEventCollector()

    await app(scope, receive, collector)

    return collector, receive


def response_starts(collector):
    return [
        event for event in collector.events if event['type'] == 'http.response.start'
    ]


class TestAppBodyLimit:
    async def test_content_length_over_limit_rejected_before_resource(self):
        resource = BodyReader()
        app = make_app(resource, limit=16)

        collector, _ = await simulate(app, body=b'x' * 17, content_length=17)

        assert collector.status == 413
        assert not resource.called
        assert len(response_starts(collector)) == 1

    async def test_content_length_at_exact_limit_allowed(self):
        resource = BodyReader()
        app = make_app(resource, limit=16)

        collector, _ = await simulate(app, body=b'x' * 16, content_length=16)

        assert collector.status == 200
        assert resource.called
        assert b''.join(resource.chunks) == b'x' * 16

    async def test_limit_disabled_by_default(self):
        resource = BodyReader()
        app = falcon.asgi.App()
        app.add_route('/thing', resource)

        collector, _ = await simulate(app, body=b'x' * 2**20, content_length=2**20)

        assert collector.status == 200
        assert resource.called

    async def test_empty_body(self):
        resource = BodyReader()
        app = make_app(resource, limit=0)

        collector, _ = await simulate(app, body=b'', content_length=0)

        assert collector.status == 200
        assert resource.called
        assert resource.chunks == []

    async def test_chunked_single_byte_chunks_over_limit(self):
        resource = BodyReader()
        app = make_app(resource, limit=5)

        collector, receive = await simulate(app, body=b'x' * 100, chunk_size=1)

        assert collector.status == 413
        assert resource.called
        # The responder only received the bytes up to the limit.
        assert b''.join(resource.chunks) == b'x' * 5
        # The body was not buffered in its entirety before raising; the
        #   (limit + 1)th byte triggered the error.
        assert receive.body_bytes <= 6
        assert len(response_starts(collector)) == 1

    async def test_chunked_single_byte_chunks_at_exact_limit(self):
        resource = BodyReader()
        app = make_app(resource, limit=5)

        collector, receive = await simulate(app, body=b'x' * 5, chunk_size=1)

        assert collector.status == 200
        assert resource.called
        assert b''.join(resource.chunks) == b'x' * 5
        assert receive.body_bytes == 5

    async def test_chunked_large_body_over_limit(self):
        resource = BodyReader()
        app = make_app(resource, limit=100)

        collector, receive = await simulate(app, body=b'x' * 2**20, chunk_size=4096)

        assert collector.status == 413
        assert resource.called
        # Consumption stopped shortly after the limit was exceeded, rather
        #   than buffering the whole 1 MiB body.
        assert receive.body_bytes <= 100 + 4096

    async def test_read_size_over_limit(self):
        class Resource:
            async def on_post(self, req, resp):
                await req.stream.read(64)

        app = make_app(Resource(), limit=16)

        collector, _ = await simulate(app, body=b'x' * 100, chunk_size=10)

        assert collector.status == 413

    async def test_media_cache_does_not_double_count(self):
        captured = {}

        class Resource:
            async def on_post(self, req, resp):
                captured['media'] = await req.get_media()
                captured['again'] = await req.get_media()

        app = make_app(Resource(), limit=16)

        body = b'{"a": "1234567"}'
        assert len(body) == 16
        collector, _ = await simulate(
            app,
            body=body,
            content_length=len(body),
            headers={'Content-Type': 'application/json'},
        )

        assert collector.status == 200
        assert captured['media'] == {'a': '1234567'}
        assert captured['media'] is captured['again']

    async def test_get_media_over_limit(self):
        class Resource:
            async def on_post(self, req, resp):
                await req.get_media()

        app = make_app(Resource(), limit=16)

        body = b'{"a": "' + b'1' * 84 + b'"}'
        collector, receive = await simulate(
            app,
            body=body,
            chunk_size=4,
            headers={'Content-Type': 'application/json'},
        )

        assert collector.status == 413
        # The error was raised as soon as the limit was exceeded, without
        #   first buffering the whole body.
        assert receive.body_bytes <= 20

    async def test_get_media_error_is_stable(self):
        class Resource:
            async def on_post(self, req, resp):
                raises = 0
                for _ in range(2):
                    try:
                        await req.get_media()
                    except falcon.HTTPContentTooLarge:
                        raises += 1
                resp.media = {'raises': raises}

        app = make_app(Resource(), limit=5)

        collector, _ = await simulate(
            app,
            body=b'{"key": "value"}',
            chunk_size=4,
            headers={'Content-Type': 'application/json'},
        )

        assert collector.status == 200
        assert b'"raises": 2' in b''.join(collector.body_chunks)

    async def test_multipart_over_limit(self):
        class Resource:
            async def on_post(self, req, resp):
                form = await req.get_media()
                async for part in form:
                    await part.stream.read()

        app = make_app(Resource(), limit=20)

        boundary = 'BOUNDARY'
        body = (
            '--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="field1"\r\n\r\n'
            + 'v' * 100
            + '\r\n--'
            + boundary
            + '--\r\n'
        )
        collector, _ = await simulate(
            app,
            body=body.encode(),
            chunk_size=16,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary},
        )

        assert collector.status == 413

    async def test_multipart_own_limits_still_apply(self):
        class Resource:
            async def on_post(self, req, resp):
                form = await req.get_media()
                async for part in form:
                    await part.stream.read()

        app = make_app(Resource(), limit=2**20)
        multipart_handler = media.MultipartFormHandler()
        multipart_handler.parse_options.max_body_part_count = 1
        app.req_options.media_handlers[falcon.MEDIA_MULTIPART] = multipart_handler

        boundary = 'BOUNDARY'
        part = (
            '--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="{}"\r\n\r\n'
            'value\r\n'
        )
        body = (
            part.format('field1') + part.format('field2') + '--' + boundary + '--\r\n'
        )
        collector, _ = await simulate(
            app,
            body=body.encode(),
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary},
        )

        assert collector.status == 400
        assert b'maximum number of form body parts exceeded' in b''.join(
            collector.body_chunks
        )

    async def test_disconnect_mid_body(self):
        class Resource:
            async def on_post(self, req, resp):
                data = await req.stream.read()
                resp.media = {'size': len(data)}

        app = make_app(Resource(), limit=1000)

        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 10, chunk_size=5)
        emitter._emit_empty_chunks = False
        original_emit = emitter.emit
        event_count = 0

        async def emit_then_disconnect():
            nonlocal event_count
            event_count += 1
            if event_count > 2:
                return {'type': 'http.disconnect'}
            return await original_emit()

        scope = testing.create_scope(method='POST', path='/thing')
        collector = testing.ASGIResponseEventCollector()
        await app(scope, emit_then_disconnect, collector)

        assert collector.status == 200
        assert b'"size": 10' in b''.join(collector.body_chunks)

    async def test_disconnect_before_limit_not_reported_as_too_large(self):
        class Resource:
            async def on_post(self, req, resp):
                data = await req.stream.read()
                resp.media = {'size': len(data)}

        app = make_app(Resource(), limit=10)

        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 6, chunk_size=3)
        emitter._emit_empty_chunks = False
        original_emit = emitter.emit
        event_count = 0

        async def emit_then_disconnect():
            nonlocal event_count
            event_count += 1
            if event_count > 2:
                return {'type': 'http.disconnect'}
            return await original_emit()

        scope = testing.create_scope(method='POST', path='/thing')
        collector = testing.ASGIResponseEventCollector()
        await app(scope, emit_then_disconnect, collector)

        assert collector.status == 200
        assert b'"size": 6' in b''.join(collector.body_chunks)

    async def test_custom_error_handler(self):
        resource = BodyReader()
        app = make_app(resource, limit=16)

        async def handle_too_large(req, resp, ex, params):
            resp.status = falcon.HTTP_413
            resp.media = {'error': 'request body too large'}

        app.add_error_handler(falcon.HTTPContentTooLarge, handle_too_large)

        collector, _ = await simulate(app, body=b'x' * 17, content_length=17)

        assert collector.status == 413
        assert not resource.called
        assert b'request body too large' in b''.join(collector.body_chunks)

    async def test_response_already_committed(self):
        class Resource:
            async def on_post(self, req, resp):
                async def stream():
                    yield b'first'
                    # The response has already started at this point; the
                    #   error must terminate the stream instead of producing
                    #   a second response.
                    await req.stream.read()

                resp.stream = stream()

        app = make_app(Resource(), limit=5)

        scope = testing.create_scope(method='POST', path='/thing')
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 100, chunk_size=10)
        emitter._emit_empty_chunks = False
        collector = testing.ASGIResponseEventCollector()

        with pytest.raises(falcon.HTTPContentTooLarge):
            await app(scope, emitter, collector)

        starts = response_starts(collector)
        assert len(starts) == 1
        assert starts[0]['status'] == 200


class TestBoundedStreamLimit:
    async def test_read_exact_limit_then_next_byte_raises(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 10, chunk_size=1)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, content_length=10, max_bytes=5)

        assert await stream.read(5) == b'x' * 5

        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.read(1)

        # The error is stable; further reads keep raising it.
        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.read(1)

    async def test_readall_shares_quota_with_read(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 10, chunk_size=1)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, content_length=10, max_bytes=5)

        assert await stream.read(3) == b'x' * 3

        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.readall()

    async def test_readall_at_exact_limit(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 5, chunk_size=1)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, content_length=5, max_bytes=5)

        assert await stream.readall() == b'x' * 5
        assert stream.tell() == 5

    async def test_readall_over_limit_via_first_event(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 10)
        emitter._emit_empty_chunks = False
        first_event = await emitter.emit()
        stream = falcon.asgi.BoundedStream(
            emitter, first_event=first_event, max_bytes=5
        )

        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.readall()

    async def test_iteration_counts_toward_limit(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 10, chunk_size=1)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, content_length=10, max_bytes=5)

        chunks = []
        with pytest.raises(falcon.HTTPContentTooLarge):
            async for chunk in stream:
                chunks.append(chunk)

        assert b''.join(chunks) == b'x' * 5

    async def test_iteration_at_exact_limit(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 5, chunk_size=1)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, content_length=5, max_bytes=5)

        chunks = [chunk async for chunk in stream]

        assert b''.join(chunks) == b'x' * 5
        assert stream.tell() == 5

    async def test_exhaust_stops_at_limit_without_raising(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 100, chunk_size=10)
        emitter._emit_empty_chunks = False
        receive = CountingReceive(emitter)
        stream = falcon.asgi.BoundedStream(receive, max_bytes=10)

        await stream.exhaust()

        assert receive.body_bytes == 10

        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.read(1)

    async def test_exhaust_after_limit_exceeded_returns_quietly(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 100, chunk_size=10)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, max_bytes=5)

        with pytest.raises(falcon.HTTPContentTooLarge):
            await stream.readall()

        await stream.exhaust()

    async def test_no_limit_by_default(self):
        emitter = testing.ASGIRequestEventEmitter(body=b'x' * 100, chunk_size=10)
        emitter._emit_empty_chunks = False
        stream = falcon.asgi.BoundedStream(emitter, content_length=100)

        assert await stream.readall() == b'x' * 100
