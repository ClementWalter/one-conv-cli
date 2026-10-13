"""Validate explicitly supplied browser sessions without reading credential stores."""

from __future__ import annotations

import hashlib
import logging

from .providers.anthropic import FINGERPRINT
from .providers.base import AuthenticationRequired, JsonTransport, SchemaChanged

log = logging.getLogger(__name__)


def _claude_session(cookies):
    """Match the web client's TLS profile while keeping session material in memory."""
    from curl_cffi import requests

    session = requests.Session(impersonate="chrome")
    session.headers.update({
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://claude.ai/",
        "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
    })
    return session


def claude_accounts(cookie_jars, *, session_factory=None, on_verified=None, on_expired=None):
    """Yield verified organization-scoped sessions from the launcher's cookie reader.

    Profile and organization both scope history because different users can
    belong to the same organization. Missing user email remains unknown.
    `on_verified(profile, cookies, organizations)` runs once per session whose
    organizations validated, so callers can keep it beyond the browser's login.
    `on_expired(profile, cookies)` replaces the generic warning for a revoked session.
    """
    factory = session_factory or _claude_session
    seen = set()
    for profile, cookies in cookie_jars():
        if not isinstance(cookies, dict) or not cookies.get("sessionKey"):
            continue
        if any(not isinstance(key, str) or not key or any(c in key for c in "\r\n;=")
               or not isinstance(value, str) or any(c in value for c in "\r\n;")
               for key, value in cookies.items()):
            raise SchemaChanged("Claude browser session contains invalid cookie fields")
        session = factory(cookies)
        transport = JsonTransport(session, "https://claude.ai", FINGERPRINT)
        try:
            organizations = transport.get("/api/organizations")
        except AuthenticationRequired:
            if on_expired is not None:
                on_expired(profile, cookies)
            else:
                log.warning("A Claude browser session requires renewed authorization")
            continue
        if not isinstance(organizations, list):
            raise SchemaChanged("Claude organization response must be an array")
        accounts = []
        for organization in organizations:
            if not isinstance(organization, dict):
                raise SchemaChanged("Claude returned an invalid organization")
            identifier = organization.get("uuid")
            label = organization.get("name")
            if not isinstance(identifier, str) or not identifier or not isinstance(label, str):
                raise SchemaChanged("Claude organization identity is incomplete")
            scoped_id = f"{hashlib.sha256(profile.encode()).hexdigest()[:16]}:{identifier}"
            if scoped_id in seen:
                continue
            seen.add(scoped_id)
            accounts.append({
                "session": session,
                "account_id": scoped_id,
                "email": None,
                "organization_id": identifier,
                "organization_label": label,
                "profile": profile,
            })
        if on_verified is not None and organizations:
            on_verified(profile, cookies, organizations)
        yield from accounts
