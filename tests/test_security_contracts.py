from __future__ import annotations

import importlib
import unittest

_PART_MODULES = (
    "security_contracts_part_01",
    "security_contracts_part_02",
    "security_contracts_part_03",
    "security_contracts_part_04",
    "security_contracts_part_05",
    "security_contracts_part_06",
    "security_contracts_part_07",
)


def _import_part(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        if not __package__:
            raise
        return importlib.import_module(f"{__package__}.{module_name}")


for _module_name in _PART_MODULES:
    _module = _import_part(_module_name)
    for _name in getattr(_module, "__all__", ()):
        globals()[_name] = getattr(_module, _name)

del _module_name, _module, _name, _import_part, _PART_MODULES, importlib


if __name__ == "__main__":
    unittest.main()
