"""Host-independent ASGI archive API. Run via scripts/archive-server.py."""

import hmac
from importlib import import_module
import os

from starlette.applications import Starlette
from starlette.requests import Request, ClientDisconnect
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from archive.store import Conflict, ConversationStore


DEFAULT_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
PAGE_SIZE = 100


class Turn(BaseModel):
    ts: str
    role: str
    blocks: list[dict]


class Summary(BaseModel):
    uuid: str
    source: str
    cwd: str
    start: str
    end: str
    turns: int = Field(ge=0)
    tokens: int | None
    title: str


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    origin: str = Field(min_length=1)
    source: str = Field(min_length=1)
    cwd: str
    session: str = Field(min_length=1)
    mtime: float
    summary: Summary
    unread_native: bool | None
    turns: list[Turn]
    turns_with_subagents: list[Turn] | None
    search_text: list[str]
    originals: dict[str, str]

    @model_validator(mode="after")
    def consistent_identity(self):
        if (self.summary.uuid, self.summary.source, self.summary.cwd) != (self.session, self.source, self.cwd):
            raise ValueError("Summary identity differs from capture")
        return self


class Upload(BaseModel):
    document: Snapshot
    expected: str | None


def create_app(store: ConversationStore, token, max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES):
    if not token:
        raise ValueError("ONE_CONV_ARCHIVE_TOKEN is required")

    async def conversations(request: Request):
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        key = request.path_params.get("key")
        if request.method == "GET":
            if key:
                document = await run_in_threadpool(store.read_conversation, key, request.query_params.get("revision"))
                return JSONResponse(document if document else {"error": "Not found"}, status_code=200 if document else 404)
            return JSONResponse(await run_in_threadpool(store.list_conversations,
                request.query_params.get("after", ""), request.query_params.get("query"), PAGE_SIZE))
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > max_upload_bytes:
                    return JSONResponse({"error": "Capture exceeds configured upload limit"}, status_code=413)
        except ClientDisconnect:
            return JSONResponse({"error": "Incomplete upload"}, status_code=400)
        try:
            upload = Upload.model_validate_json(body)
            result = await run_in_threadpool(store.put, upload.document.model_dump(), upload.expected)
            return JSONResponse(result)
        except ValueError:
            return JSONResponse({"error": "Invalid v1 capture"}, status_code=422)
        except Conflict as error:
            return JSONResponse({"error": str(error)}, status_code=409)

    return Starlette(routes=[Route("/v1/conversations", conversations, methods=["GET", "POST"]),
                             Route("/v1/conversations/{key}", conversations)])


def application():
    module, name = os.environ.get("ONE_CONV_ARCHIVE_STORE_FACTORY", "archive.store:SQLiteStore").split(":")
    factory = getattr(import_module(module), name)
    return create_app(factory(os.environ["ONE_CONV_ARCHIVE_DATABASE_URL"]),
                      os.environ["ONE_CONV_ARCHIVE_TOKEN"],
                      int(os.environ.get("ONE_CONV_ARCHIVE_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))))
