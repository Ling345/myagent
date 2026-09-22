"""网页层：零新增依赖的本地可视化界面。

``runner`` 负责把一次智能体运行变成事件流，
``server`` 用标准库 http.server 把事件流以 SSE 形式推给浏览器。
"""

from agentcode.web.runner import run_stream
from agentcode.web.server import create_server, serve

__all__ = ["create_server", "run_stream", "serve"]
