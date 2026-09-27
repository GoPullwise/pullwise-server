"""Versioned policy for automatic publication of visible CI symptoms."""

from dataclasses import dataclass
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class AutoLabelPolicy:
    version: str
    minimum_confidence: float


AUTO_LABEL_POLICY = AutoLabelPolicy("ci-auto-label/v1", 0.8)


def publish_symptom(answer: Mapping, policy: AutoLabelPolicy = AUTO_LABEL_POLICY) -> bool:
    confidence = answer.get("confidence")
    return (answer.get("choice") == "present"
            and type(confidence) in (float, int)
            and isfinite(confidence) and 0 <= confidence <= 1
            and confidence >= policy.minimum_confidence)
