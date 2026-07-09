"""Tests that /v1/runs stamps db.end_session() in state.db via _run_and_close().

Verifies that completed/failed/cancelled runs all set ended_at + end_reason,
and that message active columns are NOT changed (context boundary preserved).
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def adapter_with_db(session_db):
    """Adapter with a real SessionDB so end_session() writes to state.db."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    return adapter


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/runs/{run_id}/events", adapter._handle_run_events)
    app.router.add_post("/v1/runs/{run_id}/approval", adapter._handle_run_approval)
    app.router.add_post("/v1/runs/{run_id}/stop", adapter._handle_stop_run)
    return app


def _make_mock_agent(final_response="done", failed=False):
    """Mock agent that returns immediately."""
    mock = MagicMock()
    if failed:
        mock.run_conversation.return_value = {"failed": True, "error": "test error"}
    else:
        mock.run_conversation.return_value = {"final_response": final_response}
    mock.session_prompt_tokens = 0
    mock.session_completion_tokens = 0
    mock.session_total_tokens = 0
    return mock


def _make_slow_agent():
    """Mock agent that blocks until interrupted."""
    ready = threading.Event()
    interrupted = threading.Event()

    mock_agent = MagicMock()

    def _do_interrupt(message=None):
        interrupted.set()

    mock_agent.interrupt = MagicMock(side_effect=_do_interrupt)

    def _slow_run(user_message=None, conversation_history=None, task_id=None):
        ready.set()
        interrupted.wait(timeout=10)
        return {"final_response": "interrupted"}

    mock_agent.run_conversation.side_effect = _slow_run
    mock_agent.session_prompt_tokens = 0
    mock_agent.session_completion_tokens = 0
    mock_agent.session_total_tokens = 0

    return mock_agent, ready, interrupted


async def _wait_for_status(cli, run_id, target_status, timeout=5.0):
    """Poll /v1/runs/{run_id} until status matches target."""
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        resp = await cli.get(f"/v1/runs/{run_id}")
        if resp.status == 200:
            data = await resp.json()
            if data["status"] == target_status:
                return data
        await asyncio.sleep(0.05)
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndSessionStamp:
    """_run_and_close() must stamp db.end_session() in the finally block."""

    @pytest.mark.asyncio
    async def test_completed_run_stamps_ended_at_and_reason(self, adapter_with_db, session_db):
        """A completed run must set ended_at + end_reason='completed'."""
        session_id = "test-completed-session"
        session_db.create_session(session_id, "api_server")

        app = _create_runs_app(adapter_with_db)
        with patch.object(adapter_with_db, "_create_agent") as mock_create:
            mock_create.return_value = _make_mock_agent("done")

            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello", "session_id": session_id})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                await _wait_for_status(cli, run_id, "completed")

        row = session_db.get_session(session_id)
        assert row is not None
        assert row["ended_at"] is not None, "ended_at must be stamped"
        assert row["end_reason"] == "completed"

    @pytest.mark.asyncio
    async def test_failed_run_stamps_end_reason_failed(self, adapter_with_db, session_db):
        """A structured-failure run must set end_reason='failed'."""
        session_id = "test-failed-session"
        session_db.create_session(session_id, "api_server")

        app = _create_runs_app(adapter_with_db)
        with patch.object(adapter_with_db, "_create_agent") as mock_create:
            mock_create.return_value = _make_mock_agent(failed=True)

            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello", "session_id": session_id})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                await _wait_for_status(cli, run_id, "failed")

        row = session_db.get_session(session_id)
        assert row is not None
        assert row["ended_at"] is not None
        assert row["end_reason"] == "failed"

    @pytest.mark.asyncio
    async def test_exception_run_stamps_end_reason_failed(self, adapter_with_db, session_db):
        """A run that raises an exception must set end_reason='failed'."""
        session_id = "test-exception-session"
        session_db.create_session(session_id, "api_server")

        app = _create_runs_app(adapter_with_db)
        with patch.object(adapter_with_db, "_create_agent") as mock_create:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = RuntimeError("boom")
            mock_agent.session_prompt_tokens = 0
            mock_agent.session_completion_tokens = 0
            mock_agent.session_total_tokens = 0
            mock_create.return_value = mock_agent

            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello", "session_id": session_id})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                await _wait_for_status(cli, run_id, "failed")

        row = session_db.get_session(session_id)
        assert row is not None
        assert row["ended_at"] is not None
        assert row["end_reason"] == "failed"

    @pytest.mark.asyncio
    async def test_cancelled_run_stamps_end_reason_cancelled(self, adapter_with_db, session_db):
        """A cancelled run must set end_reason='cancelled'."""
        session_id = "test-cancelled-session"
        session_db.create_session(session_id, "api_server")

        slow_agent, ready, interrupted = _make_slow_agent()

        app = _create_runs_app(adapter_with_db)
        with patch.object(adapter_with_db, "_create_agent") as mock_create:
            mock_create.return_value = slow_agent

            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello", "session_id": session_id})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                # Wait for the agent to start, then stop it
                ready.wait(timeout=5)
                stop_resp = await cli.post(f"/v1/runs/{run_id}/stop")
                assert stop_resp.status == 200

                await _wait_for_status(cli, run_id, "cancelled")

        row = session_db.get_session(session_id)
        assert row is not None
        assert row["ended_at"] is not None
        assert row["end_reason"] == "cancelled"

    @pytest.mark.asyncio
    async def test_message_active_column_unched_after_run(self, adapter_with_db, session_db):
        """The active column on messages must NOT be changed by end_session stamp.

        active is the message-history/context boundary; changing it risks
        breaking agents' context. end_session stamps ended_at/end_reason only.
        """
        session_id = "test-active-preserved"
        session_db.create_session(session_id, "api_server")
        session_db.append_message(session_id, "user", "hello before run")
        session_db.append_message(session_id, "assistant", "acknowledged")

        app = _create_runs_app(adapter_with_db)
        with patch.object(adapter_with_db, "_create_agent") as mock_create:
            mock_create.return_value = _make_mock_agent("done")

            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello", "session_id": session_id})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                await _wait_for_status(cli, run_id, "completed")

        # All messages must still have active=1
        msgs = session_db.get_messages(session_id)
        assert len(msgs) == 2
        for msg in msgs:
            assert msg["active"] == 1, f"message {msg['id']} active={msg['active']}, expected 1"

    @pytest.mark.asyncio
    async def test_idempotent_end_session_first_reason_wins(self, adapter_with_db, session_db):
        """end_session() is idempotent — first reason wins, second call no-ops."""
        session_id = "test-idempotent"
        session_db.create_session(session_id, "api_server")

        app = _create_runs_app(adapter_with_db)
        with patch.object(adapter_with_db, "_create_agent") as mock_create:
            mock_create.return_value = _make_mock_agent("done")

            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post("/v1/runs", json={"input": "hello", "session_id": session_id})
                assert resp.status == 202
                data = await resp.json()
                run_id = data["run_id"]

                await _wait_for_status(cli, run_id, "completed")

        # Session should be ended with reason "completed"
        row = session_db.get_session(session_id)
        assert row["end_reason"] == "completed"

        # Manual call with different reason should NOT overwrite
        session_db.end_session(session_id, "manual_override")
        row = session_db.get_session(session_id)
        assert row["end_reason"] == "completed", "first end_reason must win"
