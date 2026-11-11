# d4h-client

A small Python client for the [D4H Team Manager v3 API](https://api.team-manager.us.d4h.com/v3/docs).

It knows how to talk to D4H and nothing about any particular team: you pass in your team ID and
region, and each method maps its arguments one-to-one onto API fields. Your team's own conventions
belong in your code.

Maintained by [North Shore Rescue](https://www.northshorerescue.com). Requires Python 3.9+ and
depends only on `requests`.

## Install

```sh
uv add "d4h-client @ git+https://github.com/North-Shore-Rescue/d4h-client@v0.1.0"
# or
pip install "git+https://github.com/North-Shore-Rescue/d4h-client@v0.1.0"
```

## Usage

```python
from d4h import D4hClient

client = D4hClient(
    api_key="...",
    team_id=123,
    base_url="https://api.team-manager.ca.d4h.com/v3/",  # use your region's host
)

members = client.get_members()
attendance = client.get_attendance(status="ATTENDING", starts_after="2026-01-01T00:00:00Z")

exercise = client.create_exercise(
    "Rope rescue practice", "2026-01-06T02:00:00Z", "2026-01-06T05:00:00Z",
    full_team=False,  # otherwise D4H enrols the whole team as attendance
)
```

## Every endpoint

Every collection in the API is an attribute of the client, named after its path with
underscores (`equipment-brands` → `client.equipment_brands`). Each has the same methods:

```python
client.equipment.list(status="OPERATIONAL")        # GET, every page; filters are query parameters
client.equipment.get(123)                           # GET one record
client.tasks.create({"title": "Check radios"})      # POST
client.tasks.update(5, {"title": "Check all radios"})  # PATCH
client.whiteboard.delete(9)                          # DELETE
client.documents.replace(9, {"title": "v2"}, files={"file": open("card.pdf", "rb")})  # PUT
```

Request bodies use the API's own field names, as listed in the
[API docs](https://api.team-manager.us.d4h.com/v3/docs).

Calls under a collection or a record use `action` (file downloads return bytes) and
`list_related` (every page):

```python
client.events.action("publish", item_id=42)
client.members.action("retire", item_id=17, method="PATCH", data={"direction": "RETIRE", "date": "..."})
client.equipment_brands.action("merge", data={...})
client.members.action("image", item_id=17, method="GET", params={"size": "THUMBNAIL"})   # bytes
client.documents.list_related(9, "revisions")
```

Search covers every kind of record at once:

```python
client.search.list(query="rope", resource_type=["Exercise", "Equipment"])
```

Endpoints that return a single object are `client.settings`, `client.modules`,
`client.customer_identifiers`, `client.incident_involved_metadata` and `client.team_whoami`,
each with `.get(**params)`. Outside the team there are `client.whoami()`, `client.healthcheck()`,
`client.mobile_app_versions()` and `client.get_organisation_settings(organisation_id)`.

For anything else, the generic calls take a path relative to your team, or `request` takes one
relative to the base URL:

```python
client.list_all("custom-fields")
client.get("members/123")
client.request("GET", "whoami")
```

A test checks every operation in D4H's published spec against the request the client sends.
When D4H adds endpoints, refresh the list with `uv run scripts/update_spec_operations.py`; the
tests then fail until the client covers them.

## Behaviour

- **Pagination:** list methods return every record, requesting further pages until `totalSize` is
  reached. `page_size` (default 10000) sets the rows per request.
- **Timeouts:** every request has a timeout (`timeout`, default 60 seconds).
- **Retries:** GETs are retried after a 502/503/504 or a dropped connection, and any request is
  retried after a 429 (rate limited), honouring `Retry-After`. Waits back off exponentially from
  `retry_backoff` seconds, for up to `max_retries` attempts (default 3). Other writes are never
  retried, because D4H may already have applied them.
- **Unknown totals:** a list that reports `totalSize` as -1 (search does) is read until a short or empty page.
- **Errors:** any error status raises `requests.HTTPError` with the method, endpoint, status and
  response body in the message.

## Offline data

`LocalD4hClient` reads lists from saved JSON files (D4H list responses with a `results` key) in a
directory instead of calling the API, which is useful for analysis and tests. Writes still go to
the API.

```python
from d4h import LocalD4hClient

client = LocalD4hClient("...", team_id=123, base_url="https://api.team-manager.ca.d4h.com/v3/",
                        data_dir="data")
client.get_members()   # reads data/members.json
```

## Development

```sh
uv sync
uv run python -m unittest discover -s tests -p 'test_*.py'
```

The tests intercept requests at `requests.Session.send` and check exactly what goes over the wire;
they never contact D4H.

## License

MIT. See [LICENSE](LICENSE).
