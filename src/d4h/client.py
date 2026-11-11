"""Thin client for the D4H Team Manager v3 API.

This package knows how to talk to D4H and nothing about any particular team: the
team ID, API base URL and page size are passed in, and each method maps its
arguments one-to-one onto API fields. Team-specific defaults and conventions
belong in the application that uses it.
"""
import logging
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
    `client.members.list()`, `client.equipment.get(12)` or
    `client.tasks.create({...})`. Request bodies and filters use the API's own
    names, as listed in the D4H API docs.
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
