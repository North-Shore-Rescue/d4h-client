"""Work with D4H data saved as JSON files instead of the live API.

`save_lists` downloads collections into a directory, one file per collection,
and `LocalD4hClient` then serves reads from those files:

    save_lists(D4hClient(...), 'data')                  # refresh the saved data
    client = LocalD4hClient(..., data_dir='data')
    client.equipment.list()                             # reads data/equipment.json
    client.members.get(17)                              # finds member 17 in data/members.json
"""
import json
import logging
import os

import requests

from .client import D4hClient
from .resources import COLLECTIONS, ENDPOINTS

logger = logging.getLogger(__name__)

# Collections whose file is not simply the endpoint with underscores, kept for
# compatibility with data saved by earlier tooling.
FILE_NAMES = {
    'member-qualifications': 'qualifications',
    'member-qualification-awards': 'member_qualifications',
    'member-group-memberships': 'member_groups',
}

# Paths that can't be saved without arguments: search needs a query and
# modules needs a `setting`.
NOT_SAVED = {'search', 'modules'}


def file_name(path):
    """The saved file name (without .json) for a collection or endpoint path."""
    return FILE_NAMES.get(path, path.replace('-', '_'))


def _attendance_filter(records, params):
    """Apply the attendance list's API filters to saved records."""
    checks = {
        'activity_resource_type': lambda r, v: r.get('activity', {}).get('resourceType') == v,
        'status': lambda r, v: r.get('status') == v,
        'member_id': lambda r, v: r.get('member', {}).get('id') == v,
        'activity_id': lambda r, v: r.get('activity', {}).get('id') == v,
        'starts_after': lambda r, v: r.get('startsAt', '') >= v,
        'starts_before': lambda r, v: r.get('startsAt', '') <= v,
    }
    for name, value in params.items():
        if name in checks and value is not None:
            records = [r for r in records if checks[name](r, value)]
    return records


class LocalD4hClient(D4hClient):
    """Serve reads from `<data_dir>/<name>.json` files saved by `save_lists`.

    - `.list()` on any collection returns its saved records. Filters are applied
      to saved attendance; other lists are returned unfiltered.
    - `.get(id)` finds the record with that ID in the saved list.
    - Single-object endpoints (e.g. `client.settings.get()`) return their saved file.
    - Any other GET under the team raises FileNotFoundError rather than calling the API.

    Writes (create, update, delete, upload, actions) still go to the API.
    """

    FILTERS = {
        'attendance': _attendance_filter,
    }

    def __init__(self, api_key, *, data_dir='data', **settings):
        super().__init__(api_key, **settings)
        self.data_dir = data_dir

    def _file_for(self, path):
        candidates = [os.path.join(self.data_dir, f'{file_name(path)}.json'),
                      os.path.join(self.data_dir, f'{path}.json')]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(f"No saved data for '{path}': expected {candidates[0]}")

    def _load(self, path):
        with open(self._file_for(path), 'r') as file:
            return json.load(file)

    def list_all(self, path, params=None, page_size=None):
        """Return the saved results for `path`, filtered where supported."""
        results = self._load(path)['results']
        if params and path in self.FILTERS:
            results = self.FILTERS[path](results, params)
        return results

    def get(self, path, params=None):
        """Read `<collection>`, `<collection>/<id>` or a single-object endpoint from saved data."""
        parts = path.split('/')
        if len(parts) == 1:
            return self._load(path)
        if len(parts) == 2:
            collection, item_id = parts
            for record in self.list_all(collection):
                if str(record.get('id')) == item_id:
                    return record
            raise LookupError(f"No record {item_id} in saved '{collection}' data")
        raise FileNotFoundError(f"'{path}' is not available from saved data")


def save_lists(client, data_dir, collections=None):
    """Download collections from the API into `data_dir`, one JSON file each.

    Args:
        client: a D4hClient connected to the live API.
        data_dir: directory to write to; created if missing.
        collections: attribute names to save (e.g. ['members', 'attendance']);
            defaults to every collection and single-object endpoint the API key can read.

    Each list is saved as {"results": [...]}, the shape LocalD4hClient reads.
    Collections that fail (e.g. a module the team doesn't use) are skipped and
    reported. Returns {attribute name: record count, or the error message}.
    """
    if collections is None:
        collections = [name for name, path in {**COLLECTIONS, **ENDPOINTS}.items() if path not in NOT_SAVED]
    os.makedirs(data_dir, exist_ok=True)
    summary = {}
    for name in collections:
        if name in ENDPOINTS:
            path, is_list = ENDPOINTS[name], False
        elif name in COLLECTIONS:
            path, is_list = COLLECTIONS[name], True
        else:
            raise ValueError(f"Unknown collection '{name}'")
        try:
            if is_list:
                records = getattr(client, name).list()
                content, summary[name] = {'results': records}, len(records)
            else:
                content = getattr(client, name).get()
                summary[name] = 1
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 'error'
            logger.warning("Skipped %s: D4H returned %s", name, status)
            summary[name] = f"skipped ({status})"
            continue
        target = os.path.join(data_dir, f'{file_name(path)}.json')
        with open(target + '.tmp', 'w') as file:
            json.dump(content, file)
        os.replace(target + '.tmp', target)
    return summary
