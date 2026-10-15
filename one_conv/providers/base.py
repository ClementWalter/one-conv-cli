"""Provider boundaries preserve remote identities and fail visibly on upstream drift."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time
from urllib.parse import urlsplit


class ProviderError(RuntimeError):
    """An upstream failure safe to expose without response bodies or credentials."""


class AuthenticationRequired(ProviderError):
    """The supplied session needs user authorization."""


class RateLimited(ProviderError):
    """A provider requests a later retry instead of another immediate request."""

    def __init__(self, message="Provider rate limit reached", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class SchemaChanged(ProviderError):
    """The versioned parser cannot safely interpret this upstream response."""


class ProviderUnavailable(ProviderError):
    """A provider endpoint or requested capability is unavailable."""


@dataclass(frozen=True)
class ProviderAccount:
    id: str
    label: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class ConversationSummary:
    id: str
    title: str
    updated_at: str | float | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    text: str
    parent_id: str | None = None
    timestamp: str | float | None = None
    content_blocks: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    messages: tuple[Message, ...]
    active_leaf_id: str | None = None
    coverage: str = "provider_history"
    complete: bool = True


@dataclass(frozen=True)
class ConversationPage:
    items: tuple[ConversationSummary, ...]
    next_cursor: str | None = None


def retry_delay(value):
    """Support both HTTP Retry-After forms without shortening a server delay."""
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def retry_response(request, pauses=(5, 15, 45), sleep=time.sleep):
    """Preserve actual HTTP responses for callers with status-specific legacy behavior."""
    for attempt in range(len(pauses) + 1):
        response = request()
        if response.status_code not in (429, 500, 502, 503, 504):
            return response
        if attempt == len(pauses):
            return None
        delay = retry_delay(getattr(response, "headers", {}).get("Retry-After"))
        if delay is not None and delay > max(pauses, default=0):
            return None
        sleep(pauses[attempt] if delay is None else delay)
    return None


class JsonTransport:
    """Bound GET retries and prohibit redirects from carrying provider credentials."""

    def __init__(self, session, base_url, fingerprint, max_attempts=3, timeout=20, sleep=time.sleep,
                 max_retry_delay=30):
        if max_attempts < 1 or timeout <= 0:
            raise ValueError("Attempts and timeout must be positive")
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Provider origin must be HTTPS")
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.fingerprint = fingerprint
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.sleep = sleep
        self.max_retry_delay = max_retry_delay

    def get(self, path, params=None):
        return self._request("get", path, {"params": params})

    def post_search(self, path, body):
        """Permit only the evidenced read-only search POST, never provider mutations."""
        if self.base_url != "https://chatgpt.com" or path != "/backend-api/global/search":
            raise ValueError("Only the ChatGPT read-only search POST is supported")
        return self._request("post", path, {"json": body})

    def _request(self, method, path, arguments):
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("Provider requests require an absolute local path")
        for attempt in range(self.max_attempts):
            try:
                response = getattr(self.session, method)(self.base_url + path, **arguments,
                                                        timeout=self.timeout, allow_redirects=False)
            except Exception:
                # Session libraries differ; never expose exception strings containing credentials.
                if attempt + 1 == self.max_attempts:
                    raise ProviderUnavailable(f"{self.fingerprint}: transport failed") from None
                self.sleep(2 ** attempt)
                continue
            status = response.status_code
            if status in (401, 403):
                raise AuthenticationRequired(f"{self.fingerprint}: authorization required")
            if status == 429 or 500 <= status < 600:
                delay = retry_delay(getattr(response, "headers", {}).get("Retry-After"))
                delay = 2 ** attempt if delay is None else delay
                # Long provider delays belong in a job queue, not a blocked MCP request.
                if attempt + 1 == self.max_attempts or delay > self.max_retry_delay:
                    if status == 429:
                        raise RateLimited(f"{self.fingerprint}: retry later", delay)
                    raise ProviderUnavailable(f"{self.fingerprint}: upstream unavailable")
                self.sleep(delay)
                continue
            if status != 200:
                raise ProviderUnavailable(f"{self.fingerprint}: HTTP {status}")
            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise SchemaChanged(f"{self.fingerprint}: expected JSON") from None
            if not isinstance(payload, (dict, list)):
                raise SchemaChanged(f"{self.fingerprint}: expected JSON object or array")
            return payload
        raise ProviderUnavailable(f"{self.fingerprint}: exhausted retries")
