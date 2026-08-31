#!/usr/bin/env python3
"""Bridge-only MCP server for a side-effect-restricted main agent.

The server deliberately exposes only the main-agent bridge operations.  It
does not expose classification, approval, run creation, worker execution, or
publication authority.  All state is resolved through Saihai's canonical
host-owned state-root contract rather than a client-supplied path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import frontdoor_orchestrator as frontdoor


JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "saihai-main-agent-bridge"
SERVER_VERSION = "0.1.0"
WORKSPACE_ID = "Saber5656/Saihai"
FRONTDOOR_ID = "codex"
PRINCIPAL_ID = "codex-main-agent-unconfigured"
MANAGED_PRIMARY_ROOT = frontdoor.MANAGED_PRIMARY_CHECKOUT_ROOT
CHECKOUT_ROOT = frontdoor.REPO_ROOT
STATE_ROOT: Path | None = None
LAUNCH_SESSION_VERIFIER: Any | None = None
SURFACE_REGISTRY = frontdoor.load_surface_registry()
FRONTDOOR_CHOICES = tuple(
    kind for kind in SURFACE_REGISTRY.frontend_kinds if kind != "manual"
)

ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"
MAX_PROMPT_CHARS = 100_000
MAX_SESSION_ID_CHARS = 256
MAX_IDEMPOTENCY_KEY_CHARS = 512
MAX_JSONRPC_FRAME_BYTES = 1024 * 1024
MAX_JSONRPC_ID_CHARS = 128
MAX_JSONRPC_METHOD_CHARS = 128
MAX_JSONRPC_STRING_CHARS = MAX_PROMPT_CHARS
MAX_JSONRPC_TREE_DEPTH = 32
MAX_JSONRPC_TREE_NODES = 10_000
MAX_PENDING_REQUESTS_PER_PRINCIPAL = frontdoor.DEFAULT_BRIDGE_MAX_PENDING_REQUESTS
MAX_PENDING_BYTES_PER_PRINCIPAL = frontdoor.DEFAULT_BRIDGE_MAX_PENDING_BYTES


def _string_schema(*, max_length: int | None = None, pattern: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if max_length is not None:
        schema["maxLength"] = max_length
    if pattern is not None:
        schema["pattern"] = pattern
    return schema


PATH_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": frontdoor.MAX_CONTEXT_REF_COUNT,
    "items": _string_schema(max_length=4096),
}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "submit_request",
        "title": "Submit a typed Saihai request",
        "description": (
            "Submit an agent_task_request for the Saber5656/Saihai workspace. "
            "This creates a waiting-for-human request, returns only the digest of the "
            "idempotency key, and grants no approval or execution authority."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "workspace",
                "task_id",
                "request_id",
                "prompt",
                "refs",
                "allowed_paths",
                "chat_session_id",
                "idempotency_key",
            ],
            "properties": {
                "workspace": {"type": "string", "const": WORKSPACE_ID},
                "task_id": _string_schema(pattern=ARTIFACT_ID_PATTERN),
                "request_id": _string_schema(pattern=ARTIFACT_ID_PATTERN),
                "prompt": _string_schema(max_length=MAX_PROMPT_CHARS),
                "refs": {
                    **PATH_LIST_SCHEMA,
                    "minItems": 1,
                    "description": (
                        "Exact existing repository-relative paths, including filename "
                        "extensions, in first-mentioned order. In this workspace README "
                        "and CHANGELOG mean README.md and CHANGELOG.md."
                    ),
                },
                "allowed_paths": {
                    **PATH_LIST_SCHEMA,
                    "maxItems": frontdoor.MAX_ALLOWED_PATH_COUNT,
                },
                "chat_session_id": _string_schema(max_length=MAX_SESSION_ID_CHARS),
                "idempotency_key": _string_schema(max_length=MAX_IDEMPOTENCY_KEY_CHARS),
            },
        },
        "annotations": {
            "title": "Submit a typed Saihai request",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "read_projection",
        "title": "Read a redacted Saihai projection",
        "description": "Read the redacted bridge projection for a previously submitted request.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "request_id", "chat_session_id"],
            "properties": {
                "workspace": {"type": "string", "const": WORKSPACE_ID},
                "request_id": _string_schema(pattern=ARTIFACT_ID_PATTERN),
                "chat_session_id": _string_schema(max_length=MAX_SESSION_ID_CHARS),
            },
        },
        "annotations": {
            "title": "Read a redacted Saihai projection",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "ack_output",
        "title": "Acknowledge a Saihai projection",
        "description": (
            "Record receipt of an exact redacted projection digest. "
            "Acknowledgement does not change request or run state."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["workspace", "request_id", "projection_digest", "chat_session_id"],
            "properties": {
                "workspace": {"type": "string", "const": WORKSPACE_ID},
                "request_id": _string_schema(pattern=ARTIFACT_ID_PATTERN),
                "projection_digest": {
                    "type": "string",
                    "pattern": r"^sha256:[0-9a-f]{64}$",
                },
                "chat_session_id": _string_schema(max_length=MAX_SESSION_ID_CHARS),
            },
        },
        "annotations": {
            "title": "Acknowledge a Saihai projection",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
)

TOOL_BY_NAME = {str(tool["name"]): tool for tool in TOOLS}


class JsonRpcError(RuntimeError):
    """Safe JSON-RPC error whose message contains no client material."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_error() -> dict[str, Any]:
    payload = {"decision": "blocked", "reason": "saihai_bridge_request_rejected"}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "structuredContent": payload,
        "isError": True,
    }


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _canonical_state_root() -> Path:
    try:
        return frontdoor.host_state_root.resolve_configured_state_root(STATE_ROOT)
    except frontdoor.host_state_root.HostStateRootError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc


def _host_principal() -> dict[str, str]:
    return frontdoor.bridge_principal(
        FRONTDOOR_ID,
        principal_id=PRINCIPAL_ID,
    )


def _host_launch_session_identity() -> dict[str, Any] | None:
    descriptor = SURFACE_REGISTRY.descriptor(FRONTDOOR_ID)
    if not descriptor.launch_session_required:
        return None
    if LAUNCH_SESSION_VERIFIER is None:
        raise frontdoor.FrontdoorError("host launch-session verifier is not configured")
    checkout_identity = _host_checkout_identity()
    return LAUNCH_SESSION_VERIFIER.verify_parent_session(
        subject_pid=os.getppid(),
        profile_id=PRINCIPAL_ID,
        principal_id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        checkout_identity=checkout_identity,
    )


def _host_checkout_identity() -> dict[str, str]:
    return frontdoor.resolve_checkout_identity(
        workspace_id=WORKSPACE_ID,
        managed_primary=MANAGED_PRIMARY_ROOT,
        checkout_root=CHECKOUT_ROOT,
    )


def _require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JsonRpcError(-32602, "Invalid params")
    return value


def _validate_exact_fields(
    arguments: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    if set(arguments) != required and (
        not required.issubset(arguments) or not set(arguments).issubset(allowed)
    ):
        raise ValueError("invalid fields")


def _require_string(
    arguments: dict[str, Any],
    field: str,
    *,
    max_length: int | None = None,
    artifact_id: bool = False,
) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("invalid string")
    if max_length is not None and len(value) > max_length:
        raise ValueError("string too long")
    if artifact_id:
        frontdoor.validate_artifact_id(value, field)
    return value


def _require_workspace(arguments: dict[str, Any]) -> None:
    if arguments.get("workspace") != WORKSPACE_ID:
        raise ValueError("unsupported workspace")


def _require_path_list(arguments: dict[str, Any], field: str, *, non_empty: bool) -> list[str]:
    value = arguments.get(field)
    if not isinstance(value, list):
        raise ValueError("invalid path list")
    limit = frontdoor.MAX_CONTEXT_REF_COUNT if field == "refs" else frontdoor.MAX_ALLOWED_PATH_COUNT
    if len(value) > limit or (non_empty and not value):
        raise ValueError("invalid path list size")
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\x00" in item or len(item) > 4096:
            raise ValueError("invalid path item")
    return list(value)


def _submit(arguments: dict[str, Any]) -> dict[str, Any]:
    required = {
        "workspace",
        "task_id",
        "request_id",
        "prompt",
        "refs",
        "allowed_paths",
        "chat_session_id",
        "idempotency_key",
    }
    _validate_exact_fields(arguments, required=required)
    _require_workspace(arguments)
    task_id = _require_string(arguments, "task_id", artifact_id=True)
    request_id = _require_string(arguments, "request_id", artifact_id=True)
    prompt = _require_string(arguments, "prompt", max_length=MAX_PROMPT_CHARS)
    refs = _require_path_list(arguments, "refs", non_empty=True)
    allowed_paths = _require_path_list(arguments, "allowed_paths", non_empty=False)
    chat_session_id = _require_string(
        arguments,
        "chat_session_id",
        max_length=MAX_SESSION_ID_CHARS,
    )
    idempotency_key = _require_string(
        arguments,
        "idempotency_key",
        max_length=MAX_IDEMPOTENCY_KEY_CHARS,
    )
    checkout_identity = _host_checkout_identity()
    launch_session_identity = _host_launch_session_identity()
    return frontdoor.bridge_submit_request(
        state_root=_canonical_state_root(),
        frontend_kind=FRONTDOOR_ID,
        principal=_host_principal(),
        workspace_id=WORKSPACE_ID,
        checkout_identity=checkout_identity,
        launch_session_identity=launch_session_identity,
        max_pending_requests=MAX_PENDING_REQUESTS_PER_PRINCIPAL,
        max_pending_bytes=MAX_PENDING_BYTES_PER_PRINCIPAL,
        surface_registry=SURFACE_REGISTRY,
        payload={
            "task_id": task_id,
            "request_id": request_id,
            "request_kind": "agent_task_request",
            "prompt": prompt,
            "refs": refs,
            "allowed_paths": allowed_paths,
            "expires_at": "run_terminal",
            "frontdoor": FRONTDOOR_ID,
            "chat_session_id": chat_session_id,
            "idempotency_key": idempotency_key,
        },
    )


def _read_projection(arguments: dict[str, Any]) -> dict[str, Any]:
    required = {"workspace", "request_id", "chat_session_id"}
    _validate_exact_fields(arguments, required=required)
    _require_workspace(arguments)
    request_id = _require_string(arguments, "request_id", artifact_id=True)
    chat_session_id = _require_string(
        arguments,
        "chat_session_id",
        max_length=MAX_SESSION_ID_CHARS,
    )
    return frontdoor.bridge_read_projection(
        state_root=_canonical_state_root(),
        request_id=request_id,
        frontdoor=FRONTDOOR_ID,
        chat_session_id=chat_session_id,
        principal=_host_principal(),
        launch_session_identity=_host_launch_session_identity(),
        surface_registry=SURFACE_REGISTRY,
    )


def _ack_output(arguments: dict[str, Any]) -> dict[str, Any]:
    required = {"workspace", "request_id", "projection_digest", "chat_session_id"}
    _validate_exact_fields(arguments, required=required)
    _require_workspace(arguments)
    request_id = _require_string(arguments, "request_id", artifact_id=True)
    projection_digest = _require_string(arguments, "projection_digest", max_length=71)
    if (
        not projection_digest.startswith("sha256:")
        or len(projection_digest) != 71
        or any(char not in "0123456789abcdef" for char in projection_digest[7:])
    ):
        raise ValueError("invalid projection digest")
    chat_session_id = _require_string(
        arguments,
        "chat_session_id",
        max_length=MAX_SESSION_ID_CHARS,
    )
    result = frontdoor.bridge_ack_output(
        state_root=_canonical_state_root(),
        request_id=request_id,
        projection_digest=projection_digest,
        frontdoor=FRONTDOOR_ID,
        chat_session_id=chat_session_id,
        principal=_host_principal(),
        launch_session_identity=_host_launch_session_identity(),
        surface_registry=SURFACE_REGISTRY,
    )
    # The frontdoor records the host path for operators.  A main agent only
    # receives the receipt facts, never the host-owned state path.
    return {key: value for key, value in result.items() if key != "ack_path"}


TOOL_HANDLERS = {
    "submit_request": _submit,
    "read_projection": _read_projection,
    "ack_output": _ack_output,
}


def _call_tool(params: Any) -> dict[str, Any]:
    params_object = _require_object(params)
    name = params_object.get("name")
    if not isinstance(name, str) or name not in TOOL_HANDLERS:
        raise JsonRpcError(-32601, "Tool not found")
    arguments = _require_object(params_object.get("arguments", {}))
    try:
        payload = TOOL_HANDLERS[name](arguments)
    except JsonRpcError:
        raise
    except Exception:
        # Tool failures are deliberately opaque: refs and prompts may contain
        # sensitive material and must not be reflected in protocol errors.
        return _tool_error()
    return _tool_result(payload)


def dispatch(message: Any) -> dict[str, Any] | None:
    """Dispatch one decoded JSON-RPC message without raising to the caller."""

    if not isinstance(message, dict):
        return _error(None, -32600, "Invalid Request")
    request_id = message.get("id") if "id" in message else None
    is_notification = "id" not in message
    method_value = message.get("method")
    if not is_notification and not _valid_jsonrpc_id(request_id):
        return _error(None, -32600, "Invalid Request")
    if (
        message.get("jsonrpc") != JSONRPC_VERSION
        or not isinstance(method_value, str)
        or not method_value
        or len(method_value) > MAX_JSONRPC_METHOD_CHARS
        or "\x00" in method_value
        or not _bounded_json_tree(message)
    ):
        return None if is_notification else _error(request_id, -32600, "Invalid Request")

    method = method_value
    try:
        if method == "notifications/initialized":
            return None
        if is_notification:
            return None
        if method == "initialize":
            return _response(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Use only typed request submission, redacted projection reads, and receipt acknowledgement. "
                        "Approval, execution, and publication remain host-owned Saihai actions."
                    ),
                },
            )
        if method == "ping":
            return _response(request_id, {})
        if method == "tools/list":
            return _response(request_id, {"tools": list(TOOLS)})
        if method == "tools/call":
            return _response(request_id, _call_tool(message.get("params", {})))
        raise JsonRpcError(-32601, "Method not found")
    except JsonRpcError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception:
        return _error(request_id, -32603, "Internal error")


def _valid_jsonrpc_id(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -(2**53 - 1) <= value <= 2**53 - 1
    if isinstance(value, str):
        return 0 < len(value) <= MAX_JSONRPC_ID_CHARS and "\x00" not in value
    return False


def _bounded_json_tree(value: Any) -> bool:
    node_count = 0

    def visit(candidate: Any, depth: int) -> bool:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_JSONRPC_TREE_NODES or depth > MAX_JSONRPC_TREE_DEPTH:
            return False
        if isinstance(candidate, str):
            return len(candidate) <= MAX_JSONRPC_STRING_CHARS and "\x00" not in candidate
        if candidate is None or isinstance(candidate, (bool, int, float)):
            return True
        if isinstance(candidate, list):
            return all(visit(item, depth + 1) for item in candidate)
        if isinstance(candidate, dict):
            return all(
                isinstance(key, str)
                and len(key) <= MAX_JSONRPC_METHOD_CHARS
                and "\x00" not in key
                and visit(item, depth + 1)
                for key, item in candidate.items()
            )
        return False

    return visit(value, 0)


def _discard_frame_tail(input_stream: TextIO) -> None:
    while True:
        tail = input_stream.readline(MAX_JSONRPC_FRAME_BYTES + 1)
        if not tail or tail.endswith("\n"):
            return


def serve(input_stream: TextIO, output_stream: TextIO) -> None:
    """Serve newline-delimited JSON-RPC until EOF without leaking bad input."""

    while True:
        raw_line = input_stream.readline(MAX_JSONRPC_FRAME_BYTES + 1)
        if not raw_line:
            break
        truncated = len(raw_line) > MAX_JSONRPC_FRAME_BYTES and not raw_line.endswith("\n")
        if truncated:
            _discard_frame_tail(input_stream)
        try:
            encoded_size = len(raw_line.encode("utf-8"))
        except UnicodeError:
            encoded_size = MAX_JSONRPC_FRAME_BYTES + 1
        if not truncated and encoded_size <= MAX_JSONRPC_FRAME_BYTES and not raw_line.strip():
            continue
        if truncated or encoded_size > MAX_JSONRPC_FRAME_BYTES:
            response = _error(None, -32700, "Parse error")
        else:
            try:
                message = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeError, RecursionError):
                response = _error(None, -32700, "Parse error")
            else:
                response = dispatch(message)
        if response is None:
            continue
        output_stream.write(
            json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        output_stream.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Saihai main-agent bridge MCP server")
    parser.add_argument(
        "--frontdoor",
        choices=FRONTDOOR_CHOICES,
        required=True,
        help="Host-pinned agent surface identity used for bridge audit principals",
    )
    parser.add_argument(
        "--principal-id",
        required=True,
        help="Host-pinned installed frontend profile principal",
    )
    parser.add_argument(
        "--workspace-id",
        choices=(WORKSPACE_ID,),
        required=True,
        help="Host-pinned owner/repository workspace identity",
    )
    parser.add_argument(
        "--managed-primary",
        type=Path,
        required=True,
        help="Canonical managed primary checkout realpath",
    )
    parser.add_argument(
        "--checkout-root",
        type=Path,
        required=True,
        help="Canonical active primary or registered linked-worktree realpath",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="Manifest-pinned canonical host state root; never client supplied",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    global CHECKOUT_ROOT, FRONTDOOR_ID, LAUNCH_SESSION_VERIFIER, MANAGED_PRIMARY_ROOT, PRINCIPAL_ID, STATE_ROOT, WORKSPACE_ID
    os.umask(0o077)
    args = parse_args(argv)
    FRONTDOOR_ID = args.frontdoor
    PRINCIPAL_ID = frontdoor.validate_artifact_id(args.principal_id, "principal_id")
    WORKSPACE_ID = args.workspace_id
    MANAGED_PRIMARY_ROOT = args.managed_primary
    CHECKOUT_ROOT = args.checkout_root
    STATE_ROOT = args.state_root
    _canonical_state_root()
    # Fail before exposing the MCP inventory if host launch identity is not a
    # managed primary or a currently registered linked worktree.
    _host_checkout_identity()
    try:
        LAUNCH_SESSION_VERIFIER = SURFACE_REGISTRY.make_launch_session_verifier(
            FRONTDOOR_ID
        )
    except frontdoor.frontdoor_surface_registry.SurfaceRegistryError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc
    _host_launch_session_identity()
    serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
