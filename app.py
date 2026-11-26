"""兼容入口：等价于 ``python3 -m backend.server``。"""
from backend.server import BackendHandler, create_server, main
from backend.service import Backend, CareBackend, CareService, Service

#: 模块级共享后端，便于 ``from app import backend`` / ``import app; app.Backend()``
backend = CareBackend()


def create_app(*args, **kwargs):
    """返回一个已绑定后端的 ``ThreadingHTTPServer``。"""
    return create_server(*args, **kwargs)


# 常见 WSGI/ASGI 命名占位：这里的服务基于 http.server，请用 create_app/main 起服务。
app = backend
application = backend

__all__ = ["main", "create_server", "create_app", "BackendHandler",
           "CareBackend", "CareService", "Backend", "Service",
           "backend", "app", "application"]


if __name__ == "__main__":
    main()
