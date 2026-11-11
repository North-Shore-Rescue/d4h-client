"""Every operation in D4H's published API spec can be called through the client.

tests/spec_operations.json lists each (method, path) in the spec; refresh it with
`uv run scripts/update_spec_operations.py`. For each operation this test makes the
matching client call and checks the HTTP method and URL that go over the wire, so
a new endpoint in the spec fails here until the client covers it.
"""
import json
import pathlib
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from d4h import D4hClient
from d4h.resources import COLLECTIONS, ENDPOINTS, Endpoint, Resource

TEAM_ID = 4242
BASE_URL = "https://api.example.test/v3/"
ITEM_ID = 7
CHILD_ID = 8

SPEC = json.loads((pathlib.Path(__file__).parent / 'spec_operations.json').read_text())
ATTRIBUTE_FOR_PATH = {path: name for name, path in {**COLLECTIONS, **ENDPOINTS}.items()}


def response(payload):
    resp = requests.Response()
    resp.status_code = 200
    resp.encoding = 'utf-8'
    resp.headers['Content-Type'] = 'application/json'
    resp._content = json.dumps(payload).encode()
    return resp


def expected_path(spec_path):
    """The URL path the client should request for a spec path with its placeholders filled in."""
    segments = spec_path.split('/')
    filled = []
    ids = iter([ITEM_ID, CHILD_ID])
    for index, segment in enumerate(segments):
        if segment in ('{context}',):
            filled.append('team')
        elif segment in ('{contextId}', '{teamId}') and filled and filled[-1] == 'team':
            filled.append(str(TEAM_ID))
        elif segment == '{organisationId}' and filled and filled[-1] == 'organisation':
            filled.append(str(ITEM_ID))
        elif segment.startswith('{'):
            filled.append(str(next(ids)))
        else:
            filled.append(segment)
    return '/'.join(filled)


def call_for(client, method, spec_path):
    """Make the client call that corresponds to one spec operation."""
    root_calls = {
        ('GET', '/v3/healthcheck'): client.healthcheck,
        ('GET', '/v3/whoami'): client.whoami,
        ('GET', '/v3/public/mobile-app-versions'): client.mobile_app_versions,
        ('GET', '/v3/organisation/{organisationId}/settings'): lambda: client.get_organisation_settings(ITEM_ID),
    }
    if (method, spec_path) in root_calls:
        return root_calls[(method, spec_path)]()

    for prefix in ('/v3/team/{teamId}/', '/v3/{context}/{contextId}/'):
        if spec_path.startswith(prefix):
            rest = spec_path[len(prefix):].split('/')
            break
    else:
        raise AssertionError(f"no client call for {method} {spec_path}")

    attribute = ATTRIBUTE_FOR_PATH.get(rest[0])
    if attribute is None:
        raise AssertionError(f"no client attribute for collection '{rest[0]}' ({method} {spec_path})")
    target = getattr(client, attribute)
    tail = rest[1:]

    if isinstance(target, Endpoint):
        assert method == 'GET' and not tail, (method, spec_path)
        return target.get()

    if not tail:
        return {'GET': target.list, 'POST': lambda: target.create({})}[method]()
    if tail[0].startswith('{'):
        if len(tail) == 1:
            return {
                'GET': lambda: target.get(ITEM_ID),
                'PATCH': lambda: target.update(ITEM_ID, {}),
                'PUT': lambda: target.replace(ITEM_ID, {}),
                'DELETE': lambda: target.delete(ITEM_ID),
            }[method]()
        action = '/'.join(str(CHILD_ID) if part.startswith('{') else part for part in tail[1:])
        return target.action(action, item_id=ITEM_ID, method=method)
    return target.action('/'.join(tail), method=method)


class TestSpecCoverage(unittest.TestCase):

    def setUp(self):
        self.client = D4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL)
        self.sent = []

        def fake_send(session, request, **kwargs):
            self.sent.append(request)
            return response({'results': [], 'totalSize': 0})

        patcher = patch.object(requests.Session, 'send', autospec=True, side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_spec_list_is_present(self):
        self.assertGreater(len(SPEC['operations']), 250)

    def test_every_spec_operation_sends_the_right_request(self):
        for operation in SPEC['operations']:
            method, spec_path = operation['method'], operation['path']
            with self.subTest(f"{method} {spec_path}"):
                self.sent.clear()
                call_for(self.client, method, spec_path)
                self.assertEqual(len(self.sent), 1)
                self.assertEqual(self.sent[0].method, method)
                self.assertEqual(urlsplit(self.sent[0].url).path, expected_path(spec_path))

    def test_every_client_collection_exists_in_the_spec(self):
        spec_collections = set()
        for operation in SPEC['operations']:
            for prefix in ('/v3/team/{teamId}/', '/v3/{context}/{contextId}/'):
                if operation['path'].startswith(prefix):
                    spec_collections.add(operation['path'][len(prefix):].split('/')[0])
        self.assertEqual(set(COLLECTIONS.values()) | set(ENDPOINTS.values()), spec_collections)


class TestResources(unittest.TestCase):

    def setUp(self):
        self.client = D4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL, page_size=2)
        self.sent = []
        self.responses = []

        def fake_send(session, request, **kwargs):
            self.sent.append(request)
            return self.responses.pop(0)

        patcher = patch.object(requests.Session, 'send', autospec=True, side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_attributes_are_resources_bound_to_the_client(self):
        self.assertIsInstance(self.client.equipment, Resource)
        self.assertIsInstance(self.client.settings, Endpoint)
        self.assertEqual(self.client.equipment_brands.path, 'equipment-brands')

    def test_list_follows_pages_and_sends_filters(self):
        self.responses += [response({'results': [{'id': 1}, {'id': 2}], 'totalSize': 3}),
                           response({'results': [{'id': 3}], 'totalSize': 3})]
        result = self.client.equipment.list(status='OPERATIONAL')
        self.assertEqual([r['id'] for r in result], [1, 2, 3])
        self.assertEqual(self.sent[1].url,
                         f"{BASE_URL}team/{TEAM_ID}/equipment?size=2&status=OPERATIONAL&page=1")

    def test_search_pages_at_its_lower_size_limit(self):
        client = D4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL)
        self.responses.append(response({'results': [{'id': 1}], 'totalSize': 1}))
        client.search.list(query='rope')
        self.assertEqual(self.sent[0].url, f"{BASE_URL}team/{TEAM_ID}/search?size=100&query=rope")

    def test_list_related_follows_pages(self):
        self.responses += [response({'results': [{'id': 1}, {'id': 2}], 'totalSize': 3}),
                           response({'results': [{'id': 3}], 'totalSize': 3})]
        result = self.client.documents.list_related(9, 'revisions')
        self.assertEqual([r['id'] for r in result], [1, 2, 3])
        self.assertEqual(urlsplit(self.sent[0].url).path, f"/v3/team/{TEAM_ID}/documents/9/revisions")

    def test_create_and_update_send_the_body_as_given(self):
        self.responses += [response({'id': 5}), response({'id': 5})]
        self.client.tasks.create({'title': 'Check radios', 'dueAt': '2026-12-01T00:00:00Z'})
        self.client.tasks.update(5, {'title': 'Check all radios'})
        self.assertEqual(json.loads(self.sent[0].body), {'title': 'Check radios', 'dueAt': '2026-12-01T00:00:00Z'})
        self.assertEqual(self.sent[1].method, 'PATCH')
        self.assertEqual(json.loads(self.sent[1].body), {'title': 'Check all radios'})

    def test_action_with_body_and_query(self):
        self.responses.append(response({'id': 17}))
        self.client.members.action('retire', item_id=17, method='PATCH', data={'direction': 'RETIRE'})
        self.assertEqual(self.sent[0].method, 'PATCH')
        self.assertEqual(urlsplit(self.sent[0].url).path, f"/v3/team/{TEAM_ID}/members/17/retire")
        self.assertEqual(json.loads(self.sent[0].body), {'direction': 'RETIRE'})

    def test_downloads_return_bytes(self):
        image = requests.Response()
        image.status_code = 200
        image.headers['Content-Type'] = 'image/png'
        image._content = b'\x89PNG fake'
        self.responses.append(image)
        result = self.client.members.action('image', item_id=17, method='GET', params={'size': 'THUMBNAIL'})
        self.assertEqual(result, b'\x89PNG fake')
        self.assertEqual(self.sent[0].url, f"{BASE_URL}team/{TEAM_ID}/members/17/image?size=THUMBNAIL")

    def test_document_revision_upload_is_multipart_put(self):
        self.responses.append(response({'id': 9}))
        self.client.documents.replace(9, {'title': 'v2'}, files={'file': ('card.pdf', b'%PDF')})
        request = self.sent[0]
        self.assertEqual(request.method, 'PUT')
        self.assertTrue(request.headers['Content-Type'].startswith('multipart/form-data'))
        self.assertIn(b'%PDF', request.body)

    def test_unsupported_method_raises_the_api_error(self):
        error = requests.Response()
        error.status_code = 405
        error._content = b'Method Not Allowed'
        self.responses.append(error)
        with self.assertRaises(requests.HTTPError):
            self.client.roles.delete(3)


if __name__ == '__main__':
    unittest.main()
