"""Host-independent ASGI archive API. Run via scripts/archive-server.py."""

import base64
import binascii
import hmac
from importlib import import_module
import json
import os

from starlette.applications import Starlette
from starlette.requests import Request, ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from archive.store import Conflict, ConversationStore
from one_conv.archive import DIGEST, FIELDS, MAX_REQUEST_BYTES, ArchiveError, conversation_key, decompress, revision

PAGE_SIZE = 1000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Manifest(Strict):
    origin: str = Field(min_length=1)
    source: str = Field(min_length=1)
    cwd: str
    session: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    mtime: float
    capture_version: str = Field(min_length=1)
    account_label: str | None
    summary: dict
    unread_native: bool | None
    freed: float | None = None  # When this machine deleted its native files, which only this capture now holds.
    fields: dict[str, list[str]]
    originals: dict[str, list[str]]

    @model_validator(mode="after")
    def consistent(self):
        if (self.summary.get("uuid"), self.summary.get("source"), self.summary.get("cwd")) != (self.session, self.source, self.cwd):
            raise ValueError("Summary identity differs from capture")
        if set(self.fields) != set(FIELDS):
            raise ValueError("Capture fields differ from this protocol")
        if not all(DIGEST.fullmatch(d) for d in self.digests()):
            raise ValueError("Invalid chunk digest")
        return self

    def digests(self):
        return [d for parts in (*self.fields.values(), *self.originals.values()) for d in parts]


class Commit(Strict):
    manifest: Manifest
    expected: str | None


class Inventory(Strict):
    digests: list[str]


class Chunks(Strict):
    chunks: dict[str, str]


def create_app(store: ConversationStore, token, max_request_bytes=MAX_REQUEST_BYTES):
    if not token:
        raise ValueError("ONE_CONV_ARCHIVE_TOKEN is required")

    def authorized(request):
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode())

    async def body(request):
        data, oversized = bytearray(), False
        async for part in request.stream():
            # Drain an oversized body so the client receives the 413 instead of a reset connection.
            if not oversized:
                data.extend(part)
                oversized = len(data) > max_request_bytes
        if oversized:
            raise OverflowError
        return data

    def endpoint(handler):
        async def wrapped(request: Request):
            if not authorized(request):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                return await handler(request)
            except OverflowError:
                return JSONResponse({"error": "Request exceeds the archive's size limit"}, status_code=413)
            except ClientDisconnect:
                return JSONResponse({"error": "Incomplete upload"}, status_code=400)
            except (ValidationError, ValueError, ArchiveError) as error:
                return JSONResponse({"error": f"Invalid v1 request: {error}"[:300]}, status_code=422)
            except Conflict as error:
                return JSONResponse({"error": str(error)}, status_code=409)
        return wrapped

    def commit(raw):
        validated = Commit.model_validate_json(raw)
        # Store and hash the manifest exactly as sent; validation must not reshape what the client digested.
        manifest = json.loads(raw)["manifest"]
        if store.missing_chunks(validated.manifest.digests()):
            raise Conflict("Upload the capture's chunks before its manifest.")
        parts = manifest["fields"]["search_text"]
        search_text = "\n".join(json.loads(b"".join(decompress(store.read_chunk(d), d) for d in parts))).lower()
        metadata = {name: value for name, value in manifest.items() if name not in ("fields", "originals")}
        key = conversation_key(manifest["origin"], manifest["source"], manifest["cwd"], manifest["session"], manifest["locator"])
        return store.put(key, revision(manifest), metadata, search_text, manifest, validated.expected)

    @endpoint
    async def conversations(request):
        if request.method == "GET":
            return JSONResponse(await run_in_threadpool(store.list_conversations,
                request.query_params.get("after", ""), request.query_params.get("query"), PAGE_SIZE))
        return JSONResponse(await run_in_threadpool(commit, await body(request)))

    @endpoint
    async def conversation(request):
        manifest = await run_in_threadpool(store.read_conversation, request.path_params["key"], request.query_params.get("revision"))
        return JSONResponse(manifest) if manifest else JSONResponse({"error": "Not found"}, status_code=404)

    @endpoint
    async def missing(request):
        inventory = Inventory.model_validate_json(await body(request))
        return JSONResponse({"missing": await run_in_threadpool(store.missing_chunks, inventory.digests)})

    @endpoint
    async def upload(request):
        received = {}
        for name, encoded in Chunks.model_validate_json(await body(request)).chunks.items():
            try:
                data = base64.b64decode(encoded, validate=True)
            except binascii.Error as error:
                raise ValueError("Chunk is not base64") from error
            decompress(data, name)  # Verifies the digest of the raw content.
            received[name] = data
        await run_in_threadpool(store.put_chunks, received)
        return JSONResponse({"stored": len(received)})

    @endpoint
    async def chunk(request):
        data = await run_in_threadpool(store.read_chunk, request.path_params["digest"])
        return Response(data, media_type="application/octet-stream") if data else JSONResponse({"error": "Not found"}, status_code=404)

    return Starlette(routes=[Route("/v1/conversations", conversations, methods=["GET", "POST"]),
                             Route("/v1/conversations/{key}", conversation),
                             Route("/v1/chunks", upload, methods=["POST"]),
                             Route("/v1/chunks/missing", missing, methods=["POST"]),
                             Route("/v1/chunks/{digest}", chunk)])


def application():
    module, name = os.environ.get("ONE_CONV_ARCHIVE_STORE_FACTORY", "archive.store:SQLiteStore").split(":")
    factory = getattr(import_module(module), name)
    return create_app(factory(os.environ["ONE_CONV_ARCHIVE_DATABASE_URL"]), os.environ["ONE_CONV_ARCHIVE_TOKEN"])
