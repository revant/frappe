# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
import requests
import frappe
import base64
from frappe import _
from frappe.model.document import Document
from datetime import datetime, timedelta
from urllib.parse import urlencode
from six.moves.urllib.parse import unquote
from requests_oauthlib import OAuth2Session


class ConnectedApp(Document):

	def autoname(self):
		self.callback = frappe.scrub(self.provider_name)

	def validate(self):
		callback_path = 'api/method/frappe.integrations.doctype.connected_app.connected_app.callback/'
		self.redirect_uri = frappe.request.host_url + callback_path + self.callback

	def get_oauth2_session(self):
		return OAuth2Session(self.client_id, redirect_uri=self.redirect_uri, scope=self.scope)

	def get_client_token(self):
		try:
			token = self.get_stored_client_token()
		except frappe.exceptions.DoesNotExistError:
			token = self.retrieve_client_token()

		token = self.check_validity(token)
		return token

	def get_params(self, **kwargs):
		return {
			'client_id': self.client_id,
			'redirect_uri': self.redirect_uri,
			'scope': self.scope
		}.update(kwargs)

	def retrieve_client_token(self):
		client_secret = self.get_password('client_secret')
		data = self.get_params(grant_type='client_credentials', client_secret=client_secret)
		response = requests.post(
			self.token_endpoint,
			data=urlencode(data),
			headers={'Content-Type': 'application/x-www-form-urlencoded'}
		)
		token = response.json()
		return self.update_stored_client_token(token)

	def check_validity(self, token):
		if(token.get('__islocal') or (not token.access_token)):
			raise frappe.exceptions.DoesNotExistError

		expiry = token.modified + timedelta(seconds=token.expires_in)
		if expiry > datetime.now():
			return token

		return self.refresh_token(token)

	def initiate_auth_code_flow(self, user=None, redirect_to=None):
		redirect_to = redirect_to or '/desk'
		user = user or frappe.session.user
		oauth = self.get_oauth2_session()
		authorization_url, state = oauth.authorization_url(self.authorization_endpoint)

		try:
			token = self.get_stored_user_token(user)
		except frappe.exceptions.DoesNotExistError:
			token = frappe.new_doc('Token Cache')
			token.user = user
			token.connected_app = self.name

		token.state = state
		token.save()
		frappe.db.commit()

		return authorization_url

	def get_user_token(self, user=None, redirect_to=None):
		redirect_to = redirect_to or '/desk'
		user = user or frappe.session.user

		try:
			token = self.get_stored_user_token(user)
			token = self.check_validity(token)
		except frappe.exceptions.DoesNotExistError:
			redirect = self.initiate_auth_code_flow(user, redirect_to)
			frappe.local.response["type"] = "redirect"
			frappe.local.response["location"] = redirect
			return redirect

		return token

	def refresh_token(self, token):
		data = self.get_params(grant_type='refresh_token', refresh_token=token.refresh_token)
		headers = {'Content-Type': 'application/x-www-form-urlencoded'}
		response = requests.post(self.token_endpoint, data=urlencode(data), headers=headers)
		new_token = response.json()

		# Revoke old token
		data = urlencode({'token': token.get('access_token')})
		headers['Authorization'] = 'Bearer ' + new_token.get('access_token')
		requests.post(self.revocation_endpoint, data=data, headers=headers)

		return self.update_stored_client_token(new_token)

	def get_stored_client_token(self):
		return frappe.get_doc('Token Cache', self.name + '-user')

	def get_stored_user_token(self, user):
		return frappe.get_doc('Token Cache', self.name + '-' + user)

	def update_stored_client_token(self, token_data):
		try:
			stored_token = self.get_stored_client_token()
		except frappe.exceptions.DoesNotExistError:
			stored_token = frappe.new_doc('Token Cache')

		stored_token.connected_app = self.name
		stored_token.access_token = token_data.get('access_token')
		stored_token.refresh_token = token_data.get('refresh_token')
		stored_token.expires_in = token_data.get('expires_in')
		stored_token.save(ignore_permissions=True)
		frappe.db.commit()

		return frappe.get_doc('Token Cache', stored_token.name)


@frappe.whitelist(allow_guest=True)
def callback(code=None, state=None):
	"""Handle client's code."""
	if frappe.request.method != 'GET':
		throw_error(_('Invalid Method'))
		return

	if frappe.session.user == 'Guest':
		throw_error(_('Please Sign In'))
		return

	path = frappe.request.path[1:].split("/")
	if len(path) == 4 and path[3]:
		connected_app = path[3]
		token_cache = frappe.get_doc('Token Cache', connected_app + '-' + frappe.session.user)
		if not token_cache:
			throw_error(_('State Not Found'))
			return

		if state != token_cache.state:
			throw_error(_('Invalid State'))
			return

		try:
			app = frappe.get_doc('Connected App', connected_app)
		except frappe.exceptions.DoesNotExistError:
			throw_error(_('Invalid App'))
			return

		oauth = app.get_oauth2_session()
		token = oauth.fetch_token(app.token_endpoint, code=code)

		token_cache.access_token = token.get('access_token')
		token_cache.refresh_token = token.get('refresh_token')
		token_cache.expires_in = token.get('expires_in')
		token_cache.state = None
		token_cache.save()
		frappe.db.commit()

		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = '/desk'
	else:
		throw_error(_('Invalid Parameter(s)'))
		return


def throw_error(error):
	"""Set Response Status 400 and show error."""
	frappe.local.response['http_status_code'] = 400
	frappe.local.response['error'] = error


def str_to_b64(string):
	"""Return base64 encoded string."""
	return base64.b64encode(string.encode('utf-8'))


def b64_to_str(b64):
	"""Return base64 decoded string."""
	return base64.b64decode(b64).decode('utf-8')