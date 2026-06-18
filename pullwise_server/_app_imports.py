from __future__ import annotations

_SKIPPED_GLOBALS = {
    "__builtins__",
    "__cached__",
    "__doc__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
}
_COMPAT_TARGETS: list[dict] = []


def import_compat_globals(source: dict, target: dict) -> None:
    for name, value in source.items():
        if name in _SKIPPED_GLOBALS:
            continue
        target.setdefault(name, value)


def register_compat_targets(*targets: object) -> None:
    _COMPAT_TARGETS.clear()
    for target in targets:
        if isinstance(target, dict):
            _COMPAT_TARGETS.append(target)
        elif hasattr(target, "__dict__"):
            _COMPAT_TARGETS.append(vars(target))


def sync_compat_globals(source: dict, names: tuple[str, ...]) -> None:
    for name in names:
        if name in _SKIPPED_GLOBALS or name not in source:
            continue
        value = source[name]
        for target in _COMPAT_TARGETS:
            if target is source:
                continue
            target[name] = value
