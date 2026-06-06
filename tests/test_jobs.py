from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.approval_policy import ApprovalRequired, fingerprint, wrap_tools
from backend.chat import AgentChat
from backend.scheduling import initial_due, iso_utc, utc_now
from backend.store import AgentStore
from backend.worker_service import scan_due
from backend.worker_service import execute_claimed
from backend.worker_service import _singleton_lock


class _Reply:
    content = "done"


class _LLM:
    def invoke(self, _messages):
        return _Reply()


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "company.db"
        self.store = AgentStore(self.db)
        self.agent = self.store.hire("Ada", "Engineer")

    def tearDown(self):
        self.tmp.cleanup()

    def test_chat_persists_one_human_and_one_ai_turn(self):
        with patch("backend.chat.get_llm", return_value=_LLM()), \
                patch("backend.chat.build_tools_sync", return_value=[]):
            AgentChat(self.agent.id, self.store).send("hello")
        history = self.store.history(self.agent.id)
        self.assertEqual([(m.role, m.content) for m in history],
                         [("human", "hello"), ("ai", "done")])

    def test_due_once_and_heartbeat_enqueue_once(self):
        past = iso_utc(utc_now())
        job = self.store.create_job(
            self.agent.id, "once", "report", "once", past,
            "America/Los_Angeles", past,
        )
        self.store.create_heartbeat(self.agent.id, "pulse", "inspect", 60, past)
        self.assertEqual(scan_due(self.store), 2)
        self.assertEqual(scan_due(self.store), 0)
        self.assertFalse(self.store.get_job(job.id).enabled)
        self.assertEqual({r.source_type for r in self.store.list_runs()},
                         {"job", "heartbeat"})

    def test_overdue_interval_catches_up_once_and_advances(self):
        past = "2020-01-01T00:00:00+00:00"
        job = self.store.create_job(
            self.agent.id, "interval", "report", "interval", "60",
            "America/Los_Angeles", past,
        )
        self.assertEqual(scan_due(self.store), 1)
        self.assertEqual(scan_due(self.store), 0)
        self.assertGreater(self.store.get_job(job.id).next_run_at, iso_utc(utc_now()))

    def test_fired_agent_does_not_receive_work(self):
        self.store.fire(self.agent.id)
        row = self.store.enqueue_manual_run(self.agent.id, "report", iso_utc(utc_now()))
        self.assertIsNone(row)

    def test_worker_restart_marks_running_run_as_error(self):
        self.store.enqueue_manual_run(self.agent.id, "report", iso_utc(utc_now()))
        run = self.store.claim_next_run()
        self.assertEqual(self.store.fail_interrupted_runs(), 1)
        self.assertEqual(self.store.get_run(run.id).status, "error")

    def test_worker_service_lock_is_singleton(self):
        with _singleton_lock(self.store):
            with self.assertRaises(RuntimeError):
                with _singleton_lock(self.store):
                    pass

    def test_claimed_run_persists_report(self):
        self.store.enqueue_manual_run(self.agent.id, "report", iso_utc(utc_now()))
        run = self.store.claim_next_run()
        with patch("backend.worker_service.execute_run", return_value="finished"):
            execute_claimed(self.store, run)
        saved = self.store.get_run(run.id)
        self.assertEqual((saved.status, saved.report), ("done", "finished"))
        self.assertEqual(self.store.history(self.agent.id), [])

    def test_claim_is_atomic(self):
        self.store.enqueue_manual_run(self.agent.id, "report", iso_utc(utc_now()))
        self.assertIsNotNone(self.store.claim_next_run())
        self.assertIsNone(self.store.claim_next_run())

    def test_cron_uses_named_timezone(self):
        now = datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc)
        due = initial_due("cron", "0 9 * * *", "America/Los_Angeles", now)
        self.assertEqual(due, datetime(2026, 6, 3, 16, 0, tzinfo=timezone.utc))

    def test_critical_tool_needs_one_time_grant(self):
        from langchain_core.tools import tool

        @tool
        def drive_delete(path: str) -> str:
            """Delete a test path."""
            return f"deleted {path}"

        run = self.store.enqueue_manual_run(self.agent.id, "clean", iso_utc(utc_now()))
        wrapped = wrap_tools([drive_delete], self.store, run.id, "trusted")[0]
        args = {"path": "/old.txt"}
        with self.assertRaises(ApprovalRequired):
            asyncio.run(wrapped.ainvoke(args))
        mark = fingerprint("drive_delete", args)
        approval = self.store.wait_for_approval(
            run.id, "drive_delete", '{"path":"/old.txt"}', mark, "critical"
        )
        self.store.decide_approval(approval.id, "approved")
        self.assertEqual(asyncio.run(wrapped.ainvoke(args)), "deleted /old.txt")
        with self.assertRaises(ApprovalRequired):
            asyncio.run(wrapped.ainvoke(args))

    def test_same_action_can_be_rejected_more_than_once(self):
        run = self.store.enqueue_manual_run(self.agent.id, "clean", iso_utc(utc_now()))
        for _ in range(2):
            approval = self.store.wait_for_approval(
                run.id, "drive_delete", '{"path":"/old.txt"}', "same", "critical"
            )
            self.store.decide_approval(approval.id, "rejected")
        rejected = self.store.list_approvals(decision="rejected")
        self.assertEqual(len(rejected), 2)


if __name__ == "__main__":
    unittest.main()
