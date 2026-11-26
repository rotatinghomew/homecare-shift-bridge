"""兼容别名：交换领域入口，等价于 :mod:`backend`。"""
from backend import *  # noqa: F401,F403
from backend import (  # noqa: F401
    Backend,
    CareBackend,
    CareService,
    Service,
    Store,
    create_backend,
    load_backend,
)
from backend.rules import *  # noqa: F401,F403
