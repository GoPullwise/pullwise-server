from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from pullwise_server.agent_first_authority import AuthorityError
from pullwise_server.agent_first_release_root_pin_migrations import (
    CURRENT_RELEASE_ROOT_PIN_TABLE,
)
from pullwise_server.agent_first_release_root_pins import (
    RELEASE_ROOT_PIN_FAULT_POINTS,
)
from pullwise_server.agent_first_release_trust import AgentFirstReleaseTrust
from pullwise_server.agent_first_release_trust_migrations import (
    install_current_release_trust_tables,
)


ORGANIZATION_ID = "org_pullwise"
ROOT_DIGEST = "a" * 64


class AgentFirstReleaseRootPinsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "release-root-pins.sqlite3"
        with closing(self.connect()) as connection:
            install_current_release_trust_tables(connection)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def pin_count(self) -> int:
        with closing(self.connect()) as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM {CURRENT_RELEASE_ROOT_PIN_TABLE}"
            ).fetchone()
        assert row is not None
        return int(row[0])

    def test_all_pin_fault_points_roll_back_enrollment(self) -> None:
        for point in RELEASE_ROOT_PIN_FAULT_POINTS:
            with self.subTest(point=point):
                def fault(candidate: str) -> None:
                    if candidate == point:
                        raise RuntimeError(point)

                trust = AgentFirstReleaseTrust(
                    self.connect,
                    fault_injector=fault,
                )

                with self.assertRaisesRegex(RuntimeError, point):
                    trust.enroll_root_pin(ORGANIZATION_ID, ROOT_DIGEST)

                self.assertEqual(0, self.pin_count())

    def test_exact_replay_is_a_noop_and_pin_rows_are_immutable(self) -> None:
        trust = AgentFirstReleaseTrust(self.connect)

        first = trust.enroll_root_pin(ORGANIZATION_ID, ROOT_DIGEST)
        second = trust.enroll_root_pin(ORGANIZATION_ID, ROOT_DIGEST)

        self.assertEqual(first, second)
        self.assertEqual(1, self.pin_count())
        with closing(self.connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE {CURRENT_RELEASE_ROOT_PIN_TABLE} "
                    "SET created_at = created_at + 1"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    f"DELETE FROM {CURRENT_RELEASE_ROOT_PIN_TABLE}"
                )

    def test_concurrent_exact_enrollment_converges(self) -> None:
        trust = AgentFirstReleaseTrust(self.connect)
        outcomes: list[object] = []
        outcome_lock = threading.Lock()

        def enroll() -> None:
            try:
                outcome: object = trust.enroll_root_pin(
                    ORGANIZATION_ID, ROOT_DIGEST
                )
            except BaseException as error:
                outcome = error
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=enroll) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(2, len(outcomes))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertFalse(any(isinstance(item, BaseException) for item in outcomes))
        self.assertEqual(1, self.pin_count())

    def test_invalid_pin_identifiers_fail_closed_without_writes(self) -> None:
        trust = AgentFirstReleaseTrust(self.connect)
        invalid_values: tuple[tuple[object, object], ...] = (
            ("org_", ROOT_DIGEST),
            ("org_Pullwise", ROOT_DIGEST),
            (ORGANIZATION_ID, "a" * 63),
            (ORGANIZATION_ID, "A" * 64),
            (None, ROOT_DIGEST),
        )

        for organization_id, root_digest in invalid_values:
            with self.subTest(
                organization_id=organization_id,
                root_digest=root_digest,
            ):
                with self.assertRaises(AuthorityError) as raised:
                    trust.enroll_root_pin(  # type: ignore[arg-type]
                        organization_id, root_digest
                    )
                self.assertEqual(
                    "AUTHORITY_INPUT_UNTRUSTED", raised.exception.code
                )

        self.assertEqual(0, self.pin_count())

    def test_pin_storage_failure_requires_authority_reload(self) -> None:
        trust = AgentFirstReleaseTrust(self.connect)
        with closing(self.connect()) as connection, connection:
            connection.execute(f"DROP TABLE {CURRENT_RELEASE_ROOT_PIN_TABLE}")

        with self.assertRaises(AuthorityError) as raised:
            trust.enroll_root_pin(ORGANIZATION_ID, ROOT_DIGEST)

        self.assertEqual("AUTHORITY_RELOAD_REQUIRED", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
