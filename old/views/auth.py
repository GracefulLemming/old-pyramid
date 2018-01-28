import logging
import json

from formencode.validators import Invalid

from old.lib.constants import (
    JSONDecodeErrorResponse
)
from old.lib.schemata import (
    LoginSchema,
    PasswordResetSchema
)
import old.lib.helpers as h
from old.models import (
    User,
    Page
)


LOGGER = logging.getLogger(__name__)


def login(request):
    schema = LoginSchema()
    try:
        values = json.loads(request.body.decode(request.charset))
    except ValueError:
        request.response.status_int = 400
        return JSONDecodeErrorResponse
    try:
        result = schema.to_python(values)
    except Invalid as error:
        request.response.status_int = 400
        return {'errors': error.unpack_errors()}
    username = result.get('username')
    password = result.get('password')
    if username is None or password is None:
        return not_authenticated(request)
    user = request.dbsession.query(User)\
        .filter(User.username == username).first()
    if user is None:
        return not_authenticated(request)
    # TODO: when using SQLite, the salt in the database is already bytes, but
    # when using MySQL it's str. Why?
    salt = user.salt
    if isinstance(salt, str):
        salt = salt.encode('utf8')
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
        Page.name == 'home').first()
    return {
        'authenticated': True,
        'user': user,
        'homepage': home_page
    }


def not_authenticated(request):
    request.response.status_int = 401
    return {'error': 'The username and password provided are not valid.'}


def logout(request):
    request.session.delete()
    return {'authenticated': False}


def email_reset_password(request):
    """Reset the user's password and email them a new one.
    :URL: ``POST /login/email_reset_password``
    :request body: a JSON object with a ``"username"`` attribute.
    :returns: a dictionary with ``'valid_username'`` and ``'password_reset'``
        keys whose values are booleans.
    """
    schema = PasswordResetSchema()
    try:
        values = json.loads(request.body.decode(request.charset))
    except ValueError:
        request.response.status_int = 400
        return JSONDecodeErrorResponse
    try:
        result = schema.to_python(values)
    except Invalid as error:
        request.response.status_int = 400
        return {'errors': error.unpack_errors()}
    username = result.get('username')
    user = request.dbsession.query(User).filter(
        User.username == username).first()
    if username is not None and user is not None:
        try:
            new_password = h.generate_password()
            # TODO: instead of sending whole config, just get pyramid settings
            # that we need.
            app_url = request.route_url('info')
            h.send_password_reset_email_to(
                user, new_password, request.registry.settings, app_url)
            salt = user.salt
            if isinstance(salt, str):
                salt = salt.encode('utf8')
            user.password = str(h.encrypt_password(new_password, salt))
            request.dbsession.add(user)
            if request.registry.settings.get('testing', '0') == '1':
                return {'valid_username': True, 'password_reset': True,
                        'new_password': new_password}
            return {'valid_username': True, 'password_reset': True}
        except Exception as error:     # socket.error was too specific ...
            LOGGER.warning('EMAIL RESET PASSWORD: MAIL SEND FAIL: %s: %s',
                           error.__class__.__name__, error)
            request.response.status_int = 500
            return {'error': 'The server is unable to send email.'}
    else:
        request.response.status_int = 400
        return {'error': 'The username provided is not valid.'}
