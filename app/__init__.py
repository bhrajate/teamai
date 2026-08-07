"""进程入口：每个子包一个进程，只做装配与启动，业务逻辑在 src/teamai。

依赖方向单向：app.* 可以 import teamai.*，反之不行（由 tests/unit/test_layering.py 校验）。
"""
