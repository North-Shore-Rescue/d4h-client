"""Thin client for the D4H Team Manager v3 API.

This package knows how to talk to D4H and nothing about any particular team: the
team ID, API base URL and page size are passed in, and each method maps its
arguments one-to-one onto API fields. Team-specific defaults and conventions
belong in the application that uses it.
"""
import logging
import os
import time

import requests

from .resources import CollectionsMixin

logger = logging.getLogger(__name__)

# 429 means the request was rejected before D4H acted on it, so resending is
# safe for any method. Gateway errors and dropped connections are only retried
# for GETs: a write may already have been applied.
RETRY_ANY_METHOD = frozenset({429})
RETRY_READS_ONLY = frozenset({502, 503, 504})
MAX_RETRY_WAIT_SECONDS = 60


class D4hClient(CollectionsMixin):
    """Client for one D4H team.

    Every collection in the API is an attribute (see d4h/resources.py), e.g.
    `client.equipment.list()` or `client.tasks.create({...})`. The named
    `get_*`/`create_*` methods below cover the most common calls.
    """

    def __init__(self, api_key, *, team_id, base_url, page_size=10000, timeout=60, max_retries=3,
                 retry_backoff=1.0):
        """
        Args:
            api_key (str): D4H API key, sent as a bearer token.
            team_id (int): ID of the team every request is scoped to.
            base_url (str): Regional API root including the version,
                e.g. "https://api.team-manager.ca.d4h.com/v3/".
            page_size (int): `size` requested per page from list endpoints.
                Lists are always fetched in full, however many pages that takes.
            timeout (float): Seconds to wait for D4H to connect or respond.
            max_retries (int): Extra attempts after a retryable failure (0 disables retrying).
            retry_backoff (float): Seconds before the first retry; doubles each attempt.
        """
        self.team_id = team_id
        self.base_url = base_url.rstrip('/') + '/'
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._session = requests.Session()
        self._session.headers['Authorization'] = f'Bearer {api_key}'

    # ── Transport ───────────────────────────────────────────────────────────

    def _team_endpoint(self, path):
        return f"team/{self.team_id}/{path}"

    def _request(self, method, endpoint, *, params=None, json=None, data=None, files=None):
        """Send one request (retrying where safe) and return the parsed response.

        Returns the decoded JSON body, or True for a successful response with no
        body (e.g. 204 from a DELETE). Raises requests.HTTPError for any error status.
        """
        url = f"{self.base_url}{endpoint}"
        attempt = 0
        while True:
            attempt += 1
            retries_left = attempt <= self.max_retries
            try:
                response = self._session.request(
                    method, url, params=params, json=json, data=data, files=files, timeout=self.timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if method != 'GET' or not retries_left:
                    raise
                self._wait_before_retry(method, endpoint, attempt, reason=type(exc).__name__)
                continue

            retryable = response.status_code in RETRY_ANY_METHOD or (
                method == 'GET' and response.status_code in RETRY_READS_ONLY
            )
            if retryable and retries_left:
                self._wait_before_retry(method, endpoint, attempt, reason=f"status {response.status_code}",
                                        retry_after=response.headers.get('Retry-After'))
                continue
            break

        if not response.ok:
            raise requests.HTTPError(
                f"D4H API {method} {endpoint} failed with {response.status_code}: {response.text}",
                response=response
            )
        # DELETE (and some other endpoints) return 204 / empty bodies
        if response.status_code == 204 or not response.content:
            return response.ok
        # Images and document downloads come back as file bytes.
        if 'json' not in response.headers.get('Content-Type', 'application/json'):
            return response.content
        return response.json()

    def _wait_before_retry(self, method, endpoint, attempt, reason, retry_after=None):
        delay = self.retry_backoff * 2 ** (attempt - 1)
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                pass  # an HTTP-date; fall back to our own backoff
        delay = min(delay, MAX_RETRY_WAIT_SECONDS)
        logger.warning("D4H API %s %s: %s, retrying in %.1fs (attempt %d of %d)",
                       method, endpoint, reason, delay, attempt, self.max_retries)
        time.sleep(delay)

    # ── Generic requests ────────────────────────────────────────────────────

    def request(self, method, endpoint, *, params=None, data=None, files=None):
        """Call any endpoint. `endpoint` is relative to the base URL, e.g. 'whoami'.

        `data` is sent as JSON, or as multipart form fields when `files` is given.
        Returns parsed JSON, file bytes for non-JSON responses, or True for an empty body.
        """
        if files:
            return self._request(method, endpoint, params=params, data=data, files=files)
        return self._request(method, endpoint, params=params, json=data)

    # The calls below take `path` relative to the team, e.g. 'members/123'.

    def get(self, path, params=None):
        """GET a team endpoint and return the parsed response."""
        return self._request('GET', self._team_endpoint(path), params=params)

    def list_all(self, path, params=None, page_size=None):
        """Return every record from a team list endpoint, following pages to the end.

        `page_size` overrides the client's page size for endpoints with a lower limit.
        """
        page_size = page_size or self.page_size
        query = {'size': page_size, **(params or {})}
        results = []
        page = 0
        while True:
            if page:
                query['page'] = page
            response = self.get(path, query)
            batch = response.get('results') if isinstance(response, dict) else None
            if not batch:
                break
            results.extend(batch)
            total = response.get('totalSize')
            if total is None or total < 0:  # search reports -1 when the total is unknown
                if len(batch) < page_size:
                    break
            elif len(results) >= total:
                break
            page += 1
        return results

    def post(self, path, data=None, files=None):
        """POST to a team endpoint: `data` as JSON, or as a multipart form when `files` is given."""
        return self.request('POST', self._team_endpoint(path), data=data, files=files)

    def patch(self, path, data=None, files=None):
        """PATCH a team endpoint: `data` as JSON, or as a multipart form when `files` is given."""
        return self.request('PATCH', self._team_endpoint(path), data=data, files=files)

    def put(self, path, data=None, files=None):
        """PUT to a team endpoint: `data` as JSON, or as a multipart form when `files` is given."""
        return self.request('PUT', self._team_endpoint(path), data=data, files=files)

    def delete(self, path):
        """DELETE a team endpoint."""
        return self._request('DELETE', self._team_endpoint(path))

    # ── Endpoints outside the team ──────────────────────────────────────────

    def whoami(self):
        """The account the API key belongs to."""
        return self.request('GET', 'whoami')

    def healthcheck(self):
        return self.request('GET', 'healthcheck')

    def mobile_app_versions(self):
        return self.request('GET', 'public/mobile-app-versions')

    def get_organisation_settings(self, organisation_id):
        return self.request('GET', f'organisation/{organisation_id}/settings')

    # ── Lists ───────────────────────────────────────────────────────────────

    def get_qualifications(self):
        return self.list_all('member-qualifications')

    def get_member_qualifications(self):
        return self.list_all('member-qualification-awards')

    def get_members(self):
        return self.list_all('members')

    def get_events(self):
        return self.list_all('events')

    def get_exercises(self):
        return self.list_all('exercises')

    def get_incidents(self):
        return self.list_all('incidents')

    def get_incident_involved_persons(self):
        return self.list_all('incident-involved-persons')

    def get_member_group_memberships(self):
        return self.list_all('member-group-memberships')

    def get_location_bookmarks(self):
        return self.list_all('location-bookmarks')

    def get_tags(self):
        return self.list_all('tags')

    # ── Qualifications and documents ────────────────────────────────────────

    def add_member_qualification(self, member_id, qualification_id, awarded, expires=None):
        data = {
            'memberId': member_id,
            'qualificationId': qualification_id,
            'startsAt': awarded
        }
        if expires is not None:
            data['endsAt'] = expires

        return self.post('member-qualification-awards', data)

    def upload_document(self, file_path, target_resource_type, target_resource_id,
                        title=None, description=None, container_id=None, restricted="UNRESTRICTED"):
        """
        Upload a document to D4H.

        :param file_path: Path to the file to be uploaded
        :param target_resource_type: Type of resource to attach the document to
        :param target_resource_id: ID of the resource to attach the document to
        :param title: Title of the document (optional)
        :param description: Description of the document (optional)
        :param container_id: ID of the container to place the document in (optional)
        :param restricted: Access restriction level (default: "UNRESTRICTED")
        :return: Response from the API
        """
        data = {
            'targetResourceType': target_resource_type,
            'targetResourceId': target_resource_id,
            'restricted': restricted
        }
        if title:
            data['title'] = title
        if description:
            data['description'] = description
        if container_id:
            data['containerId'] = container_id

        # Read the bytes up front rather than passing the open file, so a retry
        # resends the whole document instead of an exhausted file handle.
        with open(file_path, 'rb') as file:
            content = file.read()
        return self.post('documents', data, files={'file': (os.path.basename(file_path), content)})

    # ── Tags ────────────────────────────────────────────────────────────────

    def add_tags_to_incident(self, incident_id, tag_ids):
        return self._add_tags('incidents', incident_id, tag_ids)

    def add_tags_to_exercise(self, exercise_id, tag_ids):
        return self._add_tags('exercises', exercise_id, tag_ids)

    def add_tags_to_event(self, event_id, tag_ids):
        return self._add_tags('events', event_id, tag_ids)

    def _add_tags(self, activity_path, activity_id, tag_ids):
        return self.post(f'{activity_path}/{activity_id}/tags', {"tagIds": tag_ids})

    # ── Duties ──────────────────────────────────────────────────────────────

    def get_duties(self, after=None, before=None, role_id=None, member_id=None):
        """
        Get duties from D4H with optional filters.

        Args:
            after (str): Filter duties after this date (ISO 8601 format)
            before (str): Filter duties before this date (ISO 8601 format)
            role_id (int): Filter by role ID
            member_id (int): Filter by member ID

        Returns:
            list: List of duties matching the filters
        """
        params = {}
        if after:
            params['after'] = after
        if before:
            params['before'] = before
        if member_id:
            params['memberId'] = member_id
        results = self.list_all('duties', params)

        # role_id is not supported as an API filter — apply client-side
        if role_id is not None:
            results = [d for d in results if d.get('role', {}).get('id') == role_id]

        return results

    def create_duty(self, member_id, role_id, duty_type, starts_at, ends_at, notes=''):
        """
        Create a duty period for a member in D4H.

        Args:
            member_id (int): The member's ID
            role_id (int): The role ID for the duty
            duty_type (str): 'ON' or 'OFF'
            starts_at (str): ISO 8601 UTC start datetime
            ends_at (str): ISO 8601 UTC end datetime
            notes (str): Optional notes

        Returns:
            dict: API response
        """
        data = {
            'memberId': member_id,
            'roleId': role_id,
            'type': duty_type,
            'startsAt': starts_at,
            'endsAt': ends_at,
            'notes': notes,
        }
        return self.post('duties/add-duty-period', data)

    # ── Exercises and events ────────────────────────────────────────────────

    def create_exercise(self, reference_description, starts_at, ends_at, **fields):
        """Create an exercise. See `_activity_payload` for the optional fields."""
        return self.post('exercises', self._activity_payload(reference_description, starts_at, ends_at, **fields))

    def create_event(self, reference_description, starts_at, ends_at, **fields):
        """Create an event. See `_activity_payload` for the optional fields."""
        return self.post('events', self._activity_payload(reference_description, starts_at, ends_at, **fields))

    @staticmethod
    def _activity_payload(reference_description, starts_at, ends_at, *, description=None, reference=None,
                          tracking_number=None, plan=None, location_bookmark_id=None, full_team=None):
        """Build the create payload shared by exercises and events.

        Each optional argument maps to one API field and is left out of the
        request when not given, so D4H applies its own default.

        Args:
            reference_description (str): Activity title (`referenceDescription`).
            starts_at (str): ISO 8601 start datetime.
            ends_at (str): ISO 8601 end datetime.
            description (str): `description`.
            reference (str): `reference`.
            tracking_number (str): `trackingNumber`.
            plan (str): `plan`.
            location_bookmark_id (int): `locationBookmarkId`.
            full_team (bool): `fullTeam`. D4H enrols the whole team as
                attendance unless this is sent as False.
        """
        data = {
            'referenceDescription': reference_description,
            'startsAt': starts_at,
            'endsAt': ends_at,
        }
        if full_team is not None:
            data['fullTeam'] = full_team
        if description:
            data['description'] = description
        if reference:
            data['reference'] = reference
        if tracking_number:
            data['trackingNumber'] = tracking_number
        if plan:
            data['plan'] = plan
        if location_bookmark_id:
            data['locationBookmarkId'] = location_bookmark_id
        return data

    def update_exercise(self, exercise_id, data):
        return self.patch(f'exercises/{exercise_id}', data)

    def update_event(self, event_id, data):
        return self.patch(f'events/{event_id}', data)

    # ── Attendance ──────────────────────────────────────────────────────────

    def create_attendance(self, member_id, activity_id, starts_at, ends_at, status="ATTENDING"):
        data = {
            'memberId': member_id,
            'activityId': activity_id,
            'startsAt': starts_at,
            'endsAt': ends_at,
            'status': status,
        }
        return self.post('attendance', data)

    def delete_attendance(self, attendance_id):
        return self.delete(f'attendance/{attendance_id}')

    def get_attendance(self, activity_resource_type=None, status=None, member_id=None,
                       activity_id=None, starts_after=None, starts_before=None):
        """
        Get attendance records from D4H with optional filters.

        Args:
            activity_resource_type (str): Filter by activity type - 'Event', 'Exercise', or 'Incident'
            status (str): Filter by status - 'ABSENT', 'ATTENDING', or 'REQUESTED'
            member_id (int): Filter by member ID
            activity_id (int): Filter by activity ID
            starts_after (str): Filter attendance starting after this ISO 8601 datetime (sent as given)
            starts_before (str): Filter attendance starting before this ISO 8601 datetime (sent as given)

        Returns:
            list: List of attendance records
        """
        params = {}
        if activity_resource_type:
            params['activity_resource_type'] = activity_resource_type
        if status:
            params['status'] = status
        if member_id:
            params['member_id'] = member_id
        if activity_id:
            params['activity_id'] = activity_id
        if starts_after:
            params['starts_after'] = starts_after
        if starts_before:
            params['starts_before'] = starts_before
        return self.list_all('attendance', params)
