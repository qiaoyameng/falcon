"""App-level tests for multi-segment converters followed by a URI suffix.

These tests exercise the compiled router through full WSGI and ASGI app
requests, observing the routed resource and the extracted parameters.
"""

import itertools
import time

import pytest

from falcon import testing
from falcon.routing import DefaultRouter
from falcon.routing.converters import BaseConverter


class Resource:
    def __init__(self, resource_id):
        self.resource_id = resource_id
        self.kwargs = None

    def on_get(self, req, resp, **kwargs):
        self.kwargs = kwargs
        resp.media = {'id': self.resource_id, 'params': kwargs}


class GetOnlyResource:
    def on_get(self, req, resp, **kwargs):
        resp.media = {'params': kwargs}


@pytest.fixture
def client(asgi, util):
    return testing.TestClient(util.create_app(asgi))


def test_basic_suffix(client):
    archives = Resource(1)
    client.app.add_route('/archives/{key:path}/revisions/{revision:int}', archives)

    resp = client.simulate_get('/archives/a/b/c/revisions/5')

    assert resp.status_code == 200
    assert resp.json == {'id': 1, 'params': {'key': 'a/b/c', 'revision': 5}}
    assert archives.kwargs == {'key': 'a/b/c', 'revision': 5}


def test_static_route_priority(client):
    dynamic = Resource(1)
    static = Resource(2)
    static_prefix = Resource(3)
    client.app.add_route('/archives/{key:path}/revisions/{revision:int}', dynamic)
    client.app.add_route('/archives/all', static)
    client.app.add_route('/archives/featured/revisions/{revision:int}', static_prefix)

    resp = client.simulate_get('/archives/all')
    assert resp.json == {'id': 2, 'params': {}}

    # NOTE: The static segment takes priority over the multi-segment
    #   converter, exactly as it does over a regular dynamic segment.
    resp = client.simulate_get('/archives/featured/revisions/3')
    assert resp.json == {'id': 3, 'params': {'revision': 3}}

    resp = client.simulate_get('/archives/other/revisions/3')
    assert resp.json == {'id': 1, 'params': {'key': 'other', 'revision': 3}}


def test_suffix_type_failure_falls_back(client):
    catch_all = Resource(1)
    client.app.add_route('/archives/{key:path}', catch_all)
    client.app.add_route(
        '/archives/{key:path}/revisions/{revision:int}', Resource(2)
    )

    resp = client.simulate_get('/archives/a/revisions/7')
    assert resp.json == {'id': 2, 'params': {'key': 'a', 'revision': 7}}

    # NOTE: The int converter rejects the final segment, so the request
    #   falls back to the catch-all route; no parameters from the failed
    #   candidate may leak into the params of the route that matches.
    resp = client.simulate_get('/archives/a/revisions/xyz')
    assert resp.json == {'id': 1, 'params': {'key': 'a/revisions/xyz'}}
    assert catch_all.kwargs == {'key': 'a/revisions/xyz'}


def test_partial_match_then_next_candidate(client):
    latest = Resource(1)
    revision = Resource(2)
    client.app.add_route('/a/{key:path}/revisions/latest', latest)
    client.app.add_route('/a/{key:path}/revisions/{revision:int}', revision)

    resp = client.simulate_get('/a/x/y/revisions/latest')
    assert resp.json == {'id': 1, 'params': {'key': 'x/y'}}
    assert latest.kwargs == {'key': 'x/y'}

    resp = client.simulate_get('/a/x/y/revisions/42')
    assert resp.json == {'id': 2, 'params': {'key': 'x/y', 'revision': 42}}
    assert revision.kwargs == {'key': 'x/y', 'revision': 42}


def test_suffix_with_complex_segment(client):
    by_name = Resource(1)
    by_part = Resource(2)
    client.app.add_route('/a/{key:path}/file-{name}.{ext}', by_name)
    client.app.add_route('/a/{key:path}/part-{index:int}-{label}', by_part)

    resp = client.simulate_get('/a/x/y/file-report.txt')
    assert resp.json == {
        'id': 1,
        'params': {'key': 'x/y', 'name': 'report', 'ext': 'txt'},
    }

    resp = client.simulate_get('/a/x/y/part-3-foo')
    assert resp.json == {
        'id': 2,
        'params': {'key': 'x/y', 'index': 3, 'label': 'foo'},
    }

    # NOTE: The complex suffix requires an int; fall through to no match.
    resp = client.simulate_get('/a/x/y/part-foo-3')
    assert resp.status_code == 404


def test_method_not_allowed(client):
    client.app.add_route(
        '/archives/{key:path}/revisions/{revision:int}', GetOnlyResource()
    )

    resp = client.simulate_post('/archives/a/b/revisions/5')

    assert resp.status_code == 405
    assert resp.headers['Allow'] == 'GET, OPTIONS'


def test_trailing_slash(client):
    client.app.add_route(
        '/archives/{key:path}/revisions/{revision:int}', Resource(1)
    )
    client.app.add_route(
        '/docs/{key:path}/revisions/{revision:int}/', Resource(2)
    )

    resp = client.simulate_get('/archives/a/revisions/5/')
    assert resp.status_code == 404

    resp = client.simulate_get('/docs/a/b/revisions/5/')
    assert resp.json == {'id': 2, 'params': {'key': 'a/b', 'revision': 5}}

    resp = client.simulate_get('/docs/a/b/revisions/5')
    assert resp.status_code == 404


def test_repeated_suffix_literal(client):
    client.app.add_route(
        '/archives/{key:path}/revisions/{revision:int}', Resource(1)
    )

    # NOTE: The multi-segment converter consumes as many segments as
    #   possible; the suffix is anchored to the last two segments.
    resp = client.simulate_get('/archives/a/revisions/1/revisions/2')
    assert resp.json == {'id': 1, 'params': {'key': 'a/revisions/1', 'revision': 2}}


def test_add_route_after_first_compile(client):
    client.app.add_route('/one/{key:path}/revisions/{revision:int}', Resource(1))

    resp = client.simulate_get('/one/a/revisions/1')
    assert resp.json == {'id': 1, 'params': {'key': 'a', 'revision': 1}}

    # NOTE: The router has been compiled by the first request; adding
    #   another route must recompile it on demand.
    client.app.add_route('/two/{key:path}/revisions/{revision:int}', Resource(2))

    resp = client.simulate_get('/two/b/c/revisions/2')
    assert resp.json == {'id': 2, 'params': {'key': 'b/c', 'revision': 2}}

    resp = client.simulate_get('/one/a/revisions/1')
    assert resp.json == {'id': 1, 'params': {'key': 'a', 'revision': 1}}


def test_registration_order_independence(asgi, util):
    routes = (
        ('/a/{key:path}/revisions/{revision:int}', 1),
        ('/a/{key:path}/revisions/latest', 2),
        ('/a/{key:path}', 3),
        ('/a/static/page', 4),
    )
    requests = (
        '/a/p/q/revisions/7',
        '/a/p/revisions/latest',
        '/a/p/q',
        '/a/static/page',
    )

    expected = None
    for order in itertools.permutations(range(len(routes))):
        other_client = testing.TestClient(util.create_app(asgi))
        for index in order:
            template, resource_id = routes[index]
            other_client.app.add_route(template, Resource(resource_id))

        outcome = tuple(other_client.simulate_get(path).json for path in requests)
        if expected is None:
            expected = outcome
        assert outcome == expected


@pytest.mark.parametrize(
    'template',
    (
        '/foo/{a:path}/{b:path}',
        '/foo/{a:path}/bar/{b:path}',
        '/foo/{a:path}/bar/{b:path}/baz',
    ),
)
def test_reject_multiple_multi_segment(client, template):
    client.app.add_route('/foo/{a:path}/bar', Resource(1))
    client.simulate_get('/foo/x/bar')

    with pytest.raises(ValueError, match='only one field per route template'):
        client.app.add_route(template, Resource(2))

    # NOTE: The rejected template must not pollute the compiled router.
    resp = client.simulate_get('/foo/x/y/bar')
    assert resp.json == {'id': 1, 'params': {'a': 'x/y'}}


def test_custom_multi_segment_converter(client):
    class SegmentsConverter(BaseConverter):
        CONSUME_MULTIPLE_SEGMENTS = True

        def convert(self, value):
            if len(value) > 3:
                return None
            return list(value)

    client.app.router_options.converters['segs'] = SegmentsConverter
    client.app.add_route('/s/{parts:segs}/tail', Resource(1))

    resp = client.simulate_get('/s/a/b/tail')
    assert resp.json == {'id': 1, 'params': {'parts': ['a', 'b']}}

    # NOTE: The converter itself rejects four segments; no shorter split
    #   exists, so the request does not match.
    resp = client.simulate_get('/s/a/b/c/d/tail')
    assert resp.status_code == 404


def test_deep_suffix(client):
    client.app.add_route(
        '/a/{key:path}/revisions/{revision:int}/meta/{name}', Resource(1)
    )

    resp = client.simulate_get('/a/x/y/revisions/3/meta/author')
    assert resp.json == {
        'id': 1,
        'params': {'key': 'x/y', 'revision': 3, 'name': 'author'},
    }


def test_scale_long_path_with_repeated_suffix_literal(client):
    # NOTE: The compiled router computes a single end-anchored split per
    #   candidate route, so matching stays linear in the path length even
    #   when the suffix literal repeats many times in the path; an
    #   implementation enumerating split points would not complete this
    #   many repetitions in time.
    client.app.add_route(
        '/archives/{key:path}/revisions/{revision:int}', Resource(1)
    )

    count = 5000
    path = '/archives/' + '/'.join(['revisions'] * count) + '/42'

    start = time.perf_counter()
    resp = client.simulate_get(path)
    elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    assert resp.json == {
        'id': 1,
        'params': {'key': '/'.join(['revisions'] * (count - 1)), 'revision': 42},
    }
    # NOTE: Generous bound; observed time is on the order of milliseconds.
    assert elapsed < 5.0


def test_router_find_directly():
    # NOTE: Sanity check of the raw router API without an app.
    router = DefaultRouter()
    router.add_route('/archives/{key:path}/revisions/{revision:int}', Resource(1))

    resource, __, params, uri_template = router.find('/archives/a/b/revisions/9')
    assert resource.resource_id == 1
    assert params == {'key': 'a/b', 'revision': 9}
    assert uri_template == '/archives/{key:path}/revisions/{revision:int}'

    assert router.find('/archives/revisions/9') is None
