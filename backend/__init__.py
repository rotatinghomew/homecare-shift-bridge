"""分段转介与到访交换 —— 跨站居家照护交接后端（Python 3.11 标准库）。

快速开始
--------
>>> from backend import CareBackend
>>> b = CareBackend()
>>> family = b.family_board("D7")
>>> duty = b.duty_board()

HTTP 控制台/JSON API：``python3 -m backend.server --port 8000``
"""
from . import rules, timeutil
from .errors import AuthorizationDenied, CareError, ConflictError, NotFoundError, ValidationFailed
from .service import (
    Backend,
    CareBackend,
    CareService,
    Exchange,
    ExchangeBackend,
    ExchangeService,
    Service,
    create_backend,
    load_backend,
)
from .store import DATA_FILE, Store

__all__ = [
    "CareBackend", "CareService", "Backend", "Service",
    "Exchange", "ExchangeBackend", "ExchangeService",
    "Store",
    "create_backend", "load_backend", "DATA_FILE",
    "rules", "timeutil",
    "CareError", "AuthorizationDenied", "ValidationFailed",
    "NotFoundError", "ConflictError",
]

__version__ = "1.0.0"
