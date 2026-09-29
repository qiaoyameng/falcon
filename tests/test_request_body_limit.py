"""Tests for the RequestOptions.body_max_bytes limit (WSGI)."""

import io
import json

import pytest

import falcon
import falcon.testing as testing
from falcon.stream import BoundedStream


class CountingInputStream:
    """File-like wrapper that counts the bytes actually read."""

    def __init__(self, data):
        self._stream = io.BytesIO(data)
        self.bytes_read = 0

    def read(self, size=-1):
        data = self._stream.read(size)
        self.bytes_read += len(data)
        return data

    def readline(self, size=-1):
        data = self._stream.readline(size)
        self.bytes_read += len(data)
        return data

    def readlines(self, hint=-1):
        lines = self._stream.readlines(hint)
        self.bytes_read += sum(len(line) for line in lines)
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        line = self._stream.readline()
        if not line:
            raise StopIteration
        self.bytes_read += len(line)
        return line


class ReadingResource:
    """Records whether the responder ran and what it read."""

    def __init__(self):
        self.ran = False
        self.body = None

    def on_post(self, req, resp):
        self.ran = True
        self.body = req.bounded_stream.read()
        resp.media = {'size': len(self.body)}


def make_app(limit, resource=None):
    app = falcon.App()
    app.req_options.body_max_bytes = limit
    if resource is not None:
        app.add_route('/', resource)
    return app


def simulate(app, env):
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured['status'] = status
        captured['headers'] = headers

    body = b''.join(app(env, start_response))
    return captured['status'], body


def make_env(body, content_length='__default__', content_type=None):
    headers = {}
    if content_type is not None:
        headers['Content-Type'] = content_type
    env = testing.create_environ(method='POST', body=body, headers=headers)
    if content_length == '__default__':
        # NOTE: create_environ() already sets CONTENT_LENGTH to match.
        pass
    elif content_length is None:
        del env['CONTENT_LENGTH']
    else:
        env['CONTENT_LENGTH'] = str(content_length)
    return env


class TestBodyMaxBytesOption:
    @pytest.mark.parametrize('value', [None, 0, 1, 2**63])
    def test_valid_values(self, value):
        options = falcon.RequestOptions()
        options.body_max_bytes = value
        assert options.body_max_bytes == value

    @pytest.mark.parametrize('value', [-1, -100, 1.5, '1024', True, object()])
    def test_invalid_values(self, value):
        options = falcon.RequestOptions()
        with pytest.raises(ValueError):
            options.body_max_bytes = value

    def test_default_is_disabled(self):
        assert falcon.RequestOptions().body_max_bytes is None

    def test_disabled_limit_allows_large_body(self):
        resource = ReadingResource()
        app = make_app(None, resource)
        client = testing.TestClient(app)

        result = client.simulate_post('/', body='x' * 2**20)

        assert result.status == falcon.HTTP_200
        assert result.json == {'size': 2**20}


class TestWSGIContentLengthCheck:
    def test_declared_length_exceeds_limit(self):
        resource = ReadingResource()
        app = make_app(10, resource)
        stream = CountingInputStream(b'x' * 100)

        env = make_env(b'x' * 100)
        env['wsgi.input'] = stream
        status, body = simulate(app, env)

        assert status == falcon.HTTP_413
        assert b'exceeds the configured limit of 10 bytes' in body
        # NOTE: The responder must not run, and the request body must
        #   not be consumed at all.
        assert not resource.ran
        assert stream.bytes_read == 0

    def test_exact_limit_allowed(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        status, body = simulate(app, make_env(b'x' * 10))

        assert status == falcon.HTTP_200
        assert resource.body == b'x' * 10

    def test_one_byte_over_limit(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        status, body = simulate(app, make_env(b'x' * 11))

        assert status == falcon.HTTP_413
        assert not resource.ran

    def test_empty_body_with_zero_limit(self):
        resource = ReadingResource()
        app = make_app(0, resource)

        status, body = simulate(app, make_env(b''))

        assert status == falcon.HTTP_200
        assert resource.ran

    def test_zero_limit_rejects_any_body(self):
        resource = ReadingResource()
        app = make_app(0, resource)

        status, body = simulate(app, make_env(b'x'))

        assert status == falcon.HTTP_413
        assert not resource.ran

    def test_custom_error_handler(self):
        resource = ReadingResource()
        app = make_app(10, resource)

        def handle_error(req, resp, ex, params):
            resp.status = falcon.HTTP_413
            resp.media = {'custom': True}

        app.add_error_handler(falcon.HTTPContentTooLarge, handle_error)
        client = testing.TestClient(app)

        result = client.simulate_post('/', body='x' * 11)

        assert result.status == falcon.HTTP_413
        assert result.json == {'custom': True}
        assert not resource.ran


class TestWSGIBoundedStreamLimit:
    def test_default_no_limit(self):
        stream = BoundedStream(io.BytesIO(b'x' * 100), 100)
        assert stream.max_bytes is None
        assert stream.read() == b'x' * 100

    def test_read_up_to_limit_then_error(self):
        raw = CountingInputStream(b'x' * 15)
        stream = BoundedStream(raw, 15, max_bytes=10)

        assert stream.read(10) == b'x' * 10

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(1)

        # NOTE: Once the limit has been exceeded, the stream stops
        #   consuming data and behaves as exhausted.
        assert stream.eof
        assert stream.read(1) == b''
        assert raw.bytes_read == 10

    def test_read_past_limit_raises_before_reading(self):
        raw = CountingInputStream(b'x' * 15)
        stream = BoundedStream(raw, 15, max_bytes=10)

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(11)

        # NOTE: The oversized body must not be buffered before the
        #   check takes place.
        assert raw.bytes_read == 0

    def test_exact_limit_reads_to_eof_cleanly(self):
        raw = CountingInputStream(b'x' * 10)
        stream = BoundedStream(raw, 10, max_bytes=10)

        assert stream.read() == b'x' * 10
        assert stream.read() == b''

    def test_iteration_enforces_limit(self):
        raw = CountingInputStream(b'aa\nbb\ncc\n')
        stream = BoundedStream(raw, 9, max_bytes=6)

        assert next(stream) == b'aa\n'
        assert next(stream) == b'bb\n'
        with pytest.raises(falcon.HTTPContentTooLarge):
            next(stream)

    def test_iteration_exact_limit_stops_cleanly(self):
        stream = BoundedStream(io.BytesIO(b'aa\nbb\n'), 6, max_bytes=6)

        assert list(stream) == [b'aa\n', b'bb\n']

    def test_exhaust_enforces_limit(self):
        raw = CountingInputStream(b'x' * 100)
        stream = BoundedStream(raw, 100, max_bytes=64)

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.exhaust(chunk_size=32)

        assert raw.bytes_read == 64


class TestWSGIAppStreamEnforcement:
    def test_iteration_over_unknown_length_body(self):
        # NOTE: When no Content-Length is declared, the bounded stream
        #   may not be read directly (existing safe boundary), but
        #   iteration is still capped by the configured limit.
        class IteratingResource:
            def __init__(self):
                self.lines = []

            def on_post(self, req, resp):
                for line in req.bounded_stream:
                    self.lines.append(line)

        resource = IteratingResource()
        app = make_app(6, resource)

        env = make_env(b'aa\nbb\ncc\n', content_length=None)
        env['wsgi.input'] = CountingInputStream(b'aa\nbb\ncc\n')
        status, body = simulate(app, env)

        assert status == falcon.HTTP_413
        assert resource.lines == [b'aa\n', b'bb\n']

    def test_unknown_length_read_boundary_preserved(self):
        # NOTE: Without a reliable Content-Length, the bounded stream
        #   must not probe the input stream; the limit does not change
        #   that existing safety boundary.
        resource = ReadingResource()
        app = make_app(100, resource)

        stream = CountingInputStream(b'x' * 50)
        env = make_env(b'x' * 50, content_length=None)
        env['wsgi.input'] = stream
        status, body = simulate(app, env)

        assert status == falcon.HTTP_200
        assert resource.body == b''
        assert stream.bytes_read == 0

    def test_shared_budget_across_entry_points(self):
        # NOTE: Bytes consumed via iteration and read() share a single
        #   budget; switching entry points does not reset it.
        class MixedResource:
            def on_post(self, req, resp):
                self.first = next(req.bounded_stream)
                self.second = req.bounded_stream.read(3)
                # NOTE: The body is exactly at the limit now, so the
                #   next byte must trigger the error.
                req.bounded_stream.read(1)

        resource = MixedResource()
        app = make_app(6, resource)

        # NOTE: The raw stream actually carries more data than the
        #   declared Content-Length.
        env = make_env(b'aa\nbb\ncc\n', content_length=6)
        env['wsgi.input'] = CountingInputStream(b'aa\nbb\ncc\n')
        status, body = simulate(app, env)

        assert status == falcon.HTTP_413
        assert resource.first == b'aa\n'
        assert resource.second == b'bb\n'


class TestWSGIMediaAndMultipart:
    def test_media_cache_not_double_counted(self):
        doc = {'a': 'bcdefg'}
        body = json.dumps(doc).encode()

        class MediaResource:
            def on_post(self, req, resp):
                self.first = req.get_media()
                self.second = req.get_media()
                self.tail = req.bounded_stream.read()
                resp.media = {'ok': True}

        resource = MediaResource()
        app = make_app(len(body), resource)

        status, _ = simulate(app, make_env(body))

        assert status == falcon.HTTP_200
        assert resource.first == doc
        assert resource.second is resource.first
        assert resource.tail == b''

    def test_stream_read_then_media_shares_budget(self):
        # NOTE: Reading part of the body via the stream and the rest
        #   via get_media() draws on a single shared budget (and stream
        #   position); the media handler only sees the remaining bytes,
        #   which no longer form a valid JSON document.
        doc = {'a': 'bcdefg'}
        body = json.dumps(doc).encode()

        class MediaResource:
            def on_post(self, req, resp):
                self.head = req.bounded_stream.read(5)
                req.get_media()

        resource = MediaResource()
        app = make_app(len(body), resource)

        status, _ = simulate(app, make_env(body))

        assert status == falcon.HTTP_400
        assert resource.head == body[:5]

    def test_media_over_limit_rejected_up_front(self):
        class MediaResource:
            def __init__(self):
                self.ran = False

            def on_post(self, req, resp):
                self.ran = True
                req.get_media()

        resource = MediaResource()
        app = make_app(4, resource)

        body = b'{"a": 1}'
        env = make_env(body, content_type='application/json')
        status, _ = simulate(app, env)

        assert status == falcon.HTTP_413
        assert not resource.ran

    @staticmethod
    def _multipart_body(payload_size):
        return (
            b'--boundary\r\n'
            b'Content-Disposition: form-data; name="field"\r\n'
            b'\r\n' + b'x' * payload_size + b'\r\n'
            b'--boundary--\r\n'
        )

    def test_multipart_over_limit(self):
        class FormResource:
            def __init__(self):
                self.ran = False

            def on_post(self, req, resp):
                self.ran = True
                form = req.get_media()
                for part in form:
                    part.stream.read()

        resource = FormResource()
        app = make_app(10, resource)

        body = self._multipart_body(100)
        env = make_env(
            body, content_type='multipart/form-data; boundary=boundary'
        )
        status, _ = simulate(app, env)

        assert status == falcon.HTTP_413
        assert not resource.ran

    def test_multipart_within_limit(self):
        class FormResource:
            def on_post(self, req, resp):
                form = req.get_media()
                self.parts = {part.name: part.stream.read() for part in form}
                resp.media = {'ok': True}

        resource = FormResource()
        body = self._multipart_body(8)
        app = make_app(len(body), resource)

        env = make_env(
            body, content_type='multipart/form-data; boundary=boundary'
        )
        status, _ = simulate(app, env)

        assert status == falcon.HTTP_200
        assert resource.parts == {'field': b'x' * 8}

    def test_multipart_own_limits_still_apply(self):
        class FormResource:
            def on_post(self, req, resp):
                form = req.get_media()
                for part in form:
                    part.stream.read()

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
        env = make_env(
            body, content_type='multipart/form-data; boundary=boundary'
        )
        status, _ = simulate(app, env)

        # NOTE: The parser's own part-count limit is independent of the
        #   request body byte limit.
        assert status == falcon.HTTP_400
