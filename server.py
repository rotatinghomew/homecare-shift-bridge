"""根级兼容入口：``python3 server.py`` 启动 JSON API + 控制台。"""
from backend.server import *  # noqa: F401,F403
from backend.server import main, create_server, BackendHandler

if __name__ == "__main__":
    main()
