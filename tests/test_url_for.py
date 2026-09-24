import uuid
from datetime import datetime
from datetime import timezone

import pytest

import falcon
from falcon import testing
from falcon.routing import BaseConverter
from falcon.routing import CompiledRouter
from falcon.routing.compiled import UnacceptableRouteError


class EchoResource:
    def on_get(self, req, resp, **params):
        resp.media = {name: str(value) for name, value in params.items()}


@pytest.fixture()
def app(asgi, util):
    return util.create_app(asgi)


@pytest.fixture()
def client(app):
    return testing.TestClient(app)


def roundtrip(client, app, route_name, expected_path, **params):
    path = app.url_for(route_name, **params)
    assert path == expected_path

    response = client.simulate_get(path)
    assert response.status == falcon.HTTP_200
    return response


class TestUrlForRoundTrip:
    def test_simple_field(self, app, client):
        app.add_route('/things/{tid}', EchoResource(), name='thing')

        response = roundtrip(client, app, 'thing', '/things/42', tid='42')
        assert response.json == {'tid': '42'}

    def test_field_named_name(self, app, client):
        app.add_route('/hello/{name}', EchoResource(), name='hello')

        response = roundtrip(client, app, 'hello', '/hello/world', name='world')
        assert response.json == {'name': 'world'}

    def test_complex_segment(self, app, client):
        app.add_route(
            '/repos/{org}/{repo}/compare/{usr0}:{branch0}...{usr1}:{branch1}',
            EchoResource(),
            name='compare',
        )

        response = roundtrip(
            client,
            app,
            'compare',
            '/repos/falconry/falcon/compare/jdoe:main...asmith:dev',
            org='falconry',
            repo='falcon',
            usr0='jdoe',
            branch0='main',
            usr1='asmith',
            branch1='dev',
        )
        assert response.json == {
            'org': 'falconry',
            'repo': 'falcon',
            'usr0': 'jdoe',
            'branch0': 'main',
            'usr1': 'asmith',
            'branch1': 'dev',
        }

    def test_int_converter(self, app, client):
        app.add_route(
            '/items/{oid:int(min=7, max=100)}', EchoResource(), name='item'
        )

        response = roundtrip(client, app, 'item', '/items/42', oid=42)
        assert response.json == {'oid': '42'}

    def test_int_converter_num_digits(self, app, client):
        app.add_route('/codes/{code:int(num_digits=4)}', EchoResource(), name='code')

        response = roundtrip(client, app, 'code', '/codes/1234', code=1234)
        assert response.json == {'code': '1234'}

    def test_float_converter(self, app, client):
        app.add_route(
            '/geo/{lat:float(min=-90, max=90)}', EchoResource(), name='geo'
        )

        response = roundtrip(client, app, 'geo', '/geo/45.5', lat=45.5)
        assert response.json == {'lat': '45.5'}

    def test_uuid_converter(self, app, client):
        app.add_route('/objects/{oid:uuid}', EchoResource(), name='object')

        oid = uuid.uuid4()
        response = roundtrip(client, app, 'object', '/objects/{0}'.format(oid), oid=oid)
        assert response.json == {'oid': str(oid)}

    def test_datetime_converter(self, app, client):
        app.add_route('/logs/{ts:dt}', EchoResource(), name='log')

        ts = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        response = roundtrip(
            client, app, 'log', '/logs/2024-01-02T03%3A04%3A05%2B0000', ts=ts
        )
        assert response.json == {'ts': '2024-01-02 03:04:05+00:00'}

    def test_datetime_converter_custom_format(self, app, client):
        app.add_route(
            '/archive/{day:dt("%Y%m%d")}', EchoResource(), name='archive'
        )

        day = datetime(2024, 3, 17)
        response = roundtrip(client, app, 'archive', '/archive/20240317', day=day)
        assert response.json == {'day': '2024-03-17 00:00:00'}

    def test_path_converter(self, app, client):
        app.add_route('/files/{file_path:path}', EchoResource(), name='file')

        response = roundtrip(
            client, app, 'file', '/files/a/b/c.txt', file_path='a/b/c.txt'
        )
        assert response.json == {'file_path': 'a/b/c.txt'}

    def test_path_converter_empty(self, app, client):
        app.add_route('/files/{file_path:path}', EchoResource(), name='file')

        response = roundtrip(client, app, 'file', '/files/', file_path='')
        assert response.json == {'file_path': ''}

    def test_path_converter_unicode_segments(self, app, client):
        app.add_route('/files/{file_path:path}', EchoResource(), name='file')

        response = roundtrip(
            client,
            app,
            'file',
            '/files/a/%E6%97%A5%E6%9C%AC/b%25c',
            file_path='a/日本/b%c',
        )
        assert response.json == {'file_path': 'a/日本/b%c'}

    def test_trailing_slash(self, app, client):
        app.add_route('/trail/{tid}/', EchoResource(), name='trail')

        response = roundtrip(client, app, 'trail', '/trail/xyz/', tid='xyz')
        assert response.json == {'tid': 'xyz'}

    @pytest.mark.parametrize(
        'value,expected',
        [
            ('100% sure', '100%25%20sure'),
            ('what?', 'what%3F'),
            ('café', 'caf%C3%A9'),
            ('日本語', '%E6%97%A5%E6%9C%AC%E8%AA%9E'),
            ('a#b&c=d', 'a%23b%26c%3Dd'),
        ],
    )
    def test_value_encoding(self, app, client, value, expected):
        app.add_route('/echo/{text}', EchoResource(), name='echo')

        response = roundtrip(
            client, app, 'echo', '/echo/{0}'.format(expected), text=value
        )
        assert response.json == {'text': value}

    def test_root_route(self, app, client):
        app.add_route('/', EchoResource(), name='root')

        response = roundtrip(client, app, 'root', '/')
        assert response.json == {}

    def test_static_template(self, app, client):
        app.add_route('/static/path', EchoResource(), name='static')

        response = roundtrip(client, app, 'static', '/static/path')
        assert response.json == {}

    def test_unnamed_routes_unaffected(self, app, client):
        app.add_route('/named/{tid}', EchoResource(), name='named')
        app.add_route('/unnamed/{tid}', EchoResource())

        response = client.simulate_get('/unnamed/5')
        assert response.status == falcon.HTTP_200
        assert response.json == {'tid': '5'}

    def test_suffixed_responders(self, app, client):
        class SuffixedResource:
            def on_get_collection(self, req, resp, tid):
                resp.text = 'collection:{0}'.format(tid)

        app.add_route(
            '/sfx/{tid}',
            SuffixedResource(),
            suffix='collection',
            name='sfx',
        )

        assert app.url_for('sfx', tid='9') == '/sfx/9'
        response = client.simulate_get('/sfx/9')
        assert response.text == 'collection:9'


class TestUrlForErrors:
    def test_unknown_name(self, app):
        app.add_route('/things/{tid}', EchoResource(), name='thing')

        with pytest.raises(KeyError, match='No route is registered'):
            app.url_for('nope', tid='1')

    def test_missing_param(self, app):
        app.add_route('/things/{tid}/{sub}', EchoResource(), name='thing')

        with pytest.raises(ValueError, match='Missing required.*tid'):
            app.url_for('thing', sub='x')

    def test_extra_param(self, app):
        app.add_route('/things/{tid}', EchoResource(), name='thing')

        with pytest.raises(ValueError, match='Unexpected.*nope'):
            app.url_for('thing', tid='1', nope='x')

    def test_missing_and_extra_params(self, app):
        app.add_route('/things/{tid}', EchoResource(), name='thing')

        with pytest.raises(ValueError, match='Missing required'):
            app.url_for('thing', nope='x')

    @pytest.mark.parametrize('value', [6, 101, 'not-an-int'])
    def test_int_converter_constraints(self, app, value):
        app.add_route(
            '/items/{oid:int(min=7, max=100)}', EchoResource(), name='item'
        )

        with pytest.raises(ValueError, match='does not satisfy'):
            app.url_for('item', oid=value)

    def test_int_converter_num_digits(self, app):
        app.add_route('/codes/{code:int(num_digits=4)}', EchoResource(), name='code')

        with pytest.raises(ValueError, match='does not satisfy'):
            app.url_for('code', code=42)

    @pytest.mark.parametrize('value', [-91.0, 91.0, float('inf'), float('nan')])
    def test_float_converter_constraints(self, app, value):
        app.add_route(
            '/geo/{lat:float(min=-90, max=90)}', EchoResource(), name='geo'
        )

        with pytest.raises(ValueError, match='does not satisfy'):
            app.url_for('geo', lat=value)

    def test_uuid_converter_constraints(self, app):
        app.add_route('/objects/{oid:uuid}', EchoResource(), name='object')

        with pytest.raises(ValueError, match='does not satisfy'):
            app.url_for('object', oid='not-a-uuid')

    def test_datetime_converter_wrong_type(self, app):
        app.add_route('/logs/{ts:dt}', EchoResource(), name='log')

        with pytest.raises(ValueError, match='Unable to render'):
            app.url_for('log', ts='2024-01-02')

    def test_slash_in_simple_field_rejected(self, app):
        app.add_route('/things/{tid}', EchoResource(), name='thing')

        with pytest.raises(ValueError, match='may not contain "/"'):
            app.url_for('thing', tid='a/b')

    def test_converter_without_to_string(self, app):
        class ShoutConverter(BaseConverter):
            def convert(self, value):
                return value.upper()

        app.router_options.converters['shout'] = ShoutConverter
        app.add_route('/shout/{word:shout}', EchoResource(), name='shout')

        with pytest.raises(NotImplementedError, match='to_string'):
            app.url_for('shout', word='hello')

    def test_converter_to_string_not_returning_str(self, app):
        class BrokenConverter(BaseConverter):
            def convert(self, value):
                return value

            def to_string(self, value):
                return 42

        app.router_options.converters['broken'] = BrokenConverter
        app.add_route('/broken/{val:broken}', EchoResource(), name='broken')

        with pytest.raises(ValueError, match='must return a string'):
            app.url_for('broken', val='x')

    def test_converter_to_string_rendering_slash(self, app):
        class SneakyConverter(BaseConverter):
            def convert(self, value):
                return value

            def to_string(self, value):
                return 'a/b'

        app.router_options.converters['sneaky'] = SneakyConverter
        app.add_route('/sneaky/{val:sneaky}', EchoResource(), name='sneaky')

        with pytest.raises(ValueError, match='multiple path segments'):
            app.url_for('sneaky', val='x')

    def test_custom_router_not_supported(self, asgi, util):
        class CustomRouter:
            def add_route(self, uri_template, resource, **kwargs):
                pass

            def find(self, uri, req=None):
                return None

        app = util.create_app(asgi, router=CustomRouter())
        app.add_route('/things/{tid}', EchoResource(), name='thing')

        with pytest.raises(NotImplementedError, match='does not support'):
            app.url_for('thing', tid='1')


class TestCustomConverter:
    def test_custom_converter_roundtrip(self, app, client):
        class SlugConverter(BaseConverter):
            def convert(self, value):
                return value.replace('-', ' ')

            def to_string(self, value):
                return value.replace(' ', '-')

        app.router_options.converters['slug'] = SlugConverter
        app.add_route('/posts/{title:slug}', EchoResource(), name='post')

        response = roundtrip(
            client, app, 'post', '/posts/hello-world', title='hello world'
        )
        assert response.json == {'title': 'hello world'}

    def test_custom_multi_segment_converter(self, app, client):
        class ListConverter(BaseConverter):
            CONSUME_MULTIPLE_SEGMENTS = True

            def convert(self, value):
                return [int(part) for part in value]

            def to_string(self, value):
                return '/'.join(str(item) for item in value)

        app.router_options.converters['intlist'] = ListConverter
        app.add_route('/sum/{numbers:intlist}', EchoResource(), name='sum')

        response = roundtrip(
            client, app, 'sum', '/sum/1/2/3', numbers=[1, 2, 3]
        )
        assert response.json == {'numbers': '[1, 2, 3]'}


class TestRouteNames:
    def test_duplicate_name_different_template(self, app):
        app.add_route('/one/{tid}', EchoResource(), name='dup')

        with pytest.raises(UnacceptableRouteError, match='already used'):
            app.add_route('/two/{tid}', EchoResource(), name='dup')

    def test_duplicate_name_leaves_no_partial_state(self, app, client):
        app.add_route('/one/{tid}', EchoResource(), name='dup')

        with pytest.raises(UnacceptableRouteError):
            app.add_route('/two/{tid}', EchoResource(), name='dup')

        # NOTE: the original name still maps to the original route, and
        # the rejected route was not added to the routing tree.
        assert app.url_for('dup', tid='1') == '/one/1'
        assert client.simulate_get('/two/1').status == falcon.HTTP_404
        assert client.simulate_get('/one/1').status == falcon.HTTP_200

    def test_same_name_same_template_allowed(self, app):
        app.add_route('/one/{tid}', EchoResource(), name='dup')
        app.add_route('/one/{tid}', EchoResource(), name='dup')

        assert app.url_for('dup', tid='1') == '/one/1'

    @pytest.mark.parametrize('name', ['', 123])
    def test_invalid_name(self, app, name):
        with pytest.raises(UnacceptableRouteError, match='non-empty strings'):
            app.add_route('/things', EchoResource(), name=name)

    def test_names_are_scoped_per_app(self, asgi, util):
        app1 = util.create_app(asgi)
        app2 = util.create_app(asgi)

        app1.add_route('/one/{tid}', EchoResource(), name='item')
        app2.add_route('/two/{tid}', EchoResource(), name='item')

        assert app1.url_for('item', tid='1') == '/one/1'
        assert app2.url_for('item', tid='1') == '/two/1'

    def test_router_level_url_for(self):
        router = CompiledRouter()
        router.add_route('/things/{tid:int}', EchoResource(), name='thing')

        assert router.url_for('thing', tid=3) == '/things/3'
        resource, _, params, _ = router.find('/things/3')
        assert isinstance(resource, EchoResource)
        assert params == {'tid': 3}
