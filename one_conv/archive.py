"""Versioned archive transport. Database and hosting details stay server-side."""

import hashlib
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ArchiveError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to another endpoint.


def encode(value, *, sort_keys=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":")).encode()


def revision(document):
    return hashlib.sha256(encode(document, sort_keys=True)).hexdigest()


def conversation_key(origin, source, cwd, session):
    return revision([origin, source, cwd, session])


class ArchiveClient:
    def __init__(self, url, token, timeout=30):
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ArchiveError("Archive requires HTTPS (HTTP is allowed only on loopback).")
        if not token or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ArchiveError("Configure an archive URL and a separate token.")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def request(self, path, body=None):
        request = Request(self.url + "/v1/" + path,
                          data=encode(body) if body is not None else None,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise ArchiveError(f"Archive returned HTTP {error.code}; no changes acknowledged.") from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise ArchiveError("Archive unavailable or returned an invalid response.") from error

    def list_conversations(self, query=None):
        after = ""
        while True:
            page = self.request("conversations?" + urlencode({"after": after, **({"query": query} if query is not None else {})}))
            yield from page["items"]
            following = page["next"]
            if following is None:
                break
            if following <= after:
                raise ArchiveError("Archive pagination did not advance.")
            after = following

    def read_conversation(self, key, revision=None):
        return self.request("conversations/" + key + ("?" + urlencode({"revision": revision}) if revision else ""))

    def put(self, document, expected):
        return self.request("conversations", {"document": document, "expected": expected})


def configured_client():
    url = os.environ.get("ONE_CONV_ARCHIVE_URL")
    return ArchiveClient(url, os.environ.get("ONE_CONV_ARCHIVE_TOKEN", "")) if url else None
