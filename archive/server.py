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
from one_conv.archive import MAX_REQUEST_BYTES, ArchiveError, decompress

PAGE_SIZE = 1000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Fields(Strict):
    turns: list[str]
    turns_with_subagents: list[str]
    search_text: list[str]


class Manifest(Strict):
    origin: str = Field(min_length=1)
    source: str = Field(min_length=1)
    cwd: str
    session: str = Field(min_length=1)
    mtime: float
    summary: dict
    unread_native: bool | None
    fields: Fields
    originals: dict[str, list[str]]

    @model_validator(mode="after")
    def consistent(self):
        if (self.summary.get("uuid"), self.summary.get("source"), self.summary.get("cwd")) != (self.session, self.source, self.cwd):
            raise ValueError("Summary identity differs from capture")
        if not all(len(d) == 64 for parts in (*self.fields.model_dump().values(), *self.originals.values()) for d in parts):
            raise ValueError("Invalid chunk digest")
        return self


class Commit(Strict):
    manifest: Manifest
    expected: str | None


class Inventory(Strict):
    digests: list[str] = Field(max_length=MAX_REQUEST_BYTES // 64)


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

    @endpoint
    async def conversations(request):
        if request.method == "GET":
            return JSONResponse(await run_in_threadpool(store.list_conversations,
                request.query_params.get("after", ""), request.query_params.get("query"), PAGE_SIZE))
        raw = await body(request)
        commit = Commit.model_validate_json(raw)
        # Store and hash the manifest exactly as sent; validation must not reshape what the client digested.
        return JSONResponse(await run_in_threadpool(store.put, json.loads(raw)["manifest"], commit.expected))

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
