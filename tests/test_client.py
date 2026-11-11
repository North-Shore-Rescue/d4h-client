"""Pins the wire contract of D4hClient: which HTTP verb, URL, query parameters and
body each method sends, and how responses are turned into return values.

Requests are intercepted at requests.Session.send, the lowest point every request
passes through whether the client calls requests.get() or holds its own Session.
That keeps these tests valid across a refactor of the client's internals: they
assert on what goes over the wire, not on how the client produces it. Retry
waits are captured rather than slept, so no test ever pauses.

The client is team-agnostic, so these tests use a made-up team, host, page size
and record IDs.
"""
import ast
import json
import logging
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import requests

import d4h.client
from d4h import D4hClient, LocalD4hClient, save_lists

TEAM_ID = 4242
BASE_URL = "https://api.example.test/v3/"
DEFAULT_PAGE_SIZE = 250
TEAM_PATH = f"/v3/team/{TEAM_ID}"


def make_client(**overrides):
    settings = dict(team_id=TEAM_ID, base_url=BASE_URL, page_size=DEFAULT_PAGE_SIZE)
    settings.update(overrides)
    return D4hClient('test-key', **settings)


def make_response(status=200, payload=None, body=b'', headers=None):
    """Build a real requests.Response so .ok, .json() and .text behave as in production."""
    response = requests.Response()
    response.status_code = status
    response.encoding = 'utf-8'
    response._content = json.dumps(payload).encode() if payload is not None else body
    response.headers.update(headers or {})
    return response


def list_payload(results, total=None, page=0):
    """A list response shaped like the real D4H v3 API, including paging metadata."""
    return {'results': results, 'page': page, 'pageSize': DEFAULT_PAGE_SIZE,
            'totalSize': len(results) if total is None else total}


def ids(records):
    return [record['id'] for record in records]


class D4hClientTestCase(unittest.TestCase):
    """Base class: a client whose HTTP traffic is captured, never sent."""

    def setUp(self):
        # Retry warnings are expected in these tests; keep them out of the output.
        logging.getLogger('d4h').setLevel(logging.ERROR)
        self.addCleanup(logging.getLogger('d4h').setLevel, logging.NOTSET)
        self.client = make_client()
        self.sent = []
        self.send_kwargs = []
        self.sleeps = []
        self.queued = []
        self.next_response = make_response(payload=list_payload([]))

        def fake_send(session, request, **kwargs):
            self.sent.append(request)
            self.send_kwargs.append(kwargs)
            outcome = self.queued.pop(0) if self.queued else self.next_response
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        for patcher in (
            patch.object(requests.Session, 'send', autospec=True, side_effect=fake_send),
            patch.object(d4h.client.time, 'sleep', side_effect=self.sleeps.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def respond_with(self, status=200, payload=None, body=b''):
        self.next_response = make_response(status, payload, body)

    def queue(self, *outcomes):
        """Outcomes for the next requests, in order: responses to return or exceptions to raise."""
        self.queued.extend(outcomes)

    def queries(self):
        return [dict(parse_qsl(urlsplit(request.url).query)) for request in self.sent]

    def last_request(self):
        self.assertEqual(len(self.sent), 1, "expected exactly one HTTP request")
        return self.sent[0]

    def assert_request(self, method, path, query=None, body=None):
        """Assert the single request sent matches; `path` is relative to the team URL."""
        request = self.last_request()
        url = urlsplit(request.url)
        self.assertEqual(request.method, method)
        self.assertEqual(url.path, f"{TEAM_PATH}/{path}")
        if query is not None:
            self.assertEqual(dict(parse_qsl(url.query)), query)
        if body is not None:
            self.assertEqual(json.loads(request.body), body)
        return request


class TestConnection(D4hClientTestCase):

    def test_requests_go_to_the_configured_base_url_and_team(self):
        self.client.members.list()
        self.assertEqual(self.last_request().url.split('?')[0], "https://api.example.test/v3/team/4242/members")

    def test_a_different_team_and_region_are_used_when_configured(self):
        client = make_client(team_id=7, base_url="https://api.team-manager.us.d4h.com/v3/")
        client.members.list()
        self.assertEqual(self.last_request().url.split('?')[0],
                         "https://api.team-manager.us.d4h.com/v3/team/7/members")

    def test_base_url_without_trailing_slash_is_accepted(self):
        client = make_client(base_url="https://api.example.test/v3")
        client.members.list()
        self.assertEqual(self.last_request().url.split('?')[0], "https://api.example.test/v3/team/4242/members")

    def test_team_and_base_url_are_required(self):
        with self.assertRaises(TypeError):
            D4hClient('test-key')
        with self.assertRaises(TypeError):
            # Positional extras used to mean `local`; they must not be read as a team ID.
            D4hClient('test-key', False)

    def test_local_mode_is_not_a_constructor_flag(self):
        with self.assertRaises(TypeError):
            make_client(local=True)

    def test_api_key_is_sent_as_bearer_token(self):
        self.client.members.list()
        self.assertEqual(self.last_request().headers['Authorization'], 'Bearer test-key')

    def test_json_writes_declare_json_content_type(self):
        self.respond_with(payload={'id': 1})
        self.client.attendance.create({'memberId': 1, 'activityId': 2, 'startsAt': '2026-01-01T00:00:00Z', 'endsAt': '2026-01-01T01:00:00Z'})
        self.assertEqual(self.last_request().headers['Content-Type'], 'application/json')


class TestListEndpoints(D4hClientTestCase):

    LISTS = {
        'member_qualifications': 'member-qualifications',
        'member_qualification_awards': 'member-qualification-awards',
        'members': 'members',
        'events': 'events',
        'exercises': 'exercises',
        'incidents': 'incidents',
        'incident_involved_persons': 'incident-involved-persons',
        'member_group_memberships': 'member-group-memberships',
        'location_bookmarks': 'location-bookmarks',
        'tags': 'tags',
        'duties': 'duties',
        'attendance': 'attendance',
    }

    def test_each_list_gets_its_endpoint_and_returns_results(self):
        records = [{'id': 1}, {'id': 2}]
        for attribute, path in self.LISTS.items():
            with self.subTest(collection=attribute):
                self.sent.clear()
                self.respond_with(payload=list_payload(records))

                result = getattr(self.client, attribute).list()

                self.assert_request('GET', path, query={'size': str(DEFAULT_PAGE_SIZE)})
                self.assertEqual(result, records)

    def test_list_requests_ask_for_the_configured_page_size(self):
        self.client.members.list()
        self.assert_request('GET', 'members', query={'size': '250'})

    def test_page_size_defaults_to_10000(self):
        client = D4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL)
        client.members.list()
        self.assert_request('GET', 'members', query={'size': '10000'})

    def test_filters_are_sent_as_given(self):
        # The API's own query names: snake_case on attendance, camelCase on duties.
        self.client.attendance.list(activity_resource_type='Exercise', status='ATTENDING', member_id=17,
                                    activity_id=42, starts_after='2026-01-01', starts_before='2026-02-01T08:30:00Z')
        self.assert_request('GET', 'attendance', query={
            'size': str(DEFAULT_PAGE_SIZE),
            'activity_resource_type': 'Exercise',
            'status': 'ATTENDING',
            'member_id': '17',
            'activity_id': '42',
            'starts_after': '2026-01-01',
            'starts_before': '2026-02-01T08:30:00Z',
        })

        self.sent.clear()
        self.client.duties.list(after='2026-01-01T00:00:00Z', memberId=17)
        self.assert_request('GET', 'duties', query={
            'size': str(DEFAULT_PAGE_SIZE), 'after': '2026-01-01T00:00:00Z', 'memberId': '17'})


class TestRecordCalls(D4hClientTestCase):
    """The writes NSR relies on, made through resources: bodies are sent exactly as given."""

    def test_create_sends_the_body_and_returns_the_parsed_response(self):
        cases = [
            ('attendance', 'attendance', {'memberId': 17, 'activityId': 42, 'startsAt': '2026-01-01T18:00:00Z',
                                          'endsAt': '2026-01-01T21:00:00Z', 'status': 'ATTENDING'}),
            ('exercises', 'exercises', {'referenceDescription': 'Rope Rescue Practice', 'fullTeam': False,
                                        'startsAt': '2026-01-06T02:00:00Z', 'endsAt': '2026-01-06T05:00:00Z'}),
            ('events', 'events', {'referenceDescription': 'Open House', 'trackingNumber': '26-1003',
                                  'startsAt': '2026-01-06T02:00:00Z', 'endsAt': '2026-01-06T05:00:00Z'}),
            ('member_qualification_awards', 'member-qualification-awards',
             {'memberId': 17, 'qualificationId': 602, 'startsAt': '2026-01-01T00:00:00Z'}),
        ]
        for attribute, path, body in cases:
            with self.subTest(collection=attribute):
                self.sent.clear()
                self.respond_with(payload={'id': 9})

                result = getattr(self.client, attribute).create(body)

                request = self.assert_request('POST', path, body=body)
                self.assertEqual(request.headers['Content-Type'], 'application/json')
                self.assertEqual(result, {'id': 9})

    def test_update_patches_the_record(self):
        for attribute, path in (('exercises', 'exercises'), ('events', 'events')):
            with self.subTest(collection=attribute):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                result = getattr(self.client, attribute).update(359312, {'description': 'Corrected'})

                self.assert_request('PATCH', f'{path}/359312', body={'description': 'Corrected'})
                self.assertEqual(result, {'id': 359312})

    def test_delete(self):
        self.respond_with(status=204)

        result = self.client.attendance.delete(30001)

        request = self.assert_request('DELETE', 'attendance/30001')
        self.assertIsNone(request.body)
        self.assertIs(result, True)

    def test_tagging_an_activity(self):
        for attribute in ('exercises', 'events', 'incidents'):
            with self.subTest(collection=attribute):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                getattr(self.client, attribute).action('tags', item_id=359312, data={'tagIds': [5841, 5970]})

                self.assert_request('POST', f'{attribute}/359312/tags', body={'tagIds': [5841, 5970]})

    def test_adding_a_duty_period(self):
        body = {'memberId': 17, 'roleId': 501, 'type': 'ON', 'startsAt': '2026-01-01T00:00:00Z',
                'endsAt': '2026-01-08T00:00:00Z', 'notes': ''}
        self.respond_with(payload={'id': 5})

        self.client.duties.action('add-duty-period', data=body)

        self.assert_request('POST', 'duties/add-duty-period', body=body)


class TestDocumentUpload(D4hClientTestCase):

    def setUp(self):
        super().setUp()
        handle = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        handle.write(b'%PDF-fake certificate bytes')
        handle.close()
        self.file_path = handle.name
        self.addCleanup(os.remove, self.file_path)
        self.respond_with(payload={'id': 77})

    def multipart_fields(self, request):
        """Return {field name: value bytes} for a multipart/form-data request body."""
        content_type = request.headers['Content-Type']
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='), content_type)
        boundary = content_type.split('boundary=')[1].encode()
        body = request.body if isinstance(request.body, bytes) else request.body.encode()
        fields = {}
        for part in body.split(b'--' + boundary):
            if b'\r\n\r\n' not in part:
                continue
            headers, value = part.split(b'\r\n\r\n', 1)
            name = headers.split(b'name="')[1].split(b'"')[0].decode()
            fields[name] = value.rstrip(b'\r\n')
        return fields

    def test_upload_sends_multipart_form_with_file_and_fields(self):
        result = self.client.documents.upload(self.file_path, {
            'targetResourceType': 'MemberQualificationAward', 'targetResourceId': 900,
            'restricted': 'UNRESTRICTED', 'title': 'First Aid'})

        request = self.assert_request('POST', 'documents')
        self.assertEqual(self.multipart_fields(request), {
            'targetResourceType': b'MemberQualificationAward',
            'targetResourceId': b'900',
            'restricted': b'UNRESTRICTED',
            'title': b'First Aid',
            'file': b'%PDF-fake certificate bytes',
        })
        self.assertIn(f'filename="{os.path.basename(self.file_path)}"'.encode(), request.body)
        self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
        self.assertEqual(result, {'id': 77})


class TestResponseHandling(D4hClientTestCase):

    def test_error_status_raises_http_error_with_context(self):
        self.respond_with(status=422, body=b'{"error":"startsAt is required"}')

        with self.assertRaises(requests.HTTPError) as caught:
            self.client.attendance.create({'memberId': 17, 'activityId': 42})

        message = str(caught.exception)
        self.assertIn('POST', message)
        self.assertIn(f'team/{TEAM_ID}/attendance', message)
        self.assertIn('422', message)
        self.assertIn('startsAt is required', message)
        self.assertEqual(caught.exception.response.status_code, 422)

    def test_error_status_on_a_read_also_raises(self):
        self.respond_with(status=401, body=b'Unauthorized')
        with self.assertRaises(requests.HTTPError):
            self.client.members.list()

    def test_server_error_raises(self):
        self.respond_with(status=503, body=b'Service Unavailable')
        with self.assertRaises(requests.HTTPError):
            self.client.tags.list()

    def test_no_content_response_returns_true(self):
        self.respond_with(status=204)
        self.assertIs(self.client.attendance.delete(1), True)

    def test_success_with_empty_body_returns_true(self):
        self.respond_with(status=200, body=b'')
        self.assertIs(self.client.exercises.action('tags', item_id=1, data={'tagIds': [2]}), True)

    def test_json_body_is_returned_parsed(self):
        self.respond_with(status=201, payload={'id': 9, 'resourceType': 'Exercise'})
        result = self.client.exercises.create({'referenceDescription': 'Ref'})
        self.assertEqual(result, {'id': 9, 'resourceType': 'Exercise'})


class TestPagination(D4hClientTestCase):
    """Lists are fetched in full: D4H returns at most `size` rows per request."""

    def setUp(self):
        super().setUp()
        self.client = make_client(page_size=2)

    def test_follows_pages_until_total_size_is_reached(self):
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=5)),
            make_response(payload=list_payload([{'id': 3}, {'id': 4}], total=5, page=1)),
            make_response(payload=list_payload([{'id': 5}], total=5, page=2)),
        )

        result = self.client.members.list()

        self.assertEqual(ids(result), [1, 2, 3, 4, 5])
        # The first request is unchanged from the single-request client; later ones add `page`.
        self.assertEqual(self.queries(), [{'size': '2'}, {'size': '2', 'page': '1'}, {'size': '2', 'page': '2'}])
        self.assertEqual({urlsplit(request.url).path for request in self.sent}, {f"{TEAM_PATH}/members"})

    def test_a_list_that_fits_in_one_page_takes_one_request(self):
        self.queue(make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=2)))
        self.assertEqual(ids(self.client.members.list()), [1, 2])
        self.assertEqual(len(self.sent), 1)

    def test_an_empty_list_takes_one_request(self):
        self.queue(make_response(payload=list_payload([])))
        self.assertEqual(self.client.members.list(), [])
        self.assertEqual(len(self.sent), 1)

    def test_stops_at_an_empty_page_even_if_total_size_promised_more(self):
        # Rows deleted while paging: never loop waiting for records that no longer exist.
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=5)),
            make_response(payload=list_payload([], total=0, page=1)),
        )
        self.assertEqual(ids(self.client.members.list()), [1, 2])
        self.assertEqual(len(self.sent), 2)

    def test_without_total_size_stops_at_the_first_short_page(self):
        self.queue(
            make_response(payload={'results': [{'id': 1}, {'id': 2}]}),
            make_response(payload={'results': [{'id': 3}]}),
        )
        self.assertEqual(ids(self.client.members.list()), [1, 2, 3])
        self.assertEqual(len(self.sent), 2)

    def test_negative_total_size_means_unknown_so_paging_continues(self):
        # D4H's search endpoint reports totalSize -1.
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=-1)),
            make_response(payload=list_payload([{'id': 3}, {'id': 4}], total=-1, page=1)),
            make_response(payload=list_payload([], total=-1, page=2)),
        )
        self.assertEqual(ids(self.client.members.list()), [1, 2, 3, 4])
        self.assertEqual(len(self.sent), 3)

    def test_attendance_filters_are_sent_with_every_page(self):
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=3)),
            make_response(payload=list_payload([{'id': 3}], total=3, page=1)),
        )

        result = self.client.attendance.list(status='ATTENDING', member_id=17)

        self.assertEqual(ids(result), [1, 2, 3])
        self.assertEqual(self.queries(), [
            {'size': '2', 'status': 'ATTENDING', 'member_id': '17'},
            {'size': '2', 'status': 'ATTENDING', 'member_id': '17', 'page': '1'},
        ])

    def test_an_error_on_a_later_page_raises_rather_than_returning_a_partial_list(self):
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=5)),
            make_response(status=500, body=b'boom'),
        )
        with self.assertRaises(requests.HTTPError):
            self.client.members.list()


class TestTimeoutsAndRetries(D4hClientTestCase):

    def test_every_request_has_a_timeout(self):
        self.client.members.list()
        self.respond_with(payload={'id': 1})
        self.client.attendance.create({'memberId': 1, 'activityId': 2, 'startsAt': '2026-01-01T00:00:00Z', 'endsAt': '2026-01-01T01:00:00Z'})
        self.assertEqual([kwargs['timeout'] for kwargs in self.send_kwargs], [60, 60])

    def test_timeout_is_configurable(self):
        make_client(timeout=5).members.list()
        self.assertEqual(self.send_kwargs[0]['timeout'], 5)

    def test_get_is_retried_after_a_gateway_error(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.sent.clear()
                self.sleeps.clear()
                self.queue(make_response(status=status, body=b'unavailable'),
                           make_response(payload=list_payload([{'id': 1}])))

                self.assertEqual(ids(self.client.members.list()), [1])
                self.assertEqual(len(self.sent), 2)
                self.assertEqual(self.sleeps, [1.0])

    def test_retry_wait_doubles_each_attempt(self):
        self.queue(*[make_response(status=503) for _ in range(3)], make_response(payload=list_payload([])))
        self.client.members.list()
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_gives_up_after_max_retries_and_raises(self):
        self.respond_with(status=503, body=b'Service Unavailable')

        with self.assertRaises(requests.HTTPError) as caught:
            self.client.members.list()

        self.assertEqual(caught.exception.response.status_code, 503)
        self.assertEqual(len(self.sent), 4)  # the original attempt plus three retries
        self.assertEqual(len(self.sleeps), 3)

    def test_retries_can_be_disabled(self):
        self.respond_with(status=503)
        with self.assertRaises(requests.HTTPError):
            make_client(max_retries=0).members.list()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sleeps, [])

    def test_writes_are_not_retried_after_a_gateway_error(self):
        # The write may have been applied before the gateway failed; resending could duplicate it.
        self.respond_with(status=503)
        calls = [
            lambda: self.client.attendance.create({'memberId': 1, 'activityId': 2, 'startsAt': '2026-01-01T00:00:00Z', 'endsAt': '2026-01-01T01:00:00Z'}),
            lambda: self.client.events.update(9, {'description': 'x'}),
            lambda: self.client.attendance.delete(9),
        ]
        for call in calls:
            self.sent.clear()
            with self.assertRaises(requests.HTTPError):
                call()
            self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sleeps, [])

    def test_client_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 422, 500):
            with self.subTest(status=status):
                self.sent.clear()
                self.respond_with(status=status)
                with self.assertRaises(requests.HTTPError):
                    self.client.members.list()
                self.assertEqual(len(self.sent), 1)

    def test_rate_limited_requests_are_retried_for_any_method(self):
        # 429 means D4H rejected the request before acting on it, so a write is safe to resend.
        self.queue(make_response(status=429, headers={'Retry-After': '7'}), make_response(payload={'id': 30001}))

        result = self.client.attendance.create({'memberId': 17, 'activityId': 42, 'startsAt': '2026-01-01T18:00:00Z', 'endsAt': '2026-01-01T21:00:00Z'})

        self.assertEqual(result, {'id': 30001})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0].body, self.sent[1].body)
        self.assertEqual(self.sleeps, [7.0])

    def test_retry_after_is_capped_and_non_numeric_values_fall_back_to_backoff(self):
        self.queue(make_response(status=429, headers={'Retry-After': '600'}),
                   make_response(status=429, headers={'Retry-After': 'Wed, 11 Nov 2026 20:00:00 GMT'}),
                   make_response(payload=list_payload([])))
        self.client.members.list()
        self.assertEqual(self.sleeps, [60, 2.0])

    def test_get_is_retried_after_a_dropped_connection_or_timeout(self):
        for error in (requests.ConnectionError('reset'), requests.Timeout('slow')):
            with self.subTest(error=type(error).__name__):
                self.sent.clear()
                self.queue(error, make_response(payload=list_payload([{'id': 1}])))
                self.assertEqual(ids(self.client.members.list()), [1])
                self.assertEqual(len(self.sent), 2)

    def test_connection_errors_propagate_once_retries_run_out(self):
        self.queue(*[requests.ConnectionError('reset') for _ in range(4)])
        with self.assertRaises(requests.ConnectionError):
            self.client.members.list()
        self.assertEqual(len(self.sent), 4)

    def test_writes_are_not_retried_after_a_dropped_connection(self):
        self.queue(requests.ConnectionError('reset'))
        with self.assertRaises(requests.ConnectionError):
            self.client.attendance.create({'memberId': 1, 'activityId': 2, 'startsAt': '2026-01-01T00:00:00Z', 'endsAt': '2026-01-01T01:00:00Z'})
        self.assertEqual(len(self.sent), 1)

    def test_a_retried_upload_resends_the_whole_file(self):
        handle = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        handle.write(b'%PDF-fake certificate bytes')
        handle.close()
        self.addCleanup(os.remove, handle.name)
        self.queue(make_response(status=429), make_response(payload={'id': 77}))

        self.client.documents.upload(handle.name, {'targetResourceType': 'MemberQualificationAward', 'targetResourceId': 900})

        self.assertEqual(len(self.sent), 2)
        self.assertIn(b'%PDF-fake certificate bytes', self.sent[1].body)


class TestGenericRequests(D4hClientTestCase):
    """Escape hatch for endpoints that have no named method."""

    def test_get_returns_the_parsed_response(self):
        self.respond_with(payload={'id': 17, 'name': 'Alex Example'})
        result = self.client.get('members/17', {'include': 'groups'})
        self.assert_request('GET', 'members/17', query={'include': 'groups'})
        self.assertEqual(result, {'id': 17, 'name': 'Alex Example'})

    def test_list_all_pages_through_any_list_endpoint(self):
        client = make_client(page_size=1)
        self.queue(make_response(payload=list_payload([{'id': 1}], total=2)),
                   make_response(payload=list_payload([{'id': 2}], total=2, page=1)))

        result = client.list_all('custom-fields', {'bundle': 'x'})

        self.assertEqual(ids(result), [1, 2])
        self.assertEqual(self.queries(), [{'size': '1', 'bundle': 'x'}, {'size': '1', 'bundle': 'x', 'page': '1'}])
        self.assertEqual(urlsplit(self.sent[0].url).path, f"{TEAM_PATH}/custom-fields")

    def test_post_sends_json(self):
        self.respond_with(payload={'id': 5})
        result = self.client.post('whiteboard', {'text': 'Road closed'})
        request = self.assert_request('POST', 'whiteboard', body={'text': 'Road closed'})
        self.assertEqual(request.headers['Content-Type'], 'application/json')
        self.assertEqual(result, {'id': 5})

    def test_patch_sends_json(self):
        self.respond_with(payload={'id': 5})
        self.client.patch('members/17', {'position': 'Team Leader'})
        self.assert_request('PATCH', 'members/17', body={'position': 'Team Leader'})

    def test_delete(self):
        self.respond_with(status=204)
        self.assertIs(self.client.delete('whiteboard/5'), True)
        self.assert_request('DELETE', 'whiteboard/5')


class TestPackageIsSelfContained(unittest.TestCase):
    """The package stays small: requests is its only dependency."""

    def setUp(self):
        package_dir = pathlib.Path(d4h.client.__file__).parent
        self.modules = {path.name: ast.parse(path.read_text()) for path in sorted(package_dir.glob('*.py'))}
        self.assertIn('client.py', self.modules)
        self.assertIn('local.py', self.modules)

    def imports(self, tree):
        """Absolute module names imported by `tree` (relative imports are within the package)."""
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.append(node.module or '')
        return names

    def test_only_the_standard_library_and_requests_are_imported(self):
        allowed = {'json', 'logging', 'os', 'time', 'requests'}
        for filename, tree in self.modules.items():
            with self.subTest(module=filename):
                self.assertEqual({name.split('.')[0] for name in self.imports(tree)} - allowed, set())

    def test_no_pandas_and_no_printing(self):
        for filename, tree in self.modules.items():
            with self.subTest(module=filename):
                self.assertNotIn('pandas', [name.split('.')[0] for name in self.imports(tree)])
                printed = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Name) and node.func.id == 'print']
                self.assertEqual(printed, [])


class TestLocalClient(unittest.TestCase):
    """LocalD4hClient reads lists from JSON files and must not touch the network to do so."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = directory.name

        self.write('members', [{'id': 1, 'name': 'Alex Example'}])
        self.write('duties', [
            {'id': 1, 'role': {'id': 501}},
            {'id': 2, 'role': {'id': 99}},
            {'id': 3},
        ])
        self.write('attendance', [
            {'id': 1, 'member': {'id': 17}, 'activity': {'id': 42, 'resourceType': 'Exercise'},
             'status': 'ATTENDING', 'startsAt': '2026-01-10T18:00:00Z'},
            {'id': 2, 'member': {'id': 18}, 'activity': {'id': 42, 'resourceType': 'Exercise'},
             'status': 'ABSENT', 'startsAt': '2026-01-10T18:00:00Z'},
            {'id': 3, 'member': {'id': 17}, 'activity': {'id': 43, 'resourceType': 'Incident'},
             'status': 'ATTENDING', 'startsAt': '2026-03-05T04:00:00Z'},
        ])

        self.client = LocalD4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL, data_dir=self.data_dir)

        self.sent = []

        def fake_send(session, request, **kwargs):
            self.sent.append(request)
            return make_response(payload={'id': 30001})

        patcher = patch.object(requests.Session, 'send', autospec=True, side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, results):
        with open(os.path.join(self.data_dir, f'{name}.json'), 'w') as file:
            json.dump({'results': results}, file)

    def test_is_a_d4h_client(self):
        self.assertIsInstance(self.client, D4hClient)

    def test_reads_results_from_the_data_directory(self):
        self.assertEqual(self.client.members.list(), [{'id': 1, 'name': 'Alex Example'}])
        self.assertEqual(self.sent, [])

    def test_each_collection_reads_its_file(self):
        files = {
            'member_qualifications': 'qualifications',
            'member_qualification_awards': 'member_qualifications',
            'members': 'members',
            'events': 'events',
            'exercises': 'exercises',
            'incidents': 'incidents',
            'incident_involved_persons': 'incident_involved_persons',
            'member_group_memberships': 'member_groups',
            'location_bookmarks': 'location_bookmarks',
            'tags': 'tags',
            'duties': 'duties',
            'attendance': 'attendance',
            'equipment_brands': 'equipment_brands',
        }
        for attribute, file_name in files.items():
            with self.subTest(collection=attribute):
                self.write(file_name, [{'id': file_name}])
                self.assertEqual(getattr(self.client, attribute).list(), [{'id': file_name}])
        self.assertEqual(self.sent, [])

    def test_a_file_saved_with_the_endpoints_own_name_is_found(self):
        self.write('custom-fields', [{'id': 8}])
        self.assertEqual(self.client.custom_fields.list(), [{'id': 8}])

    def test_generic_list_all_reads_a_file_named_after_the_endpoint(self):
        self.write('custom_fields', [{'id': 8}])
        self.assertEqual(self.client.list_all('custom-fields'), [{'id': 8}])
        self.assertEqual(self.sent, [])

    def test_attendance_filters_are_applied_to_saved_data(self):
        cases = [
            ({}, [1, 2, 3]),
            ({'activity_resource_type': 'Exercise'}, [1, 2]),
            ({'status': 'ATTENDING'}, [1, 3]),
            ({'member_id': 17}, [1, 3]),
            ({'activity_id': 42}, [1, 2]),
            ({'starts_after': '2026-02-01'}, [3]),
            ({'starts_before': '2026-02-01'}, [1, 2]),
            ({'status': 'ATTENDING', 'activity_id': 42}, [1]),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.assertEqual(ids(self.client.attendance.list(**filters)), expected)
        self.assertEqual(self.sent, [])

    def test_other_lists_ignore_filters(self):
        self.assertEqual(ids(self.client.duties.list(memberId=99)), [1, 2, 3])

    def test_writes_still_go_to_the_api(self):
        # Only reads are local. Callers that must not write use a dry run in the layer above.
        result = self.client.attendance.create({'memberId': 17, 'activityId': 42, 'startsAt': '2026-01-01T18:00:00Z', 'endsAt': '2026-01-01T21:00:00Z'})

        self.assertEqual(result, {'id': 30001})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].method, 'POST')
        self.assertEqual(self.sent[0].url, f"{BASE_URL}team/{TEAM_ID}/attendance")


class TestLocalReads(unittest.TestCase):
    """Reads beyond lists: single records and single-object endpoints come from saved files too."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = directory.name
        with open(os.path.join(self.data_dir, 'members.json'), 'w') as file:
            json.dump({'results': [{'id': 17, 'name': 'Alex Example'}, {'id': 18, 'name': 'Sam Example'}]}, file)
        with open(os.path.join(self.data_dir, 'settings.json'), 'w') as file:
            json.dump({'timezone': 'America/Vancouver'}, file)
        self.client = LocalD4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL, data_dir=self.data_dir)
        patcher = patch.object(requests.Session, 'send', autospec=True,
                               side_effect=AssertionError("local reads must not make HTTP requests"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_finds_the_record_in_the_saved_list(self):
        self.assertEqual(self.client.members.get(18), {'id': 18, 'name': 'Sam Example'})
        self.assertEqual(self.client.members.get('17')['name'], 'Alex Example')

    def test_get_of_a_missing_record_raises_lookup_error(self):
        with self.assertRaises(LookupError):
            self.client.members.get(99)

    def test_single_object_endpoint_reads_its_file(self):
        self.assertEqual(self.client.settings.get(), {'timezone': 'America/Vancouver'})

    def test_missing_data_raises_instead_of_calling_the_api(self):
        with self.assertRaises(FileNotFoundError):
            self.client.equipment.list()
        with self.assertRaises(FileNotFoundError):
            self.client.documents.list_related(9, 'revisions')


class TestSaveLists(D4hClientTestCase):

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = os.path.join(directory.name, 'data')

    def test_saves_each_collection_in_the_shape_the_local_client_reads(self):
        self.queue(make_response(payload=list_payload([{'id': 17}])),
                   make_response(payload=list_payload([{'id': 1, 'status': 'ATTENDING'}])),
                   make_response(payload=list_payload([{'id': 4783}])),
                   make_response(payload={'timezone': 'America/Vancouver'}))

        summary = save_lists(self.client, self.data_dir,
                             ['members', 'attendance', 'member_qualifications', 'settings'])

        self.assertEqual(summary, {'members': 1, 'attendance': 1, 'member_qualifications': 1, 'settings': 1})
        # member-qualifications keeps its historical file name.
        self.assertEqual(sorted(os.listdir(self.data_dir)),
                         ['attendance.json', 'members.json', 'qualifications.json', 'settings.json'])
        local = LocalD4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL, data_dir=self.data_dir)
        self.assertEqual(local.members.get(17), {'id': 17})
        self.assertEqual(local.attendance.list(status='ATTENDING'), [{'id': 1, 'status': 'ATTENDING'}])
        self.assertEqual(local.member_qualifications.list(), [{'id': 4783}])
        self.assertEqual(local.settings.get(), {'timezone': 'America/Vancouver'})

    def test_a_collection_that_fails_is_skipped_and_reported(self):
        self.queue(make_response(status=403, body=b'module disabled'),
                   make_response(payload=list_payload([{'id': 17}])))
        logging.getLogger('d4h').setLevel(logging.CRITICAL)

        summary = save_lists(self.client, self.data_dir, ['animals', 'members'])

        self.assertEqual(summary, {'animals': 'skipped (403)', 'members': 1})
        self.assertEqual(os.listdir(self.data_dir), ['members.json'])

    def test_a_failed_refresh_keeps_the_previous_file(self):
        os.makedirs(self.data_dir)
        with open(os.path.join(self.data_dir, 'members.json'), 'w') as file:
            json.dump({'results': [{'id': 'old'}]}, file)
        self.queue(make_response(status=403))
        logging.getLogger('d4h').setLevel(logging.CRITICAL)

        save_lists(self.client, self.data_dir, ['members'])

        with open(os.path.join(self.data_dir, 'members.json')) as file:
            self.assertEqual(json.load(file), {'results': [{'id': 'old'}]})

    def test_default_saves_every_collection_except_ones_needing_arguments(self):
        summary = save_lists(self.client, self.data_dir)
        self.assertNotIn('search', summary)
        self.assertNotIn('modules', summary)
        self.assertIn('equipment', summary)
        self.assertIn('settings', summary)

    def test_unknown_collection_is_an_error(self):
        with self.assertRaises(ValueError):
            save_lists(self.client, self.data_dir, ['equipmnet'])


if __name__ == '__main__':
    unittest.main()
