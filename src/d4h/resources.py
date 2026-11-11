"""Generic access to every D4H collection, e.g. `client.equipment.list()`.

Each collection in the D4H v3 API follows the same shape:

    GET    <collection>                  -> Resource.list(**filters)   (every page)
    POST   <collection>                  -> Resource.create(data)
    GET    <collection>/<id>             -> Resource.get(id, **params)
    PATCH  <collection>/<id>             -> Resource.update(id, data)
    PUT    <collection>/<id>             -> Resource.replace(id, data)
    DELETE <collection>/<id>             -> Resource.delete(id)
    any    <collection>/<id>/<action>    -> Resource.action(action, id=id, ...)
    any    <collection>/<action>         -> Resource.action(action, ...)
    GET    <collection>/<id>/<related>   -> Resource.list_related(id, related)  (every page)

A few endpoints return one object rather than a list (team settings, modules,
whoami, ...); those are `Endpoint`s with just `get(**params)`.

Request bodies are passed through as given, using the API's own camelCase
field names; see https://api.team-manager.us.d4h.com/v3/docs for each one.
Not every collection supports every method: D4H answers an unsupported one
with an error status, which is raised as requests.HTTPError.
"""


class Resource:
    """One D4H collection, bound to a client. Created by `collection` below."""

    def __init__(self, client, path, max_page_size=None):
        self._client = client
        self.path = path
        self.max_page_size = max_page_size

    def __repr__(self):
        return f"<Resource {self.path}>"

    def _page_size(self):
        if self.max_page_size is None:
            return None
        return min(self._client.page_size, self.max_page_size)

    def _item(self, item_id):
        return f"{self.path}/{item_id}"

    def list(self, **filters):
        """Every record in the collection, following pages. Filters are query parameters."""
        return self._client.list_all(self.path, filters or None, page_size=self._page_size())

    def get(self, item_id, **params):
        return self._client.get(self._item(item_id), params or None)

    def list_related(self, item_id, related, **filters):
        """Every record in a list under one record, e.g. a document's revisions."""
        return self._client.list_all(f"{self._item(item_id)}/{related}", filters or None)

    def create(self, data=None, files=None):
        """POST a new record. With `files`, `data` is sent as multipart form fields."""
        return self._client.post(self.path, data, files=files)

    def update(self, item_id, data=None, files=None):
        """PATCH the given fields of a record."""
        return self._client.patch(self._item(item_id), data, files=files)

    def replace(self, item_id, data=None, files=None):
        """PUT a record (D4H uses this to upload a new revision of a document)."""
        return self._client.put(self._item(item_id), data, files=files)

    def delete(self, item_id):
        return self._client.delete(self._item(item_id))

    def action(self, action, *, item_id=None, method='POST', data=None, params=None):
        """Call a sub-path of the collection or of one record.

        Examples:
            client.events.action('publish', item_id=42)
            client.members.action('retire', item_id=17, method='PATCH', data={...})
            client.documents.action('download', item_id=9, method='GET')  # bytes
            client.equipment_brands.action('merge', data={...})
        """
        path = self._item(item_id) if item_id is not None else self.path
        return self._client.request(method, self._client._team_endpoint(f"{path}/{action}"),
                                    params=params, data=data)


class Endpoint:
    """A team endpoint that returns a single object rather than a list."""

    def __init__(self, client, path):
        self._client = client
        self.path = path

    def __repr__(self):
        return f"<Endpoint {self.path}>"

    def get(self, **params):
        return self._client.get(self.path, params or None)


class collection:
    """Descriptor that exposes a D4H collection or endpoint as a client attribute."""

    def __init__(self, path, kind=Resource, **options):
        self.path = path
        self.kind = kind
        self.options = options

    def __get__(self, client, owner=None):
        if client is None:
            return self
        return self.kind(client, self.path, **self.options)


# Attribute name -> API path, for every team-scoped collection in the D4H v3 API.
COLLECTIONS = {
    'animal_attendance': 'animal-attendance',
    'animal_group_memberships': 'animal-group-memberships',
    'animal_groups': 'animal-groups',
    'animal_qualifications': 'animal-qualifications',
    'animals': 'animals',
    'attendance': 'attendance',
    'contacts': 'contacts',
    'custom_field_options': 'custom-field-options',
    'custom_fields': 'custom-fields',
    'documents': 'documents',
    'duties': 'duties',
    'equipment': 'equipment',
    'equipment_brands': 'equipment-brands',
    'equipment_categories': 'equipment-categories',
    'equipment_funds': 'equipment-funds',
    'equipment_inspection_results': 'equipment-inspection-results',
    'equipment_inspection_step_results': 'equipment-inspection-step-results',
    'equipment_inspection_steps': 'equipment-inspection-steps',
    'equipment_inspections': 'equipment-inspections',
    'equipment_kinds': 'equipment-kinds',
    'equipment_locations': 'equipment-locations',
    'equipment_models': 'equipment-models',
    'equipment_retired_reasons': 'equipment-retired-reasons',
    'equipment_supplier_refs': 'equipment-supplier-refs',
    'equipment_suppliers': 'equipment-suppliers',
    'equipment_usages': 'equipment-usages',
    'events': 'events',
    'exercises': 'exercises',
    'handler_group_memberships': 'handler-group-memberships',
    'handler_groups': 'handler-groups',
    'handler_qualifications': 'handler-qualifications',
    'handlers': 'handlers',
    'health_safety_categories': 'health-safety-categories',
    'health_safety_reports': 'health-safety-reports',
    'health_safety_severities': 'health-safety-severities',
    'incident_involved_injuries': 'incident-involved-injuries',
    'incident_involved_persons': 'incident-involved-persons',
    'incidents': 'incidents',
    'location_bookmarks': 'location-bookmarks',
    'member_custom_statuses': 'member-custom-statuses',
    'member_group_memberships': 'member-group-memberships',
    'member_groups': 'member-groups',
    'member_qualification_awards': 'member-qualification-awards',
    'member_qualifications': 'member-qualifications',
    'member_retired_reasons': 'member-retired-reasons',
    'members': 'members',
    'organisations': 'organisations',
    'repairs': 'repairs',
    'resource_bundles': 'resource-bundles',
    'roles': 'roles',
    'search': 'search',
    'tags': 'tags',
    'task_assignments': 'task-assignments',
    'tasks': 'tasks',
    'teams': 'teams',
    'whiteboard': 'whiteboard',
}


# Attribute name -> API path, for team endpoints that return a single object.
ENDPOINTS = {
    'customer_identifiers': 'customer-identifiers',
    'incident_involved_metadata': 'incident-involved-metadata',
    'modules': 'modules',
    'settings': 'settings',
    'team_whoami': 'whoami',
}


class CollectionsMixin:
    """Adds one attribute per entry in COLLECTIONS and ENDPOINTS."""


# Lists whose `size` is capped below the client's page size (from the API spec).
MAX_PAGE_SIZES = {
    'search': 100,
}

for _name, _path in COLLECTIONS.items():
    setattr(CollectionsMixin, _name, collection(_path, max_page_size=MAX_PAGE_SIZES.get(_path)))
for _name, _path in ENDPOINTS.items():
    setattr(CollectionsMixin, _name, collection(_path, Endpoint))
del _name, _path
