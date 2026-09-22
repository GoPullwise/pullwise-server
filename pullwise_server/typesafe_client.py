from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence


MAX_REQUEST_BYTES = 48 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUESTIONS = 48
DEFAULT_JEV_MODEL = "jev-1.13.0"
_MEDIA_KEYS = {
    "image",
    "images",
    "image_url",
    "audio",
    "audio_url",
    "video",
    "video_url",
}
_MEDIA_DATA_PREFIXES = ("data:image/", "data:audio/", "data:video/")


def _duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_text_only_state(value: object, *, path: str = "state") -> None:
    if isinstance(value, str):
        if value.strip().lower().startswith(_MEDIA_DATA_PREFIXES):
            raise ValueError(f"text-only state cannot contain media data at {path}")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"text-only state keys must be non-empty strings at {path}")
            if key.casefold() in _MEDIA_KEYS:
                raise ValueError(f"text-only state cannot contain media field {path}.{key}")
            _validate_text_only_state(child, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_text_only_state(child, path=f"{path}[{index}]")
        return
    raise ValueError(f"text-only state accepts strings, JSON objects, or arrays of text values at {path}")


def build_request(
    *,
    state: object,
    questions: Mapping[str, Mapping[str, object]],
    model: str,
) -> dict:
    _validate_text_only_state(state)
    model = model.strip() if isinstance(model, str) else ""
    if model != DEFAULT_JEV_MODEL:
        raise ValueError(f"fixed model must be {DEFAULT_JEV_MODEL}")
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"question limit is 1..{MAX_QUESTIONS}")
    normalized_questions = {}
    for question_id, question in questions.items():
        if not isinstance(question_id, str) or not question_id.strip() or not isinstance(question, Mapping):
            raise ValueError("question ids and definitions must be non-empty")
        if question.get("type") != "choice":
            raise ValueError("only choice questions are supported")
        instructions = question.get("instructions")
        criteria = question.get("criteria")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("choice instructions must be non-empty")
        if not isinstance(criteria, Mapping) or not 2 <= len(criteria) <= 255:
            raise ValueError("choice criteria must contain 2..255 options")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(description, str)
            or not description.strip()
            for key, description in criteria.items()
        ):
            raise ValueError("choice criteria keys and descriptions must be non-empty strings")
        normalized_questions[question_id] = {
            "type": "choice",
            "instructions": instructions,
            "criteria": dict(criteria),
        }
    payload = {
        "state": state,
        "model": model,
        "questions": normalized_questions,
    }
    try:
        encoded = _canonical_bytes(payload)
    except (TypeError, ValueError) as error:
        raise ValueError("request must be finite JSON data") from error
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError(f"request byte limit is {MAX_REQUEST_BYTES}")
    return json.loads(encoded)


def _probability(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return number


def _token_count(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def validate_response(
    raw_body: bytes,
    *,
    expected_questions: Mapping[str, Sequence[str]],
    requested_model: str,
    request_id: str | None = None,
) -> dict:
    if not isinstance(raw_body, bytes) or not raw_body or len(raw_body) > MAX_RESPONSE_BYTES:
        raise ValueError("response body is empty or exceeds the response byte limit")
    try:
        payload = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("response is not strict JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("response must be an object")
    returned_model = payload.get("model")
    if returned_model != requested_model or not isinstance(returned_model, str):
        raise ValueError("returned model does not match the requested fixed model")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping) or set(answers) != set(expected_questions):
        raise ValueError("response answer keys do not exactly match requested questions")
    normalized_answers = {}
    for question_id, expected_options in expected_questions.items():
        options = tuple(expected_options)
        answer = answers[question_id]
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            raise ValueError(f"answer {question_id} is not a choice")
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
        if not isinstance(choice, str) or choice not in options:
            raise ValueError(f"answer {question_id} has an invalid choice")
        if not isinstance(probabilities, Mapping) or set(probabilities) != set(options):
            raise ValueError(f"answer {question_id} probability keys do not match criteria")
        normalized_probabilities = {
            option: _probability(probabilities[option], f"{question_id}.probabilities.{option}")
            for option in options
        }
        if abs(sum(normalized_probabilities.values()) - 1.0) > 1e-3:
            raise ValueError(f"answer {question_id} probabilities do not sum to one")
        maximum = max(normalized_probabilities.values())
        if normalized_probabilities[choice] < maximum - 1e-12:
            raise ValueError(f"answer {question_id} choice is not a maximum-probability option")
        confidence = _probability(answer.get("confidence"), f"{question_id}.confidence")
        normalized_answers[question_id] = {
            "type": "choice",
            "choice": choice,
            "probabilities": normalized_probabilities,
            "confidence": confidence,
        }
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ValueError("response usage is required")
    return {
        "model": returned_model,
        "requestId": request_id,
        "answers": normalized_answers,
        "usage": {
            "inputTokens": _token_count(usage.get("input_tokens"), "usage.input_tokens"),
            "outputTokens": _token_count(usage.get("output_tokens"), "usage.output_tokens"),
        },
    }
