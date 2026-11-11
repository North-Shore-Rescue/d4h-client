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
from d4h import D4hClient, LocalD4hClient

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
        self.client.get_members()
        self.assertEqual(self.last_request().url.split('?')[0], "https://api.example.test/v3/team/4242/members")

    def test_a_different_team_and_region_are_used_when_configured(self):
        client = make_client(team_id=7, base_url="https://api.team-manager.us.d4h.com/v3/")
        client.get_members()
        self.assertEqual(self.last_request().url.split('?')[0],
                         "https://api.team-manager.us.d4h.com/v3/team/7/members")

    def test_base_url_without_trailing_slash_is_accepted(self):
        client = make_client(base_url="https://api.example.test/v3")
        client.get_members()
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
        self.client.get_members()
        self.assertEqual(self.last_request().headers['Authorization'], 'Bearer test-key')

    def test_json_writes_declare_json_content_type(self):
        self.respond_with(payload={'id': 1})
        self.client.create_attendance(1, 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
        self.assertEqual(self.last_request().headers['Content-Type'], 'application/json')


class TestListEndpoints(D4hClientTestCase):

    LIST_METHODS = {
        'get_qualifications': 'member-qualifications',
        'get_member_qualifications': 'member-qualification-awards',
        'get_members': 'members',
        'get_events': 'events',
        'get_exercises': 'exercises',
        'get_incidents': 'incidents',
        'get_incident_involved_persons': 'incident-involved-persons',
        'get_member_group_memberships': 'member-group-memberships',
        'get_location_bookmarks': 'location-bookmarks',
        'get_tags': 'tags',
    }

    def test_each_list_method_gets_its_endpoint_and_returns_results(self):
        records = [{'id': 1}, {'id': 2}]
        for method_name, path in self.LIST_METHODS.items():
            with self.subTest(method=method_name):
                self.sent.clear()
                self.respond_with(payload=list_payload(records))

                result = getattr(self.client, method_name)()

                self.assert_request('GET', path)
                self.assertEqual(result, records)

    def test_list_requests_ask_for_the_configured_page_size(self):
        self.client.get_members()
        self.assert_request('GET', 'members', query={'size': '250'})

    def test_page_size_defaults_to_10000(self):
        client = D4hClient('test-key', team_id=TEAM_ID, base_url=BASE_URL)
        client.get_members()
        self.assert_request('GET', 'members', query={'size': '10000'})


class TestDuties(D4hClientTestCase):

    def test_get_duties_sends_date_and_member_filters(self):
        self.client.get_duties(after='2026-01-01T00:00:00Z', before='2026-02-01T00:00:00Z', member_id=17)
        self.assert_request('GET', 'duties', query={
            'size': str(DEFAULT_PAGE_SIZE),
            'after': '2026-01-01T00:00:00Z',
            'before': '2026-02-01T00:00:00Z',
            'memberId': '17',
        })

    def test_get_duties_without_filters_sends_only_page_size(self):
        self.client.get_duties()
        self.assert_request('GET', 'duties', query={'size': str(DEFAULT_PAGE_SIZE)})

    def test_get_duties_filters_role_client_side(self):
        self.respond_with(payload=list_payload([
            {'id': 1, 'role': {'id': 501}},
            {'id': 2, 'role': {'id': 99}},
            {'id': 3},
        ]))

        result = self.client.get_duties(role_id=501)

        # The API has no role filter, so nothing about the role goes over the wire.
        self.assert_request('GET', 'duties', query={'size': str(DEFAULT_PAGE_SIZE)})
        self.assertEqual([duty['id'] for duty in result], [1])

    def test_create_duty(self):
        self.respond_with(payload={'id': 5})

        result = self.client.create_duty(17, 501, 'ON', '2026-01-01T00:00:00Z', '2026-01-08T00:00:00Z',
                                         notes='week 1')

        self.assert_request('POST', 'duties/add-duty-period', body={
            'memberId': 17,
            'roleId': 501,
            'type': 'ON',
            'startsAt': '2026-01-01T00:00:00Z',
            'endsAt': '2026-01-08T00:00:00Z',
            'notes': 'week 1',
        })
        self.assertEqual(result, {'id': 5})

    def test_create_duty_defaults_to_empty_notes(self):
        self.respond_with(payload={'id': 5})
        self.client.create_duty(17, 501, 'OFF', '2026-01-01T00:00:00Z', '2026-01-08T00:00:00Z')
        self.assertEqual(json.loads(self.last_request().body)['notes'], '')


class TestAttendance(D4hClientTestCase):

    def test_get_attendance_sends_every_filter(self):
        self.client.get_attendance(
            activity_resource_type='Exercise',
            status='ATTENDING',
            member_id=17,
            activity_id=42,
            starts_after='2026-01-01T00:00:00Z',
            starts_before='2026-02-01T08:30:00Z',
        )
        self.assert_request('GET', 'attendance', query={
            'size': str(DEFAULT_PAGE_SIZE),
            'activity_resource_type': 'Exercise',
            'status': 'ATTENDING',
            'member_id': '17',
            'activity_id': '42',
            'starts_after': '2026-01-01T00:00:00Z',
            'starts_before': '2026-02-01T08:30:00Z',
        })

    def test_get_attendance_sends_dates_exactly_as_given(self):
        # Normalising date formats is the caller's job; the client does not reinterpret them.
        self.client.get_attendance(starts_after='2026-01-01', starts_before='2026-02-01')
        self.assert_request('GET', 'attendance', query={
            'size': str(DEFAULT_PAGE_SIZE),
            'starts_after': '2026-01-01',
            'starts_before': '2026-02-01',
        })

    def test_get_attendance_without_filters_sends_only_page_size(self):
        self.client.get_attendance()
        self.assert_request('GET', 'attendance', query={'size': str(DEFAULT_PAGE_SIZE)})

    def test_get_attendance_returns_results(self):
        records = [{'id': 30001, 'status': 'ATTENDING'}]
        self.respond_with(payload=list_payload(records))
        self.assertEqual(self.client.get_attendance(activity_id=42), records)

    def test_create_attendance(self):
        self.respond_with(payload={'id': 30001})

        result = self.client.create_attendance(17, 42, '2026-01-01T18:00:00Z', '2026-01-01T21:00:00Z')

        self.assert_request('POST', 'attendance', body={
            'memberId': 17,
            'activityId': 42,
            'startsAt': '2026-01-01T18:00:00Z',
            'endsAt': '2026-01-01T21:00:00Z',
            'status': 'ATTENDING',
        })
        self.assertEqual(result, {'id': 30001})

    def test_create_attendance_with_explicit_status(self):
        self.respond_with(payload={'id': 30001})
        self.client.create_attendance(17, 42, '2026-01-01T18:00:00Z', '2026-01-01T21:00:00Z', status='ABSENT')
        self.assertEqual(json.loads(self.last_request().body)['status'], 'ABSENT')

    def test_delete_attendance(self):
        self.respond_with(status=204)

        result = self.client.delete_attendance(30001)

        request = self.assert_request('DELETE', 'attendance/30001')
        self.assertIsNone(request.body)
        self.assertIs(result, True)


class TestActivities(D4hClientTestCase):
    """Exercises and events share a payload shape; each case runs against both."""

    CREATORS = {'create_exercise': 'exercises', 'create_event': 'events'}
    UPDATERS = {'update_exercise': 'exercises', 'update_event': 'events'}
    TAGGERS = {
        'add_tags_to_exercise': 'exercises',
        'add_tags_to_event': 'events',
        'add_tags_to_incident': 'incidents',
    }

    def test_create_with_required_fields_only(self):
        for method_name, path in self.CREATORS.items():
            with self.subTest(method=method_name):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                result = getattr(self.client, method_name)(
                    'Rope Rescue Practice', '2026-01-06T02:00:00Z', '2026-01-06T05:00:00Z')

                # Only what the caller supplied is sent; D4H applies its own defaults
                # for everything else (including fullTeam).
                self.assert_request('POST', path, body={
                    'referenceDescription': 'Rope Rescue Practice',
                    'startsAt': '2026-01-06T02:00:00Z',
                    'endsAt': '2026-01-06T05:00:00Z',
                })
                self.assertEqual(result, {'id': 359312})

    def test_create_with_all_optional_fields(self):
        for method_name, path in self.CREATORS.items():
            with self.subTest(method=method_name):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                getattr(self.client, method_name)(
                    'Rope Rescue Practice', '2026-01-06T02:00:00Z', '2026-01-06T05:00:00Z',
                    description='Annual recert', reference='REF-9', tracking_number='26-1003',
                    plan='Stations A-C', location_bookmark_id=703, full_team=False)

                self.assert_request('POST', path, body={
                    'referenceDescription': 'Rope Rescue Practice',
                    'startsAt': '2026-01-06T02:00:00Z',
                    'endsAt': '2026-01-06T05:00:00Z',
                    'fullTeam': False,
                    'description': 'Annual recert',
                    'reference': 'REF-9',
                    'trackingNumber': '26-1003',
                    'plan': 'Stations A-C',
                    'locationBookmarkId': 703,
                })

    def test_create_sends_full_team_true_when_asked(self):
        for method_name, path in self.CREATORS.items():
            with self.subTest(method=method_name):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                getattr(self.client, method_name)(
                    'Rope Rescue Practice', '2026-01-06T02:00:00Z', '2026-01-06T05:00:00Z', full_team=True)

                self.assertIs(json.loads(self.last_request().body)['fullTeam'], True)

    def test_create_rejects_unknown_fields(self):
        for method_name in self.CREATORS:
            with self.subTest(method=method_name):
                with self.assertRaises(TypeError):
                    getattr(self.client, method_name)(
                        'Rope Rescue Practice', '2026-01-06T02:00:00Z', '2026-01-06T05:00:00Z', colour='red')
                self.assertEqual(self.sent, [])

    def test_update_patches_the_activity_with_the_given_fields(self):
        for method_name, path in self.UPDATERS.items():
            with self.subTest(method=method_name):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                result = getattr(self.client, method_name)(359312, {'description': 'Corrected'})

                self.assert_request('PATCH', f'{path}/359312', body={'description': 'Corrected'})
                self.assertEqual(result, {'id': 359312})

    def test_add_tags(self):
        for method_name, path in self.TAGGERS.items():
            with self.subTest(method=method_name):
                self.sent.clear()
                self.respond_with(payload={'id': 359312})

                getattr(self.client, method_name)(359312, [5841, 5970])

                self.assert_request('POST', f'{path}/359312/tags', body={'tagIds': [5841, 5970]})


class TestQualifications(D4hClientTestCase):

    def test_add_member_qualification_with_expiry(self):
        self.respond_with(payload={'id': 900})

        result = self.client.add_member_qualification(17, 602, '2026-01-01T00:00:00Z',
                                                      expires='2029-01-01T00:00:00Z')

        self.assert_request('POST', 'member-qualification-awards', body={
            'memberId': 17,
            'qualificationId': 602,
            'startsAt': '2026-01-01T00:00:00Z',
            'endsAt': '2029-01-01T00:00:00Z',
        })
        self.assertEqual(result, {'id': 900})

    def test_add_member_qualification_without_expiry_omits_ends_at(self):
        self.respond_with(payload={'id': 900})

        self.client.add_member_qualification(17, 602, '2026-01-01T00:00:00Z')

        self.assert_request('POST', 'member-qualification-awards', body={
            'memberId': 17,
            'qualificationId': 602,
            'startsAt': '2026-01-01T00:00:00Z',
        })


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

    def test_upload_sends_multipart_form_with_file_and_required_fields(self):
        result = self.client.upload_document(self.file_path, 'MemberQualificationAward', 900)

        request = self.assert_request('POST', 'documents')
        self.assertEqual(self.multipart_fields(request), {
            'targetResourceType': b'MemberQualificationAward',
            'targetResourceId': b'900',
            'restricted': b'UNRESTRICTED',
            'file': b'%PDF-fake certificate bytes',
        })
        self.assertEqual(request.headers['Authorization'], 'Bearer test-key')
        self.assertEqual(result, {'id': 77})

    def test_upload_includes_optional_fields_when_given(self):
        self.client.upload_document(self.file_path, 'MemberQualificationAward', 900, title='First Aid',
                                    description='Scanned card', container_id=12, restricted='RESTRICTED')

        fields = self.multipart_fields(self.last_request())
        self.assertEqual(fields['title'], b'First Aid')
        self.assertEqual(fields['description'], b'Scanned card')
        self.assertEqual(fields['containerId'], b'12')
        self.assertEqual(fields['restricted'], b'RESTRICTED')


class TestResponseHandling(D4hClientTestCase):

    def test_error_status_raises_http_error_with_context(self):
        self.respond_with(status=422, body=b'{"error":"startsAt is required"}')

        with self.assertRaises(requests.HTTPError) as caught:
            self.client.create_attendance(17, 42, None, None)

        message = str(caught.exception)
        self.assertIn('POST', message)
        self.assertIn(f'team/{TEAM_ID}/attendance', message)
        self.assertIn('422', message)
        self.assertIn('startsAt is required', message)
        self.assertEqual(caught.exception.response.status_code, 422)

    def test_error_status_on_a_read_also_raises(self):
        self.respond_with(status=401, body=b'Unauthorized')
        with self.assertRaises(requests.HTTPError):
            self.client.get_members()

    def test_server_error_raises(self):
        self.respond_with(status=503, body=b'Service Unavailable')
        with self.assertRaises(requests.HTTPError):
            self.client.get_tags()

    def test_no_content_response_returns_true(self):
        self.respond_with(status=204)
        self.assertIs(self.client.delete_attendance(1), True)

    def test_success_with_empty_body_returns_true(self):
        self.respond_with(status=200, body=b'')
        self.assertIs(self.client.add_tags_to_exercise(1, [2]), True)

    def test_json_body_is_returned_parsed(self):
        self.respond_with(status=201, payload={'id': 9, 'resourceType': 'Exercise'})
        result = self.client.create_exercise('Ref', '2026-01-06T02:00:00Z', '2026-01-06T05:00:00Z')
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

        result = self.client.get_members()

        self.assertEqual(ids(result), [1, 2, 3, 4, 5])
        # The first request is unchanged from the single-request client; later ones add `page`.
        self.assertEqual(self.queries(), [{'size': '2'}, {'size': '2', 'page': '1'}, {'size': '2', 'page': '2'}])
        self.assertEqual({urlsplit(request.url).path for request in self.sent}, {f"{TEAM_PATH}/members"})

    def test_a_list_that_fits_in_one_page_takes_one_request(self):
        self.queue(make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=2)))
        self.assertEqual(ids(self.client.get_members()), [1, 2])
        self.assertEqual(len(self.sent), 1)

    def test_an_empty_list_takes_one_request(self):
        self.queue(make_response(payload=list_payload([])))
        self.assertEqual(self.client.get_members(), [])
        self.assertEqual(len(self.sent), 1)

    def test_stops_at_an_empty_page_even_if_total_size_promised_more(self):
        # Rows deleted while paging: never loop waiting for records that no longer exist.
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=5)),
            make_response(payload=list_payload([], total=0, page=1)),
        )
        self.assertEqual(ids(self.client.get_members()), [1, 2])
        self.assertEqual(len(self.sent), 2)

    def test_without_total_size_stops_at_the_first_short_page(self):
        self.queue(
            make_response(payload={'results': [{'id': 1}, {'id': 2}]}),
            make_response(payload={'results': [{'id': 3}]}),
        )
        self.assertEqual(ids(self.client.get_members()), [1, 2, 3])
        self.assertEqual(len(self.sent), 2)

    def test_negative_total_size_means_unknown_so_paging_continues(self):
        # D4H's search endpoint reports totalSize -1.
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=-1)),
            make_response(payload=list_payload([{'id': 3}, {'id': 4}], total=-1, page=1)),
            make_response(payload=list_payload([], total=-1, page=2)),
        )
        self.assertEqual(ids(self.client.get_members()), [1, 2, 3, 4])
        self.assertEqual(len(self.sent), 3)

    def test_attendance_filters_are_sent_with_every_page(self):
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=3)),
            make_response(payload=list_payload([{'id': 3}], total=3, page=1)),
        )

        result = self.client.get_attendance(status='ATTENDING', member_id=17)

        self.assertEqual(ids(result), [1, 2, 3])
        self.assertEqual(self.queries(), [
            {'size': '2', 'status': 'ATTENDING', 'member_id': '17'},
            {'size': '2', 'status': 'ATTENDING', 'member_id': '17', 'page': '1'},
        ])

    def test_duties_role_filter_applies_across_pages(self):
        self.queue(
            make_response(payload=list_payload([{'id': 1, 'role': {'id': 501}}, {'id': 2, 'role': {'id': 99}}],
                                               total=3)),
            make_response(payload=list_payload([{'id': 3, 'role': {'id': 501}}], total=3, page=1)),
        )
        self.assertEqual(ids(self.client.get_duties(role_id=501)), [1, 3])

    def test_an_error_on_a_later_page_raises_rather_than_returning_a_partial_list(self):
        self.queue(
            make_response(payload=list_payload([{'id': 1}, {'id': 2}], total=5)),
            make_response(status=500, body=b'boom'),
        )
        with self.assertRaises(requests.HTTPError):
            self.client.get_members()


class TestTimeoutsAndRetries(D4hClientTestCase):

    def test_every_request_has_a_timeout(self):
        self.client.get_members()
        self.respond_with(payload={'id': 1})
        self.client.create_attendance(1, 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
        self.assertEqual([kwargs['timeout'] for kwargs in self.send_kwargs], [60, 60])

    def test_timeout_is_configurable(self):
        make_client(timeout=5).get_members()
        self.assertEqual(self.send_kwargs[0]['timeout'], 5)

    def test_get_is_retried_after_a_gateway_error(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.sent.clear()
                self.sleeps.clear()
                self.queue(make_response(status=status, body=b'unavailable'),
                           make_response(payload=list_payload([{'id': 1}])))

                self.assertEqual(ids(self.client.get_members()), [1])
                self.assertEqual(len(self.sent), 2)
                self.assertEqual(self.sleeps, [1.0])

    def test_retry_wait_doubles_each_attempt(self):
        self.queue(*[make_response(status=503) for _ in range(3)], make_response(payload=list_payload([])))
        self.client.get_members()
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0])

    def test_gives_up_after_max_retries_and_raises(self):
        self.respond_with(status=503, body=b'Service Unavailable')

        with self.assertRaises(requests.HTTPError) as caught:
            self.client.get_members()

        self.assertEqual(caught.exception.response.status_code, 503)
        self.assertEqual(len(self.sent), 4)  # the original attempt plus three retries
        self.assertEqual(len(self.sleeps), 3)

    def test_retries_can_be_disabled(self):
        self.respond_with(status=503)
        with self.assertRaises(requests.HTTPError):
            make_client(max_retries=0).get_members()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sleeps, [])

    def test_writes_are_not_retried_after_a_gateway_error(self):
        # The write may have been applied before the gateway failed; resending could duplicate it.
        self.respond_with(status=503)
        calls = [
            lambda: self.client.create_attendance(1, 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z'),
            lambda: self.client.update_event(9, {'description': 'x'}),
            lambda: self.client.delete_attendance(9),
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
                    self.client.get_members()
                self.assertEqual(len(self.sent), 1)

    def test_rate_limited_requests_are_retried_for_any_method(self):
        # 429 means D4H rejected the request before acting on it, so a write is safe to resend.
        self.queue(make_response(status=429, headers={'Retry-After': '7'}), make_response(payload={'id': 30001}))

        result = self.client.create_attendance(17, 42, '2026-01-01T18:00:00Z', '2026-01-01T21:00:00Z')

        self.assertEqual(result, {'id': 30001})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0].body, self.sent[1].body)
        self.assertEqual(self.sleeps, [7.0])

    def test_retry_after_is_capped_and_non_numeric_values_fall_back_to_backoff(self):
        self.queue(make_response(status=429, headers={'Retry-After': '600'}),
                   make_response(status=429, headers={'Retry-After': 'Wed, 11 Nov 2026 20:00:00 GMT'}),
                   make_response(payload=list_payload([])))
        self.client.get_members()
        self.assertEqual(self.sleeps, [60, 2.0])

    def test_get_is_retried_after_a_dropped_connection_or_timeout(self):
        for error in (requests.ConnectionError('reset'), requests.Timeout('slow')):
            with self.subTest(error=type(error).__name__):
                self.sent.clear()
                self.queue(error, make_response(payload=list_payload([{'id': 1}])))
                self.assertEqual(ids(self.client.get_members()), [1])
                self.assertEqual(len(self.sent), 2)

    def test_connection_errors_propagate_once_retries_run_out(self):
        self.queue(*[requests.ConnectionError('reset') for _ in range(4)])
        with self.assertRaises(requests.ConnectionError):
            self.client.get_members()
        self.assertEqual(len(self.sent), 4)

    def test_writes_are_not_retried_after_a_dropped_connection(self):
        self.queue(requests.ConnectionError('reset'))
        with self.assertRaises(requests.ConnectionError):
            self.client.create_attendance(1, 2, '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
        self.assertEqual(len(self.sent), 1)

    def test_a_retried_upload_resends_the_whole_file(self):
        handle = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        handle.write(b'%PDF-fake certificate bytes')
        handle.close()
        self.addCleanup(os.remove, handle.name)
        self.queue(make_response(status=429), make_response(payload={'id': 77}))

        self.client.upload_document(handle.name, 'MemberQualificationAward', 900)

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
        self.assertEqual(self.client.get_members(), [{'id': 1, 'name': 'Alex Example'}])
        self.assertEqual(self.sent, [])

    def test_each_list_method_reads_its_file(self):
        files = {
            'get_qualifications': 'qualifications',
            'get_member_qualifications': 'member_qualifications',
            'get_members': 'members',
            'get_events': 'events',
            'get_exercises': 'exercises',
            'get_incidents': 'incidents',
            'get_incident_involved_persons': 'incident_involved_persons',
            'get_member_group_memberships': 'member_groups',
            'get_location_bookmarks': 'location_bookmarks',
            'get_tags': 'tags',
            'get_duties': 'duties',
            'get_attendance': 'attendance',
        }
        for method_name, file_name in files.items():
            with self.subTest(method=method_name):
                self.write(file_name, [{'id': file_name}])
                self.assertEqual(getattr(self.client, method_name)(), [{'id': file_name}])
        self.assertEqual(self.sent, [])

    def test_generic_list_all_reads_a_file_named_after_the_endpoint(self):
        self.write('custom_fields', [{'id': 8}])
        self.assertEqual(self.client.list_all('custom-fields'), [{'id': 8}])
        self.assertEqual(self.sent, [])

    def test_get_duties_filters_role(self):
        self.assertEqual(ids(self.client.get_duties()), [1, 2, 3])
        self.assertEqual(ids(self.client.get_duties(role_id=501)), [1])

    def test_get_attendance_applies_each_filter(self):
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
                self.assertEqual(ids(self.client.get_attendance(**filters)), expected)
        self.assertEqual(self.sent, [])

    def test_writes_still_go_to_the_api(self):
        # Only reads are local. Callers that must not write use a dry run in the layer above.
        result = self.client.create_attendance(17, 42, '2026-01-01T18:00:00Z', '2026-01-01T21:00:00Z')

        self.assertEqual(result, {'id': 30001})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].method, 'POST')
        self.assertEqual(self.sent[0].url, f"{BASE_URL}team/{TEAM_ID}/attendance")


if __name__ == '__main__':
    unittest.main()
