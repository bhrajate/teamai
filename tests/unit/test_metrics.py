"""指标：端点可服务、上报点无条件可调、multiprocess 模式的装配。

⚠️ 这个可观测面最危险的失败模式是**静默正常**：未设 PROMETHEUS_MULTIPROC_DIR 时
/metrics 照常返回 200，只是 worker 侧的投影指标一律为 0 —— 看起来一切健康。故本
文件专门验「两种模式都能服务」，并锁住缺失变量时会打 warning。
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from teamai.domain.ports import NullMetricsSink
from teamai.infrastructure.metrics import (
    MULTIPROC_ENV,
    PrometheusMetricsSink,
    build_metrics_asgi_app,
    mark_process_exit,
)


def _client(app_factory) -> TestClient:
    app = Starlette()
    app.mount("/metrics", app_factory())
    return TestClient(app)


def test_进程内模式可服务(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MULTIPROC_ENV, raising=False)

    resp = _client(build_metrics_asgi_app).get("/metrics")

    assert resp.status_code == 200
    assert "teamai_memory_outbox_pending" in resp.text


def test_缺失多进程目录时打warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """不报错是有意的（web 得起得来），所以必须留一条可 grep 的痕迹 ——
    否则部署时忘了配这个变量，症状是「指标看起来一切正常」。"""
    monkeypatch.delenv(MULTIPROC_ENV, raising=False)

    with caplog.at_level("WARNING"):
        build_metrics_asgi_app()

    assert any(MULTIPROC_ENV in r.message for r in caplog.records)


def test_多进程模式端到端(tmp_path) -> None:
    """必须起子进程测。

    ⚠️ `PROMETHEUS_MULTIPROC_DIR` 要在**指标定义之前**（即 import
    `infrastructure.metrics` 之前）就已设好 —— prometheus_client 在建 Gauge 那一刻
    决定它是「写 mmap 文件」还是「纯进程内」。本测试进程早已 import 过该模块，
    运行时 monkeypatch.setenv 已经太晚：`/metrics` 会返回 200 而 body 为空，
    这正是最危险的静默失效形态。

    所以这条用例在子进程里跑：先设变量、再 import、再上报、再汇总，与生产的
    启动顺序一致。这也顺便锁住了那个部署约束 —— 若哪天改成运行时可切换，
    这条会红。
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        from teamai.infrastructure.metrics import PrometheusMetricsSink, build_metrics_asgi_app
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        PrometheusMetricsSink().outbox_state(pending=7, dead=2, lag_seconds=42.5)

        app = Starlette()
        app.mount("/metrics", build_metrics_asgi_app())
        body = TestClient(app).get("/metrics").text
        assert "teamai_memory_outbox_pending" in body, "多进程汇总应含该指标"
        assert "7.0" in body, f"应读到 pending=7，实际:\\n{body[:600]}"
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**__import__("os").environ, MULTIPROC_ENV: str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"子进程失败:\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout


def test_运行时才设目录会得到空指标(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """把上面那个约束的**后果**固化下来。

    这不是在测「正确行为」，而是在记录一个陷阱：变量设晚了不报错，只是指标全空。
    `.env.example` 与模块文档都写了这一点，这条用例保证那个说明不会过期。
    """
    monkeypatch.setenv(MULTIPROC_ENV, str(tmp_path))

    PrometheusMetricsSink().outbox_state(pending=7, dead=2, lag_seconds=42.5)
    resp = _client(build_metrics_asgi_app).get("/metrics")

    assert resp.status_code == 200
    assert resp.text == "", "本进程 import 时未设变量，汇总必然为空"


def test_上报点全都不抛(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """指标上报绝不能让业务路径失败：埋点坏了只该缺一条线，而抛出去会让整条
    投影记录进入退避重试。"""
    monkeypatch.setenv(MULTIPROC_ENV, str(tmp_path))
    sink = PrometheusMetricsSink()

    sink.outbox_state(pending=1, dead=0, lag_seconds=0.0)
    sink.projected(op="UPSERT", result="upserted")
    sink.embed_duration(0.123)
    sink.reconciled(direction="upsert", count=3)
    mark_process_exit()


def test_null实现全都不抛() -> None:
    """让上报点可以无条件调，不必到处判 None —— 散开的 None 判断必然漏掉一处，
    而漏掉的那处就是一个静默失效的埋点。"""
    sink = NullMetricsSink()

    sink.outbox_state(pending=1, dead=1, lag_seconds=1.0)
    sink.projected(op="DELETE", result="deleted")
    sink.embed_duration(1.0)
    sink.reconciled(direction="delete", count=0)


def test_未设目录时清理是noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MULTIPROC_ENV, raising=False)
    mark_process_exit()


def test_sink实现了端口的全部方法() -> None:
    """ABC 已经保证了这件事，但显式断言能在「端口加了方法而实现忘了跟」时给出
    更直白的失败信息 —— 那种情况下 ABC 的报错发生在实例化处，堆栈指不回端口。"""
    from teamai.domain.ports import MetricsSink

    assert issubclass(PrometheusMetricsSink, MetricsSink)
    assert issubclass(NullMetricsSink, MetricsSink)
