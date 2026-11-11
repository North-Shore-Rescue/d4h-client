"""D4H client that reads list data from saved JSON files instead of the API."""
import json
import os

from .client import D4hClient


class LocalD4hClient(D4hClient):
    """Serve every list from `<data_dir>/<name>.json`, a saved D4H list response.

    Used for offline analysis and tests. Only reads are local: write methods
    (create, update, delete, upload) still call the API.
    """

    # List endpoints whose file is not simply the endpoint with underscores.
    FILE_NAMES = {
        'member-qualifications': 'qualifications',
        'member-qualification-awards': 'member_qualifications',
        'member-group-memberships': 'member_groups',
    }

    def __init__(self, api_key, *, data_dir='data', **settings):
        super().__init__(api_key, **settings)
        self.data_dir = data_dir

    def list_all(self, path, params=None):
        """Return the saved results for `path`. `params` are not applied to local data."""
        name = self.FILE_NAMES.get(path, path.replace('-', '_'))
        with open(os.path.join(self.data_dir, f'{name}.json'), 'r') as file:
            return json.load(file)['results']

    def get_attendance(self, activity_resource_type=None, status=None, member_id=None,
                       activity_id=None, starts_after=None, starts_before=None):
        """Attendance from the saved file, with the API's filters applied here instead."""
        results = self.list_all('attendance')
        if activity_resource_type:
            results = [r for r in results if r.get('activity', {}).get('resourceType') == activity_resource_type]
        if status:
            results = [r for r in results if r.get('status') == status]
        if member_id:
            results = [r for r in results if r.get('member', {}).get('id') == member_id]
        if activity_id:
            results = [r for r in results if r.get('activity', {}).get('id') == activity_id]
        if starts_after:
            results = [r for r in results if r.get('startsAt', '') >= starts_after]
        if starts_before:
            results = [r for r in results if r.get('startsAt', '') <= starts_before]
        return results
