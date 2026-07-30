"""Authenticated HTTP facade for the current Agent-First authority."""

from __future__ import annotations

from http import HTTPStatus
import sqlite3
from typing import Callable, Mapping, Protocol

from .agent_first_authority import AgentFirstAuthority, AuthorityError


REGISTER_PATH = ("v1", "agent-first", "workers", "register")
ACCEPT_PATH = ("v1", "agent-first", "tasks", "accept")
CLAIM_PATH = ("v1", "agent-first", "tasks", "claim")


class CurrentHttpResponder(Protocol):
    def binary(
        self,
        payload: bytes,
        status: int = HTTPStatus.OK,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def error(self, status: int, message: str) -> None: ...


def handle_agent_first_current_post(
    responder: CurrentHttpResponder,
    segments: list[str],
    body: object,
    *,
    connect_factory: Callable[[], sqlite3.Connection],
    worker_record: Mapping[str, object] | None,
    operator_authenticated: bool,
    operator_authorized: bool,
) -> None:
    """Dispatch one current-only authority operation after principal binding."""

    route = tuple(segments)
    if route not in {REGISTER_PATH, ACCEPT_PATH, CLAIM_PATH}:
        responder.error(HTTPStatus.NOT_FOUND, "Route not found")
        return
    if not isinstance(body, dict):
        _error(responder, HTTPStatus.BAD_REQUEST, "CONTRACT_DOCUMENT_INVALID")
        return

    if route == ACCEPT_PATH:
        if not operator_authenticated:
            _error(responder, HTTPStatus.UNAUTHORIZED, "AUTHORITY_INPUT_UNTRUSTED")
            return
        if not operator_authorized:
            _error(responder, HTTPStatus.FORBIDDEN, "AUTHORITY_INPUT_UNTRUSTED")
            return
    else:
        if worker_record is None:
            _error(responder, HTTPStatus.UNAUTHORIZED, "AUTHORITY_INPUT_UNTRUSTED")
            return
        worker_id = worker_record.get("worker_id")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or body.get("worker_id") != worker_id
        ):
            _error(responder, HTTPStatus.FORBIDDEN, "AUTHORITY_INPUT_UNTRUSTED")
            return

    authority = AgentFirstAuthority(connect_factory)
    try:
        response = (
            authority.register_worker(body)
            if route == REGISTER_PATH
            else authority.accept_current_task(body)
            if route == ACCEPT_PATH
            else authority.claim_and_issue_current_grant(body)
        )
    except AuthorityError as error:
        _canonical_response(responder, error.response_bytes, _status(error.code))
        return
    _canonical_response(responder, response, HTTPStatus.OK)


def _error(
    responder: CurrentHttpResponder,
    status: HTTPStatus,
    code: str,
) -> None:
    error = AuthorityError(code)
    _canonical_response(responder, error.response_bytes, status)


def _canonical_response(
    responder: CurrentHttpResponder,
    payload: bytes,
    status: HTTPStatus,
) -> None:
    responder.binary(
        payload,
        status,
        content_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _status(code: str) -> HTTPStatus:
    if code in {
        "AUTHORITY_FENCED",
        "IDEMPOTENCY_CONFLICT",
        "TASK_NOT_CLAIMABLE",
    }:
        return HTTPStatus.CONFLICT
    if code == "AUTHORITY_RELOAD_REQUIRED":
        return HTTPStatus.SERVICE_UNAVAILABLE
    return HTTPStatus.BAD_REQUEST


__all__ = [
    "ACCEPT_PATH",
    "CLAIM_PATH",
    "REGISTER_PATH",
    "handle_agent_first_current_post",
]
