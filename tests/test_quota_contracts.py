from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

from pullwise_server import db, quota


def make_user(user_id: str) -> dict:
    return {
        "id": user_id,
        "email": f"{user_id}@example.com",
        "billing": {"plan": "free", "status": "active"},
    }


class QuotaContractsTest(unittest.TestCase):
    def test_user_and_repository_buckets_are_period_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                user = make_user("usr_1")
                repository = db.upsert_repository({"github_repo_id": "123", "full_name": "acme/api"})

                user_usage = quota.quota_payload_for_user(user, timestamp=1_770_000_000)
                repo_usage = quota.quota_payload_for_repository(repository, user, timestamp=1_770_000_000)

        self.assertEqual(user_usage["scope"], "user")
        self.assertEqual(repo_usage["scope"], "repository")
        self.assertEqual(user_usage["period"], "2026-02")
        self.assertEqual(repo_usage["period"], "2026-02")
        self.assertEqual(user_usage["limit"], 10)
        self.assertEqual(repo_usage["limit"], 3)

    def test_user_limit_environment_variables_are_respected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_FREE_USER_REVIEW_LIMIT": "7",
                    "PULLWISE_PRO_USER_REVIEW_LIMIT": "70",
                },
                clear=True,
            ):
                free_user = make_user("usr_free")
                pro_user = {
                    **make_user("usr_pro"),
                    "billing": {"plan": "pro", "status": "active"},
                }

                free_usage = quota.quota_payload_for_user(free_user)
                pro_usage = quota.quota_payload_for_user(pro_user)

        self.assertEqual(free_usage["limit"], 7)
        self.assertEqual(pro_usage["limit"], 70)

    def test_quota_bucket_sanitizes_invalid_used_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                bucket = quota.ensure_quota_bucket(
                    scope_type="user",
                    scope_id="usr_1",
                    period="2026-05",
                    plan="free",
                    limit=10,
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    with connection:
                        connection.execute("UPDATE quota_buckets SET used = ? WHERE id = ?", ("not-a-number", bucket["id"]))

                sanitized = quota.ensure_quota_bucket(
                    scope_type="user",
                    scope_id="usr_1",
                    period="2026-05",
                    plan="free",
                    limit=10,
                )

        self.assertEqual(sanitized["used"], 0)

    def test_atomic_consume_succeeds_then_rejects_when_repo_limit_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_FREE_USER_REVIEW_LIMIT": "10",
                    "PULLWISE_FREE_REPO_REVIEW_LIMIT": "1",
                },
                clear=True,
            ):
                user = make_user("usr_1")
                repository = db.upsert_repository({"github_repo_id": "123", "full_name": "acme/api"})

                first = quota.consume_scan_quota(
                    user=user,
                    repository=repository,
                    requested_by_user_id="usr_1",
                    scan_id="sc_1",
                    request_id="req_1",
                )
                with self.assertRaises(quota.QuotaExceeded) as context:
                    quota.consume_scan_quota(
                        user=user,
                        repository=repository,
                        requested_by_user_id="usr_2",
                        scan_id="sc_2",
                        request_id="req_2",
                    )

        self.assertEqual(first["repository"]["used"], 1)
        self.assertEqual(context.exception.code, "QUOTA_EXCEEDED_REPOSITORY")

    def test_concurrent_consume_does_not_exceed_remaining_user_quota(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_FREE_USER_REVIEW_LIMIT": "1",
                    "PULLWISE_FREE_REPO_REVIEW_LIMIT": "10",
                },
                clear=True,
            ):
                user = make_user("usr_1")
                repository = db.upsert_repository({"github_repo_id": "123", "full_name": "acme/api"})
                successes = []
                failures = []

                def consume(index: int) -> None:
                    try:
                        successes.append(
                            quota.consume_scan_quota(
                                user=user,
                                repository=repository,
                                requested_by_user_id=f"usr_{index}",
                                scan_id=f"sc_{index}",
                                request_id=f"req_{index}",
                            )
                        )
                    except quota.QuotaExceeded as exc:
                        failures.append(exc.code)

                threads = [threading.Thread(target=consume, args=(index,)) for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(failures, ["QUOTA_EXCEEDED_USER"])

    def test_same_request_id_is_deduplicated_without_second_ledger_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path}, clear=True):
                user = make_user("usr_1")
                repository = db.upsert_repository({"github_repo_id": "123", "full_name": "acme/api"})

                quota.consume_scan_quota(
                    user=user,
                    repository=repository,
                    requested_by_user_id="usr_1",
                    scan_id="sc_1",
                    request_id="req_same",
                )
                second = quota.consume_scan_quota(
                    user=user,
                    repository=repository,
                    requested_by_user_id="usr_1",
                    scan_id="sc_2",
                    request_id="req_same",
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    ledger_count = connection.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0]

        self.assertTrue(second["deduplicated"])
        self.assertEqual(ledger_count, 2)

    def test_forks_share_repository_quota_with_source_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_FREE_USER_REVIEW_LIMIT": "10",
                    "PULLWISE_FREE_REPO_REVIEW_LIMIT": "1",
                },
                clear=True,
            ):
                user = make_user("usr_1")
                first_fork = db.upsert_repository(
                    {
                        "github_repo_id": "fork_1",
                        "full_name": "dev/fork-one",
                        "fork": True,
                        "source_github_repo_id": "source_1",
                    }
                )
                second_fork = db.upsert_repository(
                    {
                        "github_repo_id": "fork_2",
                        "full_name": "dev/fork-two",
                        "fork": True,
                        "source_github_repo_id": "source_1",
                    }
                )

                quota.consume_scan_quota(
                    user=user,
                    repository=first_fork,
                    requested_by_user_id="usr_1",
                    scan_id="sc_1",
                    request_id="req_1",
                )
                with self.assertRaises(quota.QuotaExceeded) as context:
                    quota.consume_scan_quota(
                        user=user,
                        repository=second_fork,
                        requested_by_user_id="usr_2",
                        scan_id="sc_2",
                        request_id="req_2",
                    )

        self.assertEqual(context.exception.code, "QUOTA_EXCEEDED_REPOSITORY")


if __name__ == "__main__":
    unittest.main()
