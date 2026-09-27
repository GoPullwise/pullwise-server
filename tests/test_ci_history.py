import unittest

from pullwise_server.ci_history import error_signature, history_candidates


def item(identifier, text="AssertionError: expected 2", **facts):
    return {"id": identifier, "module": "ci", "repositoryId": "repo-1",
            "sourceUrl": f"https://github.com/acme/repo/actions/runs/{identifier}",
            "sourceFacts": {"runId": identifier, "jobId": identifier,
                            "completedAt": f"2026-09-{int(identifier) if identifier.isdigit() else 1:02d}T00:00:00Z",
                            "workflowId": "wf-1",
                            "jobName": "test (py3.12)", "windows": [{"windowId": "w1"}], **facts},
            "evidence": [{"id": f"ev-{identifier}", "segmentAnchor": "w1", "status": "available", "text": text}],
            "handlingHistory": []}


class CiHistoryTest(unittest.TestCase):
    def test_signature_is_conservative_and_uses_text_not_hash(self):
        self.assertEqual(error_signature(item("a", "\x1b[31m AssertionError:  expected 2 \x1b[0m")),
                         error_signature(item("b")))
        self.assertIsNone(error_signature(item("c", "Downloaded 2 packages")))
        self.assertIsNone(error_signature(item("d", "AssertionError: one\nAssertionError: two")))
        self.assertNotEqual(error_signature(item("a")), error_signature(item("b", "AssertionError: expected 3")))

    def test_candidates_require_identity_prior_handling_and_exact_signature(self):
        current = item("2")
        prior = item("1")
        prior["handlingHistory"] = [{"id": "h1", "eventKind": "user_update", "disposition": "done",
                                     "note": "Updated lockfile", "createdAt": 1}]
        distractors = [item("3"), item("4", "AssertionError: expected 3"),
                       item("5", jobName="test (py3.11)"), item("6", workflowId="wf-2"),
                       item("7", runId="2"), item("8", windows=[]),
                       item("9", matrix={"python": "3.11"})]
        distractors[0]["handlingHistory"] = prior["handlingHistory"]
        for row in distractors[1:]:
            row["handlingHistory"] = prior["handlingHistory"]
        self.assertEqual([row["itemId"] for row in history_candidates(current, [prior, *distractors])], ["1"])
        self.assertEqual(history_candidates(item("2", "partial log"), [prior]), [])
        self.assertEqual(history_candidates(item("2", matrix={"python": "3.12"}), [prior]), [])
        self.assertEqual(history_candidates(item("2", completedAt=None), [prior]), [])
        self.assertEqual(history_candidates(item("2"), [item("1", "AssertionError: expected 2")]), [])

    def test_signature_collision_never_becomes_cause_claim(self):
        current = item("2")
        prior = item("1")  # The same text could come from a different cause.
        prior["handlingHistory"] = [{"eventKind": "user_update", "disposition": "done",
                                     "note": "Changed service config", "createdAt": 1}]
        candidate = history_candidates(current, [prior])[0]
        self.assertEqual(candidate["relation"], "same_observed_symptom")
        self.assertNotIn("sameCause", candidate)
