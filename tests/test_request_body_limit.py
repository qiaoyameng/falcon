import io

import pytest

import falcon
from falcon import media
from falcon import testing
from falcon.stream import BoundedStream


class TestMaxRequestBodySizeOption:
    def test_default_is_disabled(self):
        assert falcon.RequestOptions().max_request_body_size is None

    @pytest.mark.parametrize('value', [0, 1, 100, 2**63])
    def test_valid_values(self, value):
        options = falcon.RequestOptions()
        options.max_request_body_size = value
        assert options.max_request_body_size == value

    def test_reset_to_none(self):
        options = falcon.RequestOptions()
        options.max_request_body_size = 100
        options.max_request_body_size = None
        assert options.max_request_body_size is None

    @pytest.mark.parametrize('value', [-1, -2**63, '100', 1.5, True, False, b'1'])
    def test_invalid_values(self, value):
        options = falcon.RequestOptions()
        with pytest.raises(ValueError):
            options.max_request_body_size = value


class BodyResource:
    def __init__(self):
        self.called = False
        self.captured_body = None

    def on_post(self, req, resp):
        self.called = True
        self.captured_body = req.bounded_stream.read()
        resp.media = {'size': len(self.captured_body)}


@pytest.fixture
def resource():
    return BodyResource()


@pytest.fixture
def client(resource):
    app = falcon.App()
    app.req_options.max_request_body_size = 16
    app.add_route('/thing', resource)
    return testing.TestClient(app)


class TestWSGIAppBodyLimit:
    def test_content_length_over_limit_rejected_before_resource(
        self, client, resource
    ):
        result = client.simulate_post('/thing', body=b'x' * 17)

        assert result.status_code == 413
        assert not resource.called

    def test_body_at_exact_limit_allowed(self, client, resource):
        result = client.simulate_post('/thing', body=b'x' * 16)

        assert result.status_code == 200
        assert resource.called
        assert resource.captured_body == b'x' * 16

    def test_limit_disabled_by_default(self, resource):
        app = falcon.App()
        app.add_route('/thing', resource)
        client = testing.TestClient(app)

        result = client.simulate_post('/thing', body=b'x' * 2**20)

        assert result.status_code == 200
        assert len(resource.captured_body) == 2**20

    def test_empty_body_with_zero_limit(self, resource):
        app = falcon.App()
        app.req_options.max_request_body_size = 0
        app.add_route('/thing', resource)
        client = testing.TestClient(app)

        result = client.simulate_post('/thing', body=b'')

        assert result.status_code == 200
        assert resource.called
        assert resource.captured_body == b''

    def test_non_empty_body_with_zero_limit(self, client, resource):
        client.app.req_options.max_request_body_size = 0

        result = client.simulate_post('/thing', body=b'x')

        assert result.status_code == 413
        assert not resource.called

    def test_missing_content_length(self, resource):
        # NOTE: Without a reliable Content-Length, the bounded stream
        #   retains its existing safe behavior of not reading the input
        #   at all, rather than reading unboundedly to probe the length.
        app = falcon.App()
        app.req_options.max_request_body_size = 16
        app.add_route('/thing', resource)

        env = testing.create_environ(method='POST', path='/thing')
        env['wsgi.input'] = io.BytesIO(b'x' * 100)

        srmock = testing.StartResponseMock()
        b''.join(app(env, srmock))

        assert srmock.status == falcon.HTTP_200
        assert resource.called
        assert resource.captured_body == b''

    def test_custom_error_handler(self, client, resource):
        def handle_too_large(req, resp, ex, params):
            resp.status = falcon.HTTP_413
            resp.media = {'error': 'request body too large'}

        client.app.add_error_handler(falcon.HTTPContentTooLarge, handle_too_large)

        result = client.simulate_post('/thing', body=b'x' * 17)

        assert result.status_code == 413
        assert result.json == {'error': 'request body too large'}
        assert not resource.called

    def test_raw_stream_entry_point_shares_quota(self, client, resource):
        class RawStreamResource:
            def on_post(self, req, resp):
                resp.media = {
                    'wrapped': isinstance(req.stream, BoundedStream),
                    'size': len(req.stream.read()),
                }

        client.app.add_route('/raw', RawStreamResource())

        result = client.simulate_post('/raw', body=b'x' * 16)

        assert result.status_code == 200
        assert result.json == {'wrapped': True, 'size': 16}

    def test_stream_not_wrapped_when_limit_disabled(self, resource):
        captured_stream = None

        class StreamResource:
            def on_post(self, req, resp):
                nonlocal captured_stream
                captured_stream = req.stream

        app = falcon.App()
        app.add_route('/thing', StreamResource())
        client = testing.TestClient(app)
        client.simulate_post('/thing', body=b'x' * 100)

        assert not isinstance(captured_stream, BoundedStream)

    def test_media_cache_does_not_double_count(self, client, resource):
        captured = {}

        class MediaResource:
            def on_post(self, req, resp):
                captured['media'] = req.get_media()
                captured['again'] = req.get_media()

        client.app.add_route('/media', MediaResource())

        body = b'{"a": "1234567"}'
        assert len(body) == 16
        result = client.simulate_post(
            '/media', body=body, headers={'Content-Type': 'application/json'}
        )

        assert result.status_code == 200
        assert captured['media'] == {'a': '1234567'}
        assert captured['media'] is captured['again']

    def test_get_media_over_limit(self, client, resource):
        class MediaResource:
            def on_post(self, req, resp):
                req.get_media()

        client.app.add_route('/media', MediaResource())

        result = client.simulate_post(
            '/media',
            body=b'{"a": "1234567890"}',
            headers={'Content-Type': 'application/json'},
        )

        assert result.status_code == 413

    def test_multipart_over_limit(self, client, resource):
        class FormResource:
            def on_post(self, req, resp):
                form = req.get_media()
                for part in form:
                    part.stream.read()

        client.app.add_route('/form', FormResource())

        boundary = 'BOUNDARY'
        body = (
            '--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="field1"\r\n\r\n'
            + 'v' * 100
            + '\r\n--'
            + boundary
            + '--\r\n'
        )
        result = client.simulate_post(
            '/form',
            body=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary},
        )

        assert result.status_code == 413
        assert not resource.called

    def test_multipart_own_limits_still_apply(self, client, resource):
        class FormResource:
            def on_post(self, req, resp):
                form = req.get_media()
                for part in form:
                    part.stream.read()

        multipart_handler = media.MultipartFormHandler()
        multipart_handler.parse_options.max_body_part_count = 1
        client.app.req_options.media_handlers[falcon.MEDIA_MULTIPART] = (
            multipart_handler
        )
        client.app.req_options.max_request_body_size = 2**20
        client.app.add_route('/form', FormResource())

        boundary = 'BOUNDARY'
        part = (
            '--' + boundary + '\r\n'
            'Content-Disposition: form-data; name="{}"\r\n\r\n'
            'value\r\n'
        )
        body = (
            part.format('field1') + part.format('field2') + '--' + boundary + '--\r\n'
        )
        result = client.simulate_post(
            '/form',
            body=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary},
        )

        assert result.status_code == 400
        assert (
            result.json['description'] == 'maximum number of form body parts exceeded'
        )


class TestBoundedStreamLimit:
    def test_read_exact_limit_then_next_byte_raises(self):
        stream = BoundedStream(io.BytesIO(b'x' * 20), 20, max_bytes=10)

        assert stream.read(10) == b'x' * 10

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(1)

        # The error is stable; further reads keep raising it.
        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(1)

    def test_single_read_beyond_limit_raises(self):
        stream = BoundedStream(io.BytesIO(b'x' * 20), 20, max_bytes=10)

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(11)

    def test_read_at_eof_does_not_raise(self):
        stream = BoundedStream(io.BytesIO(b'x' * 10), 10, max_bytes=10)

        assert stream.read(10) == b'x' * 10
        assert stream.read() == b''
        assert stream.eof

    def test_readall_within_limit(self):
        stream = BoundedStream(io.BytesIO(b'x' * 10), 10, max_bytes=10)

        assert stream.read() == b'x' * 10

    def test_reads_share_quota(self):
        stream = BoundedStream(io.BytesIO(b'x' * 20), 20, max_bytes=10)

        assert stream.read(4) == b'x' * 4
        assert stream.read(6) == b'x' * 6

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(1)

    def test_readline_counts_toward_limit(self):
        stream = BoundedStream(io.BytesIO(b'aa\nbb\ncc\n'), 9, max_bytes=6)

        assert stream.readline(3) == b'aa\n'
        assert stream.readline(3) == b'bb\n'

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.readline(1)

    def test_iteration_counts_toward_limit(self):
        stream = BoundedStream(io.BytesIO(b'aa\nbb\ncc\n'), 9, max_bytes=6)

        chunks = []
        with pytest.raises(falcon.HTTPContentTooLarge):
            for chunk in stream:
                chunks.append(chunk)

        assert b''.join(chunks) == b'aa\nbb\n'

    def test_iteration_without_limit(self):
        stream = BoundedStream(io.BytesIO(b'aa\nbb\n'), 6)

        assert list(stream) == [b'aa\n', b'bb\n']

    def test_iteration_and_reads_share_quota(self):
        stream = BoundedStream(io.BytesIO(b'aa\nbb\ncc\n'), 9, max_bytes=6)

        assert stream.read(3) == b'aa\n'

        chunks = []
        with pytest.raises(falcon.HTTPContentTooLarge):
            for chunk in stream:
                chunks.append(chunk)

        assert b''.join(chunks) == b'bb\n'

    def test_exhaust_stops_at_limit_without_raising(self):
        stream = BoundedStream(io.BytesIO(b'x' * 100), 100, max_bytes=10)

        stream.exhaust()

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(1)

    def test_exhaust_after_partial_read(self):
        stream = BoundedStream(io.BytesIO(b'x' * 100), 100, max_bytes=10)

        assert stream.read(10) == b'x' * 10
        stream.exhaust()

        with pytest.raises(falcon.HTTPContentTooLarge):
            stream.read(1)

    def test_exhaust_within_limit(self):
        stream = BoundedStream(io.BytesIO(b'x' * 10), 10, max_bytes=10)

        stream.exhaust()

        assert stream.eof

    def test_no_limit_by_default(self):
        stream = BoundedStream(io.BytesIO(b'x' * 100), 100)

        assert stream.read() == b'x' * 100


class TestRequestStreamWrapping:
    def test_stream_wrapped_when_limit_enabled(self):
        options = falcon.RequestOptions()
        options.max_request_body_size = 10
        req = falcon.Request(testing.create_environ(body=b'x' * 10), options=options)

        assert req.stream is req.bounded_stream
        assert req.stream.read() == b'x' * 10

    def test_get_media_error_is_stable(self):
        options = falcon.RequestOptions()
        options.max_request_body_size = 10
        env = testing.create_environ(
            body=b'{"a": "1234567890"}',
            headers={'Content-Type': 'application/json'},
        )
        req = falcon.Request(env, options=options)

        for _ in range(2):
            with pytest.raises(falcon.HTTPContentTooLarge):
                req.get_media()

    def test_stream_and_media_share_quota(self):
        options = falcon.RequestOptions()
        options.max_request_body_size = 10
        env = testing.create_environ(
            body=b'{"a": "1234567"}',
            headers={'Content-Type': 'application/json'},
        )
        req = falcon.Request(env, options=options)

        assert req.stream.read(4) == b'{"a"'

        with pytest.raises(falcon.HTTPContentTooLarge):
            req.get_media()
