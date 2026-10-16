"""Explicit ownership maps limit automatic native imports to assigned workers and children."""

import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from .adapters import parse_events, read_rows
from .model import AccountingError, normalize_event, text


@contextmanager
def _regular_reader(path, *, binary=False):
    """Pinned nonblocking descriptors reject pipes and devices before any read can block."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AccountingError("Ownership mappings and native histories must be regular files")
        stream = os.fdopen(descriptor, "rb") if binary else os.fdopen(descriptor, encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def load_mapping(path):
    """Resolve relative history paths beside the private mapping file, without implicit discovery."""
    path = Path(path)
    try:
        with _regular_reader(path, binary=True) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AccountingError("Ownership mapping exceeds 1 MiB")
        data = json.loads(raw)
    except AccountingError:
        raise
    except (ValueError, RecursionError) as error:
        raise AccountingError("Invalid ownership mapping JSON") from error
    if (not isinstance(data, dict) or set(data) != {"schema_version", "histories", "owners"} or
            type(data["schema_version"]) is not int or data["schema_version"] != 1 or
            not isinstance(data["histories"], list) or not isinstance(data["owners"], dict)):
        raise AccountingError("Mapping requires schema_version=1, histories array and owners object")
    for history in data["histories"]:
        if not isinstance(history, dict) or set(history) != {"source", "path", "billing_scope"}:
            raise AccountingError("Each history requires source, path and billing_scope")
        if history["source"] not in ("codex", "claude", "omp"):
            raise AccountingError("Automatic imports support codex, claude and omp histories")
        root = Path(text(history["path"], "history path")).expanduser()
        history["path"] = root if root.is_absolute() else path.resolve().parent / root
        text(history["billing_scope"], "history billing_scope")
    for address, owner in data["owners"].items():
        text(address, "native session address")
        if not isinstance(owner, dict) or {"cost_center_id", "worker_id"} - set(owner) or set(owner) - {"cost_center_id", "worker_id", "parent_worker_id", "billing_scope"}:
            raise AccountingError("Each owner requires cost_center_id and worker_id; optional parent_worker_id and billing_scope")
        for key, value in owner.items():
            text(value, f"owner {key}")
        if not (owner["worker_id"] == address or owner["worker_id"].endswith("/" + address)):
            raise AccountingError("Mapped worker_id must be its native address or NODE/native-address")
    return data


def _identity(path, source):
    candidate = None
    with _regular_reader(path) as stream:
        for index, row in enumerate(read_rows(stream, tolerate_incomplete_final=True)):
            if index >= 100:
                break
            if source == "codex" and row.get("type") == "session_meta":
                payload = row.get("payload") or {}
                session = text(payload.get("id") or payload.get("session_id"), "Codex session ID")
                origin = payload.get("source")
                spawn = ((origin.get("subagent") or {}).get("thread_spawn") or {}) if isinstance(origin, dict) else {}
                parent = spawn.get("parent_thread_id")
                return f"codex:{session}", f"codex:{parent}" if parent else None
            if source == "omp" and row.get("type") == "session":
                return "omp:" + text(row.get("id"), "OMP session ID"), None
            if source == "claude" and (row.get("sessionId") or row.get("session_id")):
                session = text(row.get("sessionId") or row.get("session_id"), "Claude session ID")
                agent = text(row.get("agentId"), "Claude subagent ID", optional=True)
                candidate = (f"claude:{session}/subagent:{agent}", f"claude:{session}") if agent else (f"claude:{session}", None)
                if row.get("type") == "assistant" or agent:
                    return candidate
    return candidate


def _files(config, maximum):
    seen, files, missing = {}, [], 0
    for history in config["histories"]:
        root = history["path"]
        if not root.exists():
            missing += 1
            continue
        candidates = root.rglob("*.jsonl") if root.is_dir() else [root]
        for path in candidates:
            key = path.resolve()
            settings = (history["source"], history["billing_scope"])
            if key in seen:
                if seen[key] != settings:
                    raise AccountingError("One history file has conflicting source or billing scope mappings")
                continue
            seen[key] = settings
            files.append((path, history))
            if len(files) > maximum:
                raise AccountingError(f"History scan exceeds {maximum} files; narrow the explicit history paths")
    return sorted(files, key=lambda item: str(item[0])), missing


def _assignment(address, graph, owners, active=None):
    active = set() if active is None else active
    while address is not None:
        if address in owners:
            return owners[address], address
        if address in active:
            raise AccountingError("Native worker parent graph contains a cycle")
        active.add(address)
        address = graph.get(address)
    return None, None


def _qualified(address, mapped_address, owner):
    worker = owner["worker_id"]
    prefix = worker[:-len(mapped_address)]
    return prefix + address


def _timed_rows(stream, started, maximum):
    for row in read_rows(stream, tolerate_incomplete_final=True):
        if time.monotonic() - started > maximum:
            raise AccountingError("History usage scan exceeded its time bound; narrow configured paths")
        yield row


def sync_mapping(mapping_path, sink, *, max_files=10000, max_events=100000, max_seconds=30):
    """Scan configured roots read-only, inherit mapped ownership, then submit only canonical metadata."""
    started = time.monotonic()
    config = load_mapping(mapping_path)
    files, missing = _files(config, max_files)
    graph, descriptors = {}, []
    for path, history in files:
        if time.monotonic() - started > max_seconds:
            raise AccountingError("History metadata scan exceeded its time bound; narrow configured paths")
        try:
            identity = _identity(path, history["source"])
        except AccountingError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError) as error:
            raise AccountingError("Malformed native worker identity metadata") from error
        if identity is not None:
            address, parent = identity
            if address in graph and graph[address] != parent:
                raise AccountingError("Native worker has conflicting parents")
            graph[address] = parent
            descriptors.append((path, history, address))
    events, mapped, unmapped, partial, used_owners = [], 0, 0, 0, set()
    for path, history, address in descriptors:
        owner, mapped_address = _assignment(address, graph, config["owners"])
        if owner is None:
            unmapped += 1
            continue
        if time.monotonic() - started > max_seconds:
            raise AccountingError("History usage scan exceeded its time bound; narrow configured paths")
        mapped += 1
        used_owners.add(mapped_address)
        with _regular_reader(path) as stream:
            rows = _timed_rows(stream, started, max_seconds)
            if history["source"] == "claude":
                # Streaming partial responses can carry provisional counts before their final stop reason.
                rows = (row for row in rows if row.get("type") != "assistant" or (row.get("message") or {}).get("stop_reason") is not None)
            parsed = parse_events(history["source"], rows, cost_center_id=owner["cost_center_id"],
                                  billing_scope=owner.get("billing_scope", history["billing_scope"]))
        for event in parsed["events"]:
            worker = event["worker_id"]
            parent = event["parent_worker_id"]
            if worker != address and not (history["source"] == "claude" and worker.startswith(address + "/subagent:")):
                raise AccountingError("Native worker identity changed while reading its history")
            event["worker_id"] = _qualified(worker, mapped_address, owner)
            event["parent_worker_id"] = (_qualified(parent, mapped_address, owner) if parent else
                                         owner.get("parent_worker_id") if address == mapped_address else None)
            events.append(normalize_event(event))
            if len(events) > max_events:
                raise AccountingError(f"History scan exceeds {max_events} events; narrow configured paths")
        partial += parsed["coverage"]["cash_unknown_events"]
    receipts = sink.record_many(events)
    return {"files_scanned": len(files), "mapped_files": mapped, "unmapped_files": unmapped,
            "unrecognized_files": len(files) - len(descriptors), "missing_histories": missing,
            "recorded": sum(receipt["status"] == "recorded" for receipt in receipts),
            "duplicates": sum(receipt["status"] == "duplicate" for receipt in receipts),
            "enriched": sum(receipt.get("enriched", False) for receipt in receipts),
            "owners_without_history": len(set(config["owners"]) - used_owners),
            "coverage": {"usage_events": len(events), "cash_unknown_events": partial,
                         "notice": "Only explicitly mapped native workers and descendants are imported; subscription cash and quota remain unknown."}}


def watch_mapping(mapping_path, sink_factory, *, interval=5, max_backoff=60, iterations=0, wait=time.sleep):
    """Reload authority credentials and ownership each pass; transient failures retry under the same IDs."""
    if not 0.1 <= interval <= max_backoff <= 60 or type(iterations) is not int or iterations < 0:
        raise AccountingError("Watcher requires 0.1 <= interval <= max_backoff <= 60 and nonnegative iterations")
    attempt, delay = 0, interval
    while iterations == 0 or attempt < iterations:
        attempt += 1
        try:
            report = sync_mapping(mapping_path, sink_factory())
        except (AccountingError, OSError, UnicodeError) as error:
            yield {"status": "retrying", "attempt": attempt, "error": str(error), "retry_seconds": delay}
            next_delay = min(max_backoff, delay * 2)
        else:
            yield {"status": "synced", "attempt": attempt, **report}
            delay, next_delay = interval, interval
        if iterations and attempt >= iterations:
            break
        wait(delay)
        delay = next_delay
