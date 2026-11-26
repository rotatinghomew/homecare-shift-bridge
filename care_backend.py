"""根级兼容模块：``from care_backend import CareBackend``。"""
from backend import *  # noqa: F401,F403
from backend import (  # noqa: F401
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
