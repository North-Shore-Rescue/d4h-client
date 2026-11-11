"""Refresh tests/spec_operations.json from D4H's published OpenAPI spec.

Run with: uv run scripts/update_spec_operations.py
Then run the tests: any new endpoint fails tests/test_spec_coverage.py until the
client covers it.
"""
import json
import pathlib

import requests

SPEC_URL = "https://api.team-manager.us.d4h.com/v3/docs/swagger.json"
OUTPUT = pathlib.Path(__file__).resolve().parent.parent / 'tests' / 'spec_operations.json'
METHODS = ('get', 'post', 'put', 'patch', 'delete')


def main():
    spec = requests.get(SPEC_URL, timeout=60).json()
    operations = sorted(
        {(method.upper(), path) for path, item in spec['paths'].items() for method in item if method in METHODS},
        key=lambda op: (op[1], op[0]),
    )
    OUTPUT.write_text(json.dumps({
        'source': SPEC_URL,
        'api_version': spec['info']['version'],
        'operations': [{'method': method, 'path': path} for method, path in operations],
    }, indent=1) + '\n')
    print(f"Wrote {len(operations)} operations (API version {spec['info']['version']}) to {OUTPUT}")


if __name__ == '__main__':
    main()
