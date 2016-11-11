import abc
import json
import logging

from formencode.validators import Invalid
import inflect

from old.lib.SQLAQueryBuilder import SQLAQueryBuilder, OLDSearchParseError
from old.lib.dbutils import DBUtils
import old.lib.helpers as h
import old.lib.schemata as old_schemata
import old.models as old_models


LOGGER = logging.getLogger(__name__)


class Resources(abc.ABC):
    """Abstract base class for all OLD resource views. RESTful CRUD(S)
    interface based on the Atom protocol:

    +-----------------+-------------+--------------------------+--------+
    | Purpose         | HTTP Method | Path                     | Method |
    +-----------------+-------------+--------------------------+--------+
    | Create new      | POST        | /<cllctn_name>           | create |
    | Create data     | GET         | /<cllctn_name>/new       | new    |
    | Read all        | GET         | /<cllctn_name>           | index  |
    | Read specific   | GET         | /<cllctn_name>/<id>      | show   |
    | Update specific | PUT         | /<cllctn_name>/<id>      | update |
    | Update data     | GET         | /<cllctn_name>/<id>/edit | edit   |
    | Delete specific | DELETE      | /<cllctn_name>/<id>      | delete |
    | Search          | SEARCH      | /<cllctn_name>           | search |
    +-----------------+-------------+--------------------------+--------+

    TODOs:

    - implement authorization control
    - allow for resource-specific authorization control over actions.
    """
    inflect_p = inflect.engine()
    inflect_p.classical()

    def __init__(self, request):
        self.request = request
        self._db = None
        # Names
        self.collection_name = self.__class__.__name__.lower()
        self.member_name = self.inflect_p.singular_noun(self.collection_name)
        self.model_name = self.member_name.capitalize()
        self.schema_cls_name = self.model_name + 'Schema'
        # Classes
        self.model_cls = getattr(old_models, self.model_name)
        self.schema_cls = getattr(old_schemata, self.schema_cls_name)

    @property
    def db(self):
        if not self._db:
            self._db = DBUtils(self.request.dbsession,
                               self.request.registry.settings)
        return self._db

    @property
    def query_builder(self):
        return SQLAQueryBuilder(
            self.request.dbsession, self.model_name,
            settings=self.request.registry.settings)

    @property
    def logged_in_user(self):
        user_dict = self.request.session['user']
        return self.request.dbsession.query(
            old_models.User).get(user_dict['id'])

    ###########################################################################
    # Public CRUD(S) Methods
    ###########################################################################

    # @h.authorize(['administrator', 'contributor'])
    def create(self):
        """Create a new resource and return it.
        :URL: ``POST /<resource_collection_name>``
        :request body: JSON object representing the resource to create.
        :returns: the newly created resource.
        """
        schema = self.schema_cls()
        try:
            values = json.loads(self.request.body.decode(self.request.charset))
        except ValueError:
            self.request.response.status_int = 400
            return self.JSONDecodeErrorResponse
        state = h.get_state_object(
            values=values,
            dbsession=self.request.dbsession,
            logged_in_user=self.request.session.get('user', {}))
        try:
            data = schema.to_python(values, state)
        except Invalid as error:
            self.request.response.status_int = 400
            return {'errors': error.unpack_errors()}
        resource = self._create_new_resource(data)
        self.request.dbsession.add(resource)
        self.request.dbsession.flush()
        self._post_create(resource)
        return resource

    # @h.authorize(['administrator', 'contributor'])
    def new(self):
        """Return the data necessary to create a new resource.
        :URL: ``GET /<resource_collection_name>/new``.
        :returns: a dict containing the related resources necessary to create a
            new resource of this type.
        """
        return {}

    def index(self):
        """Get all resources.
        :URL: ``GET /<resource_collection_name>`` with optional query string
            parameters for ordering and pagination.
        :returns: a JSON-serialized array of resources objects.
        .. note::

           See :func:`utils.add_order_by` and :func:`utils.add_pagination` for
           the query string parameters that effect ordering and pagination.
        """
        query = self._eagerload_model(
            self.request.dbsession.query(self.model_cls))
        get_params = dict(self.request.GET)
        try:
            query = h.add_order_by(query, get_params, self.query_builder)
            query = self._filter_query(query)
            result = h.add_pagination(query, get_params)
        except Invalid as error:
            self.request.response.status_int = 400
            return {'errors': error.unpack_errors()}
        headers_ctl = self._headers_control(result)
        if headers_ctl is not False:
            return headers_ctl
        return result

    def show(self):
        """Return a resource, given its id.
        :URL: ``GET /<resource_collection_name>/<id>``
        :param str id: the ``id`` value of the resource to be returned.
        :returns: a resource model object.
        """
        resource_model, id_ = self._model_from_id()
        if not resource_model:
            self.request.response.status_int = 404
            return {'error': 'There is no %s with id %s' % (self.member_name,
                                                            id_)}
        return resource_model

    # @h.authorize(['administrator', 'contributor'])
    def update(self):
        """Update a resource and return it.
        :URL: ``PUT /<resource_collection_name>/<id>``
        :Request body: JSON object representing the resource with updated
            attribute values.
        :param str id_: the ``id`` value of the resource to be updated.
        :returns: the updated resource model.
        """
        resource_model, id_ = self._model_from_id()
        if not resource_model:
            self.request.response.status_int = 404
            return {'error': 'There is no %s with id %s' % (self.member_name,
                                                            id_)}
        schema = self.schema_cls()
        try:
            values = json.loads(self.request.body.decode(self.request.charset))
        except ValueError:
            self.request.response.status_int = 400
            return self.JSONDecodeErrorResponse
        state = h.get_state_object(
            values=values,
            dbsession=self.request.dbsession,
            logged_in_user=self.request.session.get('user', {}),
            id=id_
        )
        try:
            data = schema.to_python(values, state)
        except Invalid as error:
            self.request.response.status_int = 400
            return {'errors': error.unpack_errors()}
        resource_model = self._update_resource_model(resource_model, data)
        # resource_model will be False if there are no changes
        if not resource_model:
            self.request.response.status_int = 400
            return {'error': 'The update request failed because the submitted'
                             ' data were not new.'}
        self.request.dbsession.add(resource_model)
        self.request.dbsession.flush()
        return resource_model

    # @h.authorize(['administrator', 'contributor'])
    def edit(self):
        """Return a resource and the data needed to update it.
        :URL: ``GET /<resource_collection_name>/edit``
        :param str id: the ``id`` value of the resource that will be updated.
        :returns: a dictionary of the form::

                {"<resource_member_name>": {...}, "data": {...}}

            where the value of the ``<resource_member_name>`` key is a
            dictionary representation of the resource and the value of the
            ``data`` key is a dictionary containing the data needed to edit an
            existing resource of this type.
        """
        resource_model, id_ = self._model_from_id()
        if not resource_model:
            self.request.response.status_int = 404
            return {'error': 'There is no %s with id %s' % (self.member_name,
                                                            id_)}
        return {'data': self.new(), self.member_name: resource_model}

    # @h.authorize(['administrator', 'contributor'])
    def delete(self):
        """Delete an existing resource and return it.
        :URL: ``DELETE /<resource_collection_name>/<id>``
        :param str id: the ``id`` value of the resource to be deleted.
        :returns: the deleted resource model.
        """
        resource_model, id_ = self._model_from_id()
        if not resource_model:
            self.request.response.status_int = 404
            return {'error': 'There is no %s with id %s' % (self.member_name,
                                                            id_)}
        error_msg = self._delete_impossible(resource_model)
        if error_msg:
            self.request.response.status_int = 403
            return {'error': error_msg}
        self.request.dbsession.delete(resource_model)
        return resource_model

    def search(self):
        """Return the list of resources matching the input JSON query.
        :URL: ``SEARCH /<resource_collection_name>`` (or ``POST
            /<resource_collection_name>/search``)
        :request body: A JSON object of the form::

                {"query": {"filter": [ ... ], "order_by": [ ... ]},
                 "paginator": { ... }}

            where the ``order_by`` and ``paginator`` attributes are optional.
        """
        try:
            python_search_params = json.loads(
                self.request.body.decode(self.request.charset))
        except ValueError:
            self.request.response.status_int = 400
            return self.JSONDecodeErrorResponse
        try:
            sqla_query = self.query_builder.get_SQLA_query(
                python_search_params.get('query'))
        except (OLDSearchParseError, Invalid) as error:
            self.request.response.status_int = 400
            return {'errors': error.unpack_errors()}
        except Exception as error:  # FIX: too general exception
            LOGGER.warning('%s\'s filter expression (%s) raised an unexpected'
                           ' exception: %s.',
                           h.get_user_full_name(self.request.session['user']),
                           self.request.body, error)
            self.request.response.status_int = 400
            return {'error': 'The specified search parameters generated an'
                             'invalid database query'}
        query = self._eagerload_model(sqla_query)
        query = self._filter_query(query)
        return h.add_pagination(query, python_search_params.get('paginator'))

    ###########################################################################
    # Private Methods for Override: redefine in views for custom behaviour
    ###########################################################################

    def _eagerload_model(self, query_obj):
        """Override this in a subclass with model-specific eager loading."""
        return query_obj

    def _filter_query(self, query_obj, **kwargs):
        """Override this in a subclass with model-specific query filtering.
        E.g., in the forms view::

            >>> return h.filter_restricted_models(self.model_name, query_obj)
        """
        return query_obj

    def _headers_control(self, result):
        """Take actions based on header values and/or modify headers. If
        something other than ``False`` is returned, that will be the response.
        Useful for Last-Modified/If-Modified-Since caching, e.g., in ``index``
        method of Forms view.
        """
        return False

    def _create_new_resource(self, data):
        """Create a new resource.
        :param dict data: the data for the resource to be created.
        :returns: an SQLAlchemy model object representing the resource.
        """
        return self.model_cls(**self._get_create_data(data))

    def _update_resource_model(self, resource_model, data):
        """Update ``resource_model`` with ``data`` and return something other
        than ``False`` if resource_model has changed as a result.
        :param resource_model: the resource model to be updated.
        :param dict data: user-supplied representation of the updated resource.
        :returns: the updated resource model or, if ``changed`` has not been set
            to ``True``, ``False``.
        """
        changed = False
        user_data = self._get_user_data(data)
        for attr, val in user_data.items():
            if self._distinct(attr, val, getattr(resource_model, attr)):
                changed = True
                break
        if changed:
            for attr, val in self._get_update_data(user_data).items():
                setattr(resource_model, attr, val)
            return resource_model
        return changed

    def _distinct(self, attr, new_val, existing_val):
        """Return true if ``new_val`` is distinct from ``existing_val``. The
        ``attr`` value is provided so that certain attributes (e.g., m2m) can
        have a special definition of "distinct".
        """
        return new_val != existing_val

    def _delete_impossible(self, resource_model):
        """Return something other than false in a sub-class if this particular
        resource model cannot be deleted.
        """
        return False

    def _model_from_id(self):
        """Return a particular model instance (and the id value), given the
        model id supplied in the URL path.
        """
        id_ = self.request.matchdict['id']
        return self.request.dbsession.query(self.model_cls).get(int(id_)), id_

    def _post_create(self, resource_model):
        """Perform some action after creating a new resource model in the
        database. E.g., with forms we have to update all of the forms that
        contain the newly entered form as a morpheme.
        """
        pass

    ###########################################################################
    # Utilities --- should be in other co-super-class; from utils.py
    ###########################################################################

    def _filter_restricted_models(self, query):
        user = self.logged_in_user
        userIsUnrestricted_ = user_is_unrestricted(user, unrestricted_users)
        if userIsUnrestricted_:
            return query
        else:
            return filter_restricted_models_from_query(model_name, query, user)

    ###########################################################################
    # Abstract Methods --- must be defined in subclasses
    ###########################################################################

    @abc.abstractmethod
    def _get_user_data(self, data):
        """Process the user-provided ``data`` dict, crucially returning a *new*
        dict created from it which is ready for construction of a resource
        model.
        """

    @abc.abstractmethod
    def _get_create_data(self, data):
        """Generate a dict representing a resource to be created; add any
        creation-specific data. The ``data`` dict should be expected to be
        unprocessed, i.e., a JSON-decoded dict from the request body.
        """

    @abc.abstractmethod
    def _get_update_data(self, user_data):
        """Generate a dict representing the updated state of a specific
        resource. The ``user_data`` dict should be expected to be the output of
        ``self._get_user_data(data)``.
        """


    def get_data_for_new_action(self, GET_params, getter_map, model_name_map,
                                mandatory_attributes=[]):
        """Return the data needed to create a new model or edit an existing one.

        :param GET_params: the Pylons dict-like object containing the query
            string parameters of the request.
        :param dict getter_map: maps attribute names to functions that get the
            relevant resources.
        :param dict model_name_map: maps attribute names to the relevant model
            name.
        :param list mandatory_attributes: names of attributes whose values are
            always included in the result
        :returns: a dictionary from plural resource names to lists of resources.

        If no GET parameters are provided (i.e., GET_params is empty), then
        retrieve all data (using getter_map) return them.

        If GET parameters are specified, then for each parameter whose value is
        a non-empty string (and is not a valid ISO 8601 datetime), retrieve and
        return the appropriate list of objects.

        If the value of a GET parameter is a valid ISO 8601 datetime string,
        retrieve and return the appropriate list of objects *only* if the
        datetime param does *not* match the most recent datetime_modified value
        of the relevant data store (i.e., model object).  This makes sense
        because a non-match indicates that the requester has out-of-date data.
        """
        result = {key: [] for key in getter_map}
        if GET_params:
            for key in getter_map:
                if key in mandatory_attributes:
                    result[key] = getter_map[key]()
                else:
                    val = GET_params.get(key)
                    if val:
                        val_as_datetime_obj = h.datetime_string2datetime(val)
                        if val_as_datetime_obj:
                            most_recent_mod_time = self.db\
                                .get_most_recent_modification_datetime(
                                    model_name_map[key])
                            if val_as_datetime_obj != most_recent_mod_time:
                                result[key] = getter_map[key]()
                        else:
                            result[key] = getter_map[key]()
        else:
            for key in getter_map:
                result[key] = getter_map[key]()
        return result

    JSONDecodeErrorResponse = {
        'error': 'JSON decode error: the parameters provided were not valid'
                 ' JSON.'}
