from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoredSecretVersion:
    version: str
    fingerprint: str


class RecordingSecretWriter:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []
        self.validations: list[tuple[str, str]] = []
        self.canaries: list[tuple[str, str]] = []
        self.retirements: list[tuple[str, str]] = []

    def put_candidate(self, secret_ref: str, secret: bytes) -> StoredSecretVersion:
        self.writes.append((secret_ref, secret))
        return StoredSecretVersion(
            version=f"version-{len(self.writes)}",
            fingerprint=(
                "sha256:0123456789ab"
                if len(self.writes) == 1
                else f"sha256:{len(self.writes):012x}"
            ),
        )

    def validate_candidate(self, secret_ref: str, version: str, **_metadata: object) -> list[str]:
        self.validations.append((secret_ref, version))
        return [
            "gpt-5.1",
            "gpt-5.5",
            "gpt-5.6",
            "gpt-5.7",
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "MiniMax-M2.7",
        ]

    def canary_candidate(self, secret_ref: str, version: str, **_metadata: object) -> None:
        self.canaries.append((secret_ref, version))

    def retire_version(self, secret_ref: str, version: str) -> None:
        self.retirements.append((secret_ref, version))


class LeakyFailingSecretWriter:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def put_candidate(self, secret_ref: str, secret: bytes) -> StoredSecretVersion:
        del secret_ref, secret
        raise RuntimeError(f"broker rejected {self.secret}")

