from datetime import datetime
from datetime import timedelta
from datetime import timezone
import uuid

import pytest

import falcon
from falcon import testing
from falcon.routing import BaseConverter
from falcon.routing.compiled import UnacceptableRouteError


class CapturingResource:
    def __init__(self):
        self.captured_kwargs = None
        self.request_count = 0

    def on_get(self, req, resp, **kwargs):
        self.captured_kwargs = kwargs
        self.request_count += 1
        resp.text = 'OK'


class SuffixedResource:
    def __init__(self):
        self.captured_kwargs = None

    def on_get_collection(self, req, resp, **kwargs):
        self.captured_kwargs = kwargs
        resp.text = 'collection'


class ReversingConverter(BaseConverter):
    def convert(self, value):
        return value.upper()

    def reverse(self, value):
        return str(value).lower()


class NonReversingConverter(BaseConverter):
    def convert(self, value):
        return value


class BadReverseConverter(BaseConverter):
    def convert(self, value):
        return value

    def reverse(self, value):
        return 123


class StubRouter:
    """Minimal custom router without URI generation support."""

    def __init__(self):
        self.routes = {}

    def add_route(self, uri_template, resource, **kwargs):
        self.routes[uri_template] = resource

    def find(self, uri, req=None):
        return None


@pytest.fixture()
def app(asgi, util):
    return util.create_app(asgi)


@pytest.fixture()
def client(app):
    return testing.TestClient(app)


def test_round_trip_builtin_converters(app, client):
    resource = CapturingResource()
    app.add_route('/items/{item_id:int}', resource, name='item')
    app.add_route('/prices/{amount:float}', resource, name='price')
    app.add_route('/users/{user_id:uuid}', resource, name='user')
    app.add_route('/at/{moment:dt}', resource, name='moment')
    app.add_route('/files/{file_path:path}', resource, name='file')

    moment = datetime(2024, 3, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=2)))
    user_id = uuid.uuid4()

    cases = [
        ('item', {'item_id': 42}, {'item_id': 42}),
        ('price', {'amount': 1.5}, {'amount': 1.5}),
        ('user', {'user_id': user_id}, {'user_id': user_id}),
        ('moment', {'moment': moment}, {'moment': moment}),
        (
            'file',
            {'file_path': 'some/nested/file.txt'},
            {'file_path': 'some/nested/file.txt'},
        ),
    ]

    for name, params, expected in cases:
        uri = app.url_for(name, **params)
        assert uri.startswith('/')

        result = client.get(uri)
        assert result.status == falcon.HTTP_200
        assert resource.captured_kwargs == expected


def test_round_trip_complex_segment(app, client):
    resource = CapturingResource()
    app.add_route(
        '/repos/{org}/{repo}/compare/{usr0}:{branch0}...{usr1}:{branch1}',
        resource,
        name='compare',
    )

    uri = app.url_for(
        'compare',
        org='falconry',
        repo='falcon',
        usr0='johndoe',
        branch0='master',
        usr1='janedoe',
        branch1='feature-x',
    )
    assert uri == '/repos/falconry/falcon/compare/johndoe:master...janedoe:feature-x'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {
        'org': 'falconry',
        'repo': 'falcon',
        'usr0': 'johndoe',
        'branch0': 'master',
        'usr1': 'janedoe',
        'branch1': 'feature-x',
    }


def test_int_converter_constraints(app, client):
    resource = CapturingResource()
    app.add_route('/things/{thing_id:int(4, min=1, max=9999)}', resource, name='thing')

    uri = app.url_for('thing', thing_id=42)
    assert uri == '/things/0042'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'thing_id': 42}

    with pytest.raises(ValueError):
        app.url_for('thing', thing_id=0)
    with pytest.raises(ValueError):
        app.url_for('thing', thing_id=10000)
    with pytest.raises(ValueError):
        app.url_for('thing', thing_id='42')
    with pytest.raises(ValueError):
        app.url_for('thing', thing_id=True)


def test_float_converter_constraints(app):
    resource = CapturingResource()
    app.add_route('/prices/{amount:float(min=0.5, max=10.0)}', resource, name='price')

    assert app.url_for('price', amount=0.5) == '/prices/0.5'
    assert app.url_for('price', amount=10.0) == '/prices/10.0'

    with pytest.raises(ValueError):
        app.url_for('price', amount=0.4)
    with pytest.raises(ValueError):
        app.url_for('price', amount=10.1)
    with pytest.raises(ValueError):
        app.url_for('price', amount=float('nan'))
    with pytest.raises(ValueError):
        app.url_for('price', amount='1.5')


def test_float_converter_nan_not_equal_to_itself(app):
    resource = CapturingResource()
    app.add_route(
        '/prices/{amount:float(finite=False)}', resource, name='price'
    )

    assert app.url_for('price', amount=float('inf')) == '/prices/inf'

    with pytest.raises(ValueError):
        app.url_for('price', amount=float('nan'))


def test_trailing_slash(app, client):
    resource = CapturingResource()
    app.add_route('/things/{name}/', resource, name='thing')

    uri = app.url_for('thing', name='widget')
    assert uri == '/things/widget/'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'name': 'widget'}


def test_unicode_and_special_chars(app, client):
    resource = CapturingResource()
    app.add_route('/tags/{label}', resource, name='tag')

    uri = app.url_for('tag', label='ü %?&=')
    assert uri == '/tags/%C3%BC%20%25%3F%26%3D'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'label': 'ü %?&='}


def test_empty_path_converter_value(app, client):
    resource = CapturingResource()
    app.add_route('/files/{file_path:path}', resource, name='file')

    uri = app.url_for('file', file_path='')
    assert uri == '/files/'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'file_path': ''}


def test_path_converter_segments_from_iterable(app, client):
    resource = CapturingResource()
    app.add_route('/files/{file_path:path}', resource, name='file')

    uri = app.url_for('file', file_path=['a', 'b c', 'ü'])
    assert uri == '/files/a/b%20c/%C3%BC'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'file_path': 'a/b c/ü'}


def test_path_converter_rejects_non_segment_value(app):
    resource = CapturingResource()
    app.add_route('/files/{file_path:path}', resource, name='file')

    with pytest.raises(ValueError):
        app.url_for('file', file_path=123)


def test_duplicate_route_name(app, client):
    resource = CapturingResource()
    other = CapturingResource()
    app.add_route('/things/{thing_id:int}', resource, name='thing')

    with pytest.raises(UnacceptableRouteError):
        app.add_route('/others/{other_id:int}', other, name='thing')

    # The conflicting route must not have been half-registered; the
    #   original name still maps to the original route, and the
    #   conflicting template does not match.
    assert app.url_for('thing', thing_id=1) == '/things/1'
    result = client.get('/things/1')
    assert result.status == falcon.HTTP_200
    assert resource.request_count == 1

    result = client.get('/others/1')
    assert result.status == falcon.HTTP_404
    assert other.request_count == 0


def test_same_resource_registered_multiple_times(app, client):
    resource = CapturingResource()
    app.add_route('/things/{thing_id:int}', resource, name='thing')
    app.add_route('/things', resource, name='things')
    app.add_route('/things/{thing_id:int}/details', resource, name='thing-details')

    assert app.url_for('thing', thing_id=3) == '/things/3'
    assert app.url_for('things') == '/things'
    assert app.url_for('thing-details', thing_id=3) == '/things/3/details'

    for uri in ('/things/3', '/things', '/things/3/details'):
        result = client.get(uri)
        assert result.status == falcon.HTTP_200

    assert resource.request_count == 3


def test_same_resource_same_template_multiple_names(app):
    resource = CapturingResource()
    app.add_route('/things/{thing_id:int}', resource, name='thing')
    app.add_route('/things/{thing_id:int}', resource, name='thing-alias')

    assert app.url_for('thing', thing_id=7) == '/things/7'
    assert app.url_for('thing-alias', thing_id=7) == '/things/7'


def test_named_route_with_suffix(app, client):
    resource = SuffixedResource()
    app.add_route('/things', resource, suffix='collection', name='things')

    assert app.url_for('things') == '/things'

    result = client.get('/things')
    assert result.status == falcon.HTTP_200
    assert result.text == 'collection'


def test_unnamed_routes_unaffected(app, client):
    resource = CapturingResource()
    app.add_route('/plain/{value}', resource)
    app.add_route('/named/{value}', resource, name='named')

    assert app.url_for('named', value='x') == '/named/x'

    result = client.get('/plain/y')
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'value': 'y'}


def test_unknown_route_name(app):
    with pytest.raises(KeyError):
        app.url_for('missing')


def test_missing_parameter(app):
    resource = CapturingResource()
    app.add_route('/things/{thing_id:int}', resource, name='thing')

    with pytest.raises(KeyError):
        app.url_for('thing')


def test_extra_parameter(app):
    resource = CapturingResource()
    app.add_route('/things/{thing_id:int}', resource, name='thing')

    with pytest.raises(ValueError):
        app.url_for('thing', thing_id=1, other=2)


def test_slash_in_plain_parameter_rejected(app):
    resource = CapturingResource()
    app.add_route('/tags/{label}', resource, name='tag')

    with pytest.raises(ValueError):
        app.url_for('tag', label='a/b')


def test_invalid_route_name(app):
    resource = CapturingResource()

    with pytest.raises(TypeError):
        app.add_route('/things', resource, name=123)

    with pytest.raises(TypeError):
        app.add_route('/things', resource, name='')


def test_route_names_independent_between_apps(asgi, util):
    resource = CapturingResource()
    app_one = util.create_app(asgi)
    app_two = util.create_app(asgi)

    app_one.add_route('/one/{value}', resource, name='route')
    app_two.add_route('/two/{value}', resource, name='route')

    assert app_one.url_for('route', value='x') == '/one/x'
    assert app_two.url_for('route', value='x') == '/two/x'


def test_custom_converter_without_reverse(app):
    app.router_options.converters['noreverse'] = NonReversingConverter
    app.add_route('/things/{value:noreverse}', CapturingResource(), name='thing')

    with pytest.raises(falcon.UnsupportedError):
        app.url_for('thing', value='x')


def test_builtin_converter_without_reverse(app):
    app.add_route(
        '/things/{value:re("[a-z]+")}', CapturingResource(), name='thing'
    )

    with pytest.raises(falcon.UnsupportedError):
        app.url_for('thing', value='abc')


def test_custom_converter_with_reverse(app, client):
    app.router_options.converters['upper'] = ReversingConverter
    resource = CapturingResource()
    app.add_route('/things/{value:upper}', resource, name='thing')

    uri = app.url_for('thing', value='ABC')
    assert uri == '/things/abc'

    result = client.get(uri)
    assert result.status == falcon.HTTP_200
    assert resource.captured_kwargs == {'value': 'ABC'}


def test_custom_converter_reverse_not_returning_str(app):
    app.router_options.converters['badreverse'] = BadReverseConverter
    app.add_route('/things/{value:badreverse}', CapturingResource(), name='thing')

    with pytest.raises(TypeError):
        app.url_for('thing', value='x')


def test_custom_router_unsupported(asgi, util):
    app = util.create_app(asgi, router=StubRouter())
    app.add_route('/things', CapturingResource())

    with pytest.raises(falcon.UnsupportedError):
        app.url_for('thing')


def test_uuid_converter_accepts_string(app):
    resource = CapturingResource()
    app.add_route('/users/{user_id:uuid}', resource, name='user')

    user_id = uuid.uuid4()
    assert app.url_for('user', user_id=str(user_id)) == '/users/{0}'.format(user_id)

    with pytest.raises(ValueError):
        app.url_for('user', user_id='not-a-uuid')


def test_datetime_converter_rejects_non_datetime(app):
    resource = CapturingResource()
    app.add_route('/at/{moment:dt}', resource, name='moment')

    with pytest.raises(ValueError):
        app.url_for('moment', moment='2024-01-01')


def test_datetime_converter_unrepresentable_value(app):
    resource = CapturingResource()
    app.add_route("/at/{moment:dt('%Z')}", resource, name='moment')

    # A naive datetime formats '' for '%Z', which strptime() can not
    #   parse back.
    with pytest.raises(ValueError):
        app.url_for('moment', moment=datetime(2024, 1, 1))
