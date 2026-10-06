"""Validate decoded request text before it reaches providers or SQL parameters."""
from __future__ import annotations


def validate_json_unicode(value: object) -> None:
    """Reject unpaired surrogates and NUL in JSON strings and object keys.

    JSON escapes can decode to lone surrogates despite valid ASCII wire bytes.
    Valid surrogate pairs decode to Unicode scalar values and encode normally.
    SQLite length(text) stops at NUL; accepting it would give schema length
    checks a different string from the application and can fail a native batch.
    Iterate containers so validation does not add another recursion boundary.
    """
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            item.encode("utf-8")
            if "\x00" in item:
                raise UnicodeError("NUL is not supported in request text")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
