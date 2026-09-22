from __future__ import annotations

import unittest

from pullwise_server.product_domain import (
    HandlingSnapshot,
    ItemVersionState,
    UpdateUnitAnswers,
    WatchContextState,
    advance_item_version,
    advance_watch_context,
    carry_handling,
    context_hash,
    formal_review_actions,
    normalize_interests,
    project_existing_item_while_pending,
    project_update_unit,
    validate_watch_interests,
    watch_scope_key,
)


class ProductDomainContractsTest(unittest.TestCase):
    def test_watch_scope_survives_public_watch_id_recreation(self) -> None:
        first = watch_scope_key(
            owner_id="usr_1",
            target_repository_id=None,
            upstream_repository_id="github:123",
        )
        recreated = watch_scope_key(
            owner_id="usr_1",
            target_repository_id=None,
            upstream_repository_id="github:123",
        )

        self.assertEqual(first, recreated)
        self.assertNotIn("watch", first)
        self.assertNotEqual(
            first,
            watch_scope_key(
                owner_id="usr_2",
                target_repository_id=None,
                upstream_repository_id="github:123",
            ),
        )

    def test_shared_and_personal_watch_domains_cannot_collide(self) -> None:
        personal = watch_scope_key(
            owner_id="repo_1",
            target_repository_id=None,
            upstream_repository_id="github:123",
        )
        shared = watch_scope_key(
            owner_id="usr_1",
            target_repository_id="repo_1",
            upstream_repository_id="github:123",
        )

        self.assertNotEqual(personal, shared)

    def test_context_hash_reuses_semantics_but_context_version_never_rewinds(self) -> None:
        interests_a = [" OAuth  登录 ", "数据库迁移", "OAuth 登录"]
        interests_b = ["breaking changes"]
        initial = WatchContextState(
            context_version=1,
            context_hash=context_hash(interests_a),
            interests=normalize_interests(interests_a),
        )

        changed = advance_watch_context(initial, interests_b)
        returned = advance_watch_context(changed, ["数据库迁移", "OAuth 登录"])
        unchanged = advance_watch_context(returned, ["OAuth 登录", "数据库迁移"])

        self.assertEqual(changed.context_version, 2)
        self.assertEqual(returned.context_version, 3)
        self.assertEqual(returned.context_hash, initial.context_hash)
        self.assertEqual(unchanged, returned)

    def test_item_version_is_monotonic_across_a_b_a_and_adjacent_noop(self) -> None:
        first = advance_item_version(None, snapshot={"actions": ["reply_needed"], "sourceRevision": 1})
        no_change = advance_item_version(
            first,
            snapshot={"sourceRevision": 1, "actions": ["reply_needed"]},
        )
        changed = advance_item_version(
            no_change,
            snapshot={"actions": ["change_requested"], "sourceRevision": 2},
        )
        returned = advance_item_version(
            changed,
            snapshot={"actions": ["reply_needed"], "sourceRevision": 3},
        )

        self.assertEqual(first.item_version, 1)
        self.assertIs(no_change, first)
        self.assertEqual(changed.item_version, 2)
        self.assertEqual(returned.item_version, 3)
        self.assertNotEqual(returned.snapshot_hash, first.snapshot_hash)

    def test_multi_source_change_advances_item_version_even_when_actions_match(self) -> None:
        current = advance_item_version(
            None,
            snapshot={
                "actions": ["change_requested"],
                "sources": [
                    {"sourceId": "parent", "sourceRevision": 1},
                    {"sourceId": "reply", "sourceRevision": 1},
                ],
            },
        )
        parent_edited = advance_item_version(
            current,
            snapshot={
                "actions": ["change_requested"],
                "sources": [
                    {"sourceId": "parent", "sourceRevision": 2},
                    {"sourceId": "reply", "sourceRevision": 1},
                ],
            },
        )

        self.assertEqual(parent_edited.item_version, 2)
        self.assertNotEqual(parent_edited.snapshot_hash, current.snapshot_hash)

    def test_done_carries_only_to_unique_unchanged_adjacent_action(self) -> None:
        previous = HandlingSnapshot(disposition="done", item_version=4)

        inherited = carry_handling(
            previous,
            previous_action_signature="sig_a",
            next_action_signature="sig_a",
            matching_predecessors=1,
            context_changed=False,
            new_rule_action=False,
            next_item_version=5,
        )
        new_action = carry_handling(
            previous,
            previous_action_signature="sig_a",
            next_action_signature="sig_a",
            matching_predecessors=1,
            context_changed=False,
            new_rule_action=True,
            next_item_version=5,
        )
        ambiguous = carry_handling(
            previous,
            previous_action_signature="sig_a",
            next_action_signature="sig_a",
            matching_predecessors=2,
            context_changed=False,
            new_rule_action=False,
            next_item_version=5,
        )

        self.assertEqual(inherited.disposition, "done")
        self.assertEqual(inherited.carried_from_item_version, 4)
        self.assertEqual(new_action.disposition, "open")
        self.assertIsNone(new_action.carried_from_item_version)
        self.assertEqual(ambiguous.disposition, "open")

    def test_updates_context_change_never_carries_old_done(self) -> None:
        previous = HandlingSnapshot(disposition="dismissed", item_version=2)

        result = carry_handling(
            previous,
            previous_action_signature="same-content",
            next_action_signature="same-content",
            matching_predecessors=1,
            context_changed=True,
            new_rule_action=False,
            next_item_version=3,
        )

        self.assertEqual(result.disposition, "open")
        self.assertIsNone(result.carried_from_item_version)

    def test_empty_body_formal_request_changes_is_a_rule_action(self) -> None:
        self.assertEqual(
            formal_review_actions(review_state="CHANGES_REQUESTED", body=""),
            ("change_requested",),
        )
        self.assertEqual(formal_review_actions(review_state="APPROVED", body=""), ())

    def test_pending_assessment_keeps_existing_action_visible_without_reopening_attention_time(self) -> None:
        projection = project_existing_item_while_pending(
            previous_attention_state="needs_action",
            previous_attention_updated_at="2026-09-20T08:00:00Z",
        )

        self.assertEqual(projection.attention_state, "needs_confirmation")
        self.assertEqual(projection.attention_updated_at, "2026-09-20T08:00:00Z")
        self.assertEqual(projection.processing_status, "pending")

    def test_updates_joint_projection_requires_relevance_and_action_on_same_unit(self) -> None:
        action = project_update_unit(
            UpdateUnitAnswers(
                relevance="relevant",
                migration_stated="present",
                deprecation_stated="absent",
                breaking_change_stated="absent",
                security_fix_stated="absent",
            ),
            coverage_complete=True,
        )
        conflict = project_update_unit(
            UpdateUnitAnswers(
                relevance="not_relevant",
                migration_stated="present",
                deprecation_stated="absent",
                breaking_change_stated="absent",
                security_fix_stated="absent",
            ),
            coverage_complete=True,
        )

        self.assertEqual(action.attention_state, "needs_action")
        self.assertEqual(action.action_types, ("review_update",))
        self.assertEqual(action.update_signals, ("migration",))
        self.assertEqual(conflict.attention_state, "needs_confirmation")
        self.assertEqual(conflict.action_types, ())

    def test_partial_updates_coverage_never_infers_negative_release_result(self) -> None:
        result = project_update_unit(
            UpdateUnitAnswers(
                relevance="relevant",
                migration_stated="absent",
                deprecation_stated="absent",
                breaking_change_stated="absent",
                security_fix_stated="absent",
            ),
            coverage_complete=False,
        )

        self.assertEqual(result.attention_state, "optional")
        self.assertEqual(result.release_relevance, "relevant")
        self.assertIsNone(result.release_signal_states["migration"])
        self.assertTrue(result.coverage_partial)

    def test_domain_objects_reject_invalid_versions_and_dispositions(self) -> None:
        with self.assertRaises(ValueError):
            WatchContextState(context_version=0, context_hash="x", interests=())
        with self.assertRaises(ValueError):
            ItemVersionState(item_version=0, snapshot_hash="x")
        with self.assertRaises(ValueError):
            HandlingSnapshot(disposition="closed", item_version=1)

    def test_watch_interests_require_one_to_twenty_topics_within_two_kib(self) -> None:
        self.assertEqual(validate_watch_interests([" OAuth ", "OAuth", "数据库迁移"]), ("OAuth", "数据库迁移"))
        for invalid in ([], [f"topic-{index}" for index in range(21)], ["x" * 2049]):
            with self.subTest(invalid=invalid[:2]):
                with self.assertRaises(ValueError):
                    validate_watch_interests(invalid)


if __name__ == "__main__":
    unittest.main()
