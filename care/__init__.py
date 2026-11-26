"""``care`` 包是 :mod:`backend` 的同名别名，兼容以报文前缀命名的导入习惯。

``from care import CareBackend``、``from care.rules import authorize`` 均可。
"""
from __future__ import annotations

import sys as _sys

from backend import *  # noqa: F401,F403
from backend import __version__  # noqa: F401
from backend import (  # noqa: F401
    Backend,
    CareBackend,
    CareService,
    Service,
    Store,
    create_backend,
    load_backend,
)

_SUBMODULES = ("timeutil", "errors", "rules", "store", "derive",
               "boards", "service", "server")
for _name in _SUBMODULES:
    _mod = __import__(f"backend.{_name}", fromlist=[_name])
    _sys.modules[f"care.{_name}"] = _mod
    globals()[_name] = _mod

del _name, _SUBMODULES
