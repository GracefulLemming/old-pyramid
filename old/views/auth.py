import os
import logging
import json
from formencode.validators import Invalid
from old.lib.schemata import LoginSchema, PasswordResetSchema
import old.lib.helpers as h
from ..models import User, Page
from pyramid.security import remember, forget
from pyramid.view import view_config

log = logging.getLogger(__name__)


@view_config(route_name='authenticate', renderer='json')
def login(request):
    schema = LoginSchema()
    try:
        values = json.loads(request.body.decode(request.charset))
    except ValueError:
        request.response.status_int = 400
        return h.JSONDecodeErrorResponse
    try:
        result = schema.to_python(values)
    except Invalid as e:
        request.response.status_int = 400
        return {'errors': e.unpack_errors()}
    username = result.get('username')
    password = result.get('password')
    if username is None or password is None:
        return not_authenticated(request)
    user = request.dbsession.query(User)\
        .filter(User.username == username).first()
    if user is None:
        return not_authenticated(request)
    salt = user.salt
    password = h.encrypt_password(password, salt)
    user = request.dbsession.query(User)\
        .filter(User.username == username)\
        .filter(User.password == password).first()
    if user is None:
        return not_authenticated(request)
    # request.response.headerlist.extend(remember(request, user.id))
    request.session['user'] = user.get_dict()
    # request.session.save()
    home_page = request.dbsession.query(Page).filter(
        Page.name == u'home').first()
    return {
        'authenticated': True,
        'user': user,
        'homepage': home_page
    }


def not_authenticated(request):
    request.response.status_int = 401
    return {'error': u'The username and password provided are not valid.'}


@view_config(route_name='logout', renderer='json')
def logout(request):
    # request.response.headerlist.extend(forget(request))
    request.session.delete()
    return {'authenticated': False}


@view_config(route_name='email_reset_password', renderer='json')
def email_reset_password(request):
    """Reset the user's password and email them a new one.
    :URL: ``POST /login/email_reset_password``
    :request body: a JSON object with a ``"username"`` attribute.
    :returns: a dictionary with ``'valid_username'`` and ``'password_reset'``
        keys whose values are booleans.
    """
    schema = PasswordResetSchema()
    try:
        values = json.loads(unicode(request.body, request.charset))
    except ValueError:
        request.response.status_int = 400
        return h.JSONDecodeErrorResponse
    try:
        result = schema.to_python(values)
    except Invalid as e:
        request.response.status_int = 400
        return {'errors': e.unpack_errors()}
    username = result.get('username')
    user = request.dbsession.query(User).filter(
        User.username == username).first()
    if username is not None and user is not None:
        try:
            new_password = h.generate_password()
            # TODO: instead of sending whole config, just get pyramid settings
            # that we need.
            h.send_password_reset_email_to(
                user, new_password, config=config)
            user.password = str(
                h.encrypt_password(new_password, user.salt))
            request.dbsession.add(user)
            if os.path.split(config['__file__'])[-1] == 'test.ini':
                return {'valid_username': True, 'password_reset': True,
                        'new_password': new_password}
            else:
                return {'valid_username': True, 'password_reset': True}
        except:     # socket.error was too specific ...
            request.response.status_int = 500
            return {'error': 'The server is unable to send email.'}
    else:
        request.response.status_int = 400
        return {'error': 'The username provided is not valid.'}