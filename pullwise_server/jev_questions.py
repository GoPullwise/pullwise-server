from __future__ import annotations


PR_QUESTION_VERSION = "pr-followup/v3"
CI_QUESTION_VERSION = "ci-triage/v2"
UPDATES_QUESTION_VERSION = "updates-filter/v3"

_PRESENT_ABSENT_UNCLEAR = {
    "present": "The requested semantic signal is explicitly present in the target text.",
    "absent": "The target text does not express the requested semantic signal.",
    "unclear": "The supplied text or role/context is insufficient or ambiguous.",
}


def _choice(instructions: str, criteria: dict[str, str] | None = None) -> dict:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(criteria or _PRESENT_ABSENT_UNCLEAR),
    }


def _count(value: int, *, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} count must be within 1..{maximum}")
    return value


def pr_questions(segment_count: int) -> dict[str, dict]:
    count = _count(segment_count, maximum=8, label="segment")
    questions = {}
    semantic_tasks = {
        "change_request": "Does the speaker explicitly request a change or addition to implementation, tests, or material? Optional suggestions alone are absent.",
        "question": "Does the speaker ask for an answer or explanation? A grammatical question that does not expect a response is absent.",
        "optional_suggestion": "Does the speaker explicitly make a non-blocking optional suggestion?",
        "rereview_request": "Does the speaker explicitly request another review?",
        "completion_claim": "Does the speaker claim that some requested work was handled? Do not judge whether it was actually fixed.",
    }
    for index in range(count):
        path = f"state.segments[{index}]"
        for name, task in semantic_tasks.items():
            questions[f"s{index}_{name}"] = _choice(
                f"Evaluate only {path} as evidence, using its supplied speaker role and quotation context. {task}"
            )
        questions[f"s{index}_blocking_language"] = _choice(
            f"Evaluate only {path}. Does its current speaker explicitly say this text is blocking or non-blocking? Do not infer from tone or from GitHub review state.",
            {
                "explicit_blocking": "The text explicitly says the request blocks approval, merge, or progress.",
                "explicit_nonblocking": "The text explicitly says the request is optional or non-blocking.",
                "not_stated": "No explicit blocking status is stated.",
                "unclear": "Quotation, role, or wording makes the explicit status ambiguous.",
            },
        )
    return questions


_CI_SYMPTOMS = (
    "connection_timeout",
    "name_resolution_failure",
    "authentication_denied",
    "authorization_denied",
    "assertion_failure",
    "syntax_or_type_error",
    "package_resolution_failure",
    "resource_exhausted",
    "configuration_error",
)


def ci_questions(window_count: int) -> dict[str, dict]:
    count = _count(window_count, maximum=5, label="window")
    questions = {}
    for index in range(count):
        path = f"state.windows[{index}]"
        for symptom in _CI_SYMPTOMS:
            questions[f"w{index}_{symptom}"] = _choice(
                f"Evaluate only the visible log evidence in {path}. Is the observable symptom {symptom} present? Classify the visible symptom, not a hidden root cause."
            )
    return questions


_UPDATE_SIGNALS = (
    "migration_stated",
    "deprecation_stated",
    "breaking_change_stated",
    "security_fix_stated",
)


def update_questions(unit_count: int) -> dict[str, dict]:
    count = _count(unit_count, maximum=8, label="change unit")
    questions = {}
    for index in range(count):
        path = f"state.changeUnits[{index}]"
        questions[f"u{index}_relevance"] = _choice(
            f"Evaluate only {path} against state.interests. Is the change in this unit clearly related to the supplied interests? Mixed topics with no locatable relationship are unclear.",
            {
                "relevant": "This unit contains a change clearly related to the supplied interests.",
                "not_relevant": "This unit is clearly unrelated to the supplied interests.",
                "unclear": "The relationship is ambiguous, mixed, or not locatable in this unit.",
            },
        )
        task_text = {
            "migration_stated": "an explicit migration action",
            "deprecation_stated": "an explicit deprecation",
            "breaking_change_stated": "an explicit breaking change",
            "security_fix_stated": "an explicit security fix statement",
        }
        for signal in _UPDATE_SIGNALS:
            questions[f"u{index}_{signal}"] = _choice(
                f"Evaluate only {path} and state.interests. For the same change in this unit that relates to the interests, is {task_text[signal]} explicitly stated? Do not borrow a signal from another feature or section."
            )
    return questions
