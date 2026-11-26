"""根级兼容入口：``from service import CareBackend``。"""
from backend.service import *  # noqa: F401,F403
from backend.service import (
    Backend,
    CareBackend,
    CareService,
    Service,
    create_backend,
    load_backend,
)
