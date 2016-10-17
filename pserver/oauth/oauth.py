# -*- coding: utf-8 -*-
import logging
from calendar import timegm
from collections import OrderedDict
from datetime import datetime

import aiohttp
import asyncio
import jwt
from plone.server.api.service import Service
from plone.server.async import IAsyncUtility
from plone.server.auth.participation import AnonymousUser
from plone.server.auth.participation import PloneUser
from zope.component import getUtility
from zope.interface.interfaces import ComponentLookupError
from zope.securitypolicy.interfaces import Allow
from zope.securitypolicy.interfaces import Deny
from zope.securitypolicy.interfaces import Unset


logger = logging.getLogger(__name__)

# Asyncio Utility
NON_IAT_VERIFY = {
    'verify_iat': False,
}


class IOAuth(IAsyncUtility):
    """Marker interface for OAuth Utility."""
    pass


REST_API = {
    'getAuthCode': ['POST', 'get_authorization_code'],
    'getAuthToken': ['POST', 'get_auth_token'],
    'searchUser': ['POST', 'search_user'],
    'validToken': ['POST', 'valid_token'],
    'getUser': ['POST', 'get_user'],
    'getGroup': ['POST', 'get_group'],
    'getScopeUsers': ['GET', 'get_users'],
    'getScopes': ['GET', 'get_scopes'],
    'grantGlobalRoles': ['POST', 'grant_scope_roles'],
    'revokeGlobalRoles': ['POST', 'deny_scope_roles'],
}


class OAuth(object):
    """Object implementing OAuth Utility."""

    def __init__(self, settings):
        self.settings = settings
        self._server = settings['server']
        self._jwt_secret = settings['jwt_secret']
        self._jwt_algorithm = settings['jwt_algorithm']
        self._client_id = settings['client_id']
        self._client_password = settings['client_password']

    async def initialize(self, app=None):
        self.app = app
        self._service_token = None
        while True:
            logger.debug('Renew token')
            now = timegm(datetime.utcnow().utctimetuple())
            await self.service_token
            expiration = self._service_token['exp']
            time_to_sleep = expiration - now
            await asyncio.sleep(time_to_sleep)

    async def auth_code(self, scope, client_id):
        result = await self.call_auth('getAuthCode', {
            'client_id': client_id,
            'service_token': await self.service_token,
            'scopes': scope,
            'response_type': 'code'
        })
        if result:
            return result['auth_code']
        return None

    @property
    async def service_token(self):
        if self._service_token:
            now = timegm(datetime.utcnow().utctimetuple())
            if self._service_token['exp'] > now:
                return self._service_token['service_token']
        logger.info('SERVICE TOKEN OBTAIN')
        result = await self.call_auth('getAuthToken', {
            'client_id': self._client_id,
            'client_secret': self._client_password,
            'grant_type': 'service'
        })
        if result:
            self._service_token = result
            return self._service_token['service_token']
        return None

    async def validate_token(self, request, token):
        scope = request.site.id
        result = await self.call_auth(
            'validToken',
            params={
                'code': self._service_token['service_token'],
                'token': token,
                'scope': scope
            }
        )
        if result:
            if 'user' in result:
                return result['user']
            else:
                return None
        return None

    async def call_auth(self, call, params, future=None, **kw):
        method, url = REST_API[call]

        result = None
        with aiohttp.ClientSession() as session:
            if method == 'GET':
                logger.info('GET ' + self._server + url + str(params))
                async with session.get(
                        self._server + url, params=params) as resp:
                    if resp.status == 200:
                        try:
                            result = jwt.decode(
                                await resp.text(),
                                self._jwt_secret,
                                algorithms=[self._jwt_algorithm])
                        except jwt.InvalidIssuedAtError:
                            logger.error('Error on Time at OAuth Server')
                            result = jwt.decode(
                                await resp.text(),
                                self._jwt_secret,
                                algorithms=[self._jwt_algorithm],
                                options=NON_IAT_VERIFY)
                    else:
                        logger.error(
                            'OAUTH SERVER ERROR %d %s' % (
                                resp.status,
                                await resp.text()))
                    await resp.release()
            elif method == 'POST':
                logger.info('POST ' + self._server + url + str(params))
                async with session.post(
                        self._server + url, data=params) as resp:
                    if resp.status == 200:
                        try:
                            result = jwt.decode(
                                await resp.text(),
                                self._jwt_secret,
                                algorithms=[self._jwt_algorithm])
                        except jwt.InvalidIssuedAtError:
                            logger.error('Error on Time at OAuth Server')
                            result = jwt.decode(
                                await resp.text(),
                                self._jwt_secret,
                                algorithms=[self._jwt_algorithm],
                                options=NON_IAT_VERIFY)
                    else:
                        logger.error(
                            'OAUTH SERVER ERROR %d %s' % (
                                resp.status,
                                await resp.text()))
                    await resp.release()
            session.close()
        if future is not None:
            future.set_result(result)
        else:
            return result


class PloneJWTExtraction(object):
    """User jwt token extraction."""

    def __init__(self, request):
        """Extraction pluggin that sets initial cache value on request."""
        self.request = request
        try:
            self.config = getUtility(IOAuth)
        except ComponentLookupError:
            logger.error('No IOAuth Utility')
            self.config = None
        if not hasattr(self.request, '_cache_credentials'):
            self.request._cache_credentials = {}

    async def extract_user(self):
        """Extract the Authorization header with bearer and decodes on jwt."""
        header_auth = self.request.headers.get('AUTHORIZATION')
        creds = {}
        if header_auth is not None and self.config is not None:
            schema, _, encoded_token = header_auth.partition(' ')
            if schema.lower() == 'bearer':
                token = encoded_token.encode('ascii')
                try:
                    creds['jwt'] = jwt.decode(
                        token,
                        self.config._jwt_secret,
                        algorithms=[self.config._jwt_algorithm])
                except jwt.exceptions.DecodeError:
                    logger.warn('Invalid JWT token')

                if 'jwt' in creds:
                    oauth_utility = getUtility(IOAuth)
                    validation = await oauth_utility.validate_token(
                        self.request, creds['jwt']['token'])
                    if validation is not None and \
                            validation == creds['jwt']['login']:
                        # We validate that the actual token belongs to the same
                        # as the user on oauth
                        creds['user'] = validation

        self.request._cache_credentials.update(creds)
        return creds


class OAuthPloneUserFactory(object):

    def __init__(self, request):
        self.request = request

    async def create_user(self):
        if 'user' not in self.request._cache_credentials:
            user = AnonymousUser(self.request)
        else:
            try:
                user = OAuthPloneUser(self.request)
                await user.get_info()
            except KeyError:
                user = AnonymousUser(self.request)
        self.request._cache_user = user


class OAuthPloneUser(PloneUser):

    def __init__(self, request):
        super(OAuthPloneUser, self).__init__(request)
        self.token = self.request._cache_credentials['jwt']
        self.login = self.request._cache_credentials['user']
        self.id = self.login
        self._properties = OrderedDict()

    async def get_info(self):
        oauth_utility = getUtility(IOAuth)

        result = await oauth_utility.call_auth(
            'getUser',
            params={
                'service_token': await oauth_utility.service_token,
                'user_token': self.token['token'],
                'scope': self.request.site.id,
                'user': self.login
            }
        )

        if not result:
            raise KeyError('Not a plone.oauth User')

        self._init_data(result)

    def _init_data(self, user_data):
        self._roles = user_data['result']['roles']
        for key, value in self._roles.items():
            if value == 1:
                self._roles[key] = Allow
            elif value == 0:
                self._roles[key] = Deny
            else:
                self._roles[key] = Unset
        self._groups = [key for key, value
                        in user_data['result']['groups'].items() if value]
        self.name = user_data['result']['name']
        logger.error(self._roles)
        if len(self._roles) == 0:
            logger.error('User without roles in this scope')
            raise KeyError('Plone OAuth User has no roles in this Scope')


class getCredentials(Service):

    async def __call__(self):
        oauth_utility = getUtility(IOAuth)
        if 'client_id' in self.request.GET:
            client_id = self.request.GET['client_id']
        else:
            client_id = oauth_utility._client_id

        if hasattr(self.request, 'site'):
            scope = self.request.site.id
        else:
            scope = self.request.GET['scope']

        result = await oauth_utility.auth_code([scope], client_id)
        return {
            'auth_code': result
        }
