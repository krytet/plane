# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import base64
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import urlparse

# Third party imports
import requests
from django.conf import settings

# Module imports
from .crypto import decrypt_credential


class JiraApiError(Exception):
    def __init__(self, message, *, status_code=None, code="jira_api_error", retryable=False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class JiraCredentials:
    site_url: str
    email: str
    api_token: str


def _allowed_hosts():
    configured = getattr(settings, "JIRA_ALLOWED_HOSTS", None)
    if configured:
        return {str(host).strip().lower().rstrip(".") for host in configured if str(host).strip()}
    return {"atlassian.net"}


def normalize_site_url(value):
    """Validate a Jira Cloud base URL before any credential is sent."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Jira site URL is required")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Jira site URL must be an https URL")
    if parsed.username or parsed.password:
        raise ValueError("Credentials must not be embedded in the Jira site URL")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Jira site URL must point to the site root")
    host = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError("Private Jira addresses are not allowed")
    allowed_hosts = _allowed_hosts()
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts):
        raise ValueError("Jira site is not in the configured allowlist")
    return f"https://{host}"


def _safe_error_payload(response):
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class JiraClient:
    """Small Jira Cloud v3 client. It never logs URL credentials or headers."""

    def __init__(self, credentials, *, session=None, timeout=30):
        site_url = normalize_site_url(credentials.site_url)
        self.site_url = site_url
        self.credentials = credentials
        self.timeout = timeout
        self.session = session or requests.Session()
        token = base64.b64encode(f"{credentials.email}:{credentials.api_token}".encode("utf-8")).decode("ascii")
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
            }
        )

    @classmethod
    def from_grant(cls, grant, **kwargs):
        token = decrypt_credential(grant.encrypted_credential)
        return cls(
            JiraCredentials(site_url=grant.site.site_url, email=grant.email, api_token=token),
            **kwargs,
        )

    def _request(self, method, path, *, json_body=None, params=None):
        try:
            response = self.session.request(
                method,
                f"{self.site_url}{path}",
                json=json_body,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise JiraApiError(
                "Jira did not return a response",
                code="jira_transport_error",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            payload = _safe_error_payload(response)
            messages = payload.get("errorMessages") or []
            field_errors = payload.get("errors") or {}
            detail = "; ".join(
                [str(message) for message in messages] + [f"{key}: {value}" for key, value in field_errors.items()]
            )
            code = "jira_not_found" if response.status_code == 404 else "jira_api_error"
            if response.status_code in (401, 403):
                code = "jira_auth_error"
            elif response.status_code == 429:
                code = "jira_rate_limited"
            raise JiraApiError(
                detail or f"Jira returned HTTP {response.status_code}",
                status_code=response.status_code,
                code=code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise JiraApiError(
                "Jira returned an invalid JSON response",
                code="jira_invalid_response",
                retryable=True,
            ) from exc

    def myself(self):
        return self._request("GET", "/rest/api/3/myself")

    def get_issue(self, issue_key):
        return self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}",
            params={"fields": "summary,status,project,issuetype,updated"},
        )

    def get_create_fields(self, project_key, issue_type_id):
        payload = self._request(
            "GET",
            "/rest/api/3/issue/createmeta",
            params={"projectKeys": project_key, "issuetypeIds": issue_type_id, "expand": "projects.issuetypes.fields"},
        )
        return payload

    def create_issue(self, fields):
        return self._request("POST", "/rest/api/3/issue", json_body={"fields": fields})

    def get_transitions(self, issue_key):
        payload = self._request("GET", f"/rest/api/3/issue/{issue_key}/transitions")
        return payload.get("transitions", [])

    def transition_issue(self, issue_key, transition_id, fields=None):
        payload = {"transition": {"id": str(transition_id)}}
        if fields:
            payload["fields"] = fields
        self._request("POST", f"/rest/api/3/issue/{issue_key}/transitions", json_body=payload)
        return self.get_issue(issue_key)

    def search(self, jql, *, fields=None, max_results=50):
        return self._request(
            "GET",
            "/rest/api/3/search/jql",
            params={
                "jql": jql,
                "maxResults": max_results,
                "fields": ",".join(fields or ["summary", "status", "project", "issuetype", "updated"]),
            },
        )


def grant_client(grant, **kwargs):
    return JiraClient.from_grant(grant, **kwargs)
