"""config/config.yaml 加载：嵌套展平、优先级、缺失容错。

配置分两处（凭据走 .env、可调项走 config/config.yaml），yaml 里嵌套分组而代码里仍是
平铺的 settings.xxx，靠加载时展平衔接。这套衔接是隐式的，出错时表现为「改了
yaml 没生效」而非报错，故把三条承诺固化住：展平映射、优先级、缺文件不炸。
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest
import yaml

from teamai.config import Settings, _flatten


class TestFlatten:
    def test_单层不变(self) -> None:
        assert _flatten({"a": 1}) == {"a": 1}

    def test_嵌套按下划线拼接(self) -> None:
        assert _flatten({"model": {"full": "x"}}) == {"model_full": "x"}

    def test_多层嵌套(self) -> None:
        assert _flatten({"a": {"b": {"c": 1}}}) == {"a_b_c": 1}

    def test_多键并存(self) -> None:
        out = _flatten({"model": {"full": "x", "light_primary": "y"}, "budget": {"period": "DAILY"}})
        assert out == {"model_full": "x", "model_light_primary": "y", "budget_period": "DAILY"}

    def test_空字典(self) -> None:
        assert _flatten({}) == {}

    def test_保留非字符串值的原类型(self) -> None:
        """类型转换交给 pydantic，展平只搬运。"""
        out = _flatten({"admin_api": {"port": 9000}, "context": {"max_messages": None}})
        assert out == {"admin_api_port": 9000, "context_max_messages": None}


def _write_yaml(path: pathlib.Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


@pytest.fixture
def in_tmp_cwd(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """切到临时目录：yaml_file 与 env_file 都是相对 CWD 解析的。

    顺带隔离掉仓库里真实的 config/config.yaml 与 .env，避免本机配置影响断言。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    return tmp_path


class TestYamlLoading:
    def test_读取嵌套配置(self, in_tmp_cwd: pathlib.Path) -> None:
        _write_yaml(
            in_tmp_cwd / "config" / "config.yaml",
            """
            model:
              full: yaml-opus
            admin_api:
              port: 9999
            """,
        )
        s = Settings()
        assert s.model_full == "yaml-opus"
        assert s.admin_api_port == 9999

    def test_字符串按字段类型转换(self, in_tmp_cwd: pathlib.Path) -> None:
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", 'admin_api:\n  port: "8080"\n')
        assert Settings().admin_api_port == 8080

    def test_未提及的项回落默认值(self, in_tmp_cwd: pathlib.Path) -> None:
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "model:\n  full: only-this\n")
        s = Settings()
        assert s.model_full == "only-this"
        assert s.context_max_messages == 60, "未在 yaml 里出现的项应保持默认"

    def test_文件不存在不报错(self, in_tmp_cwd: pathlib.Path) -> None:
        assert not (in_tmp_cwd / "config" / "config.yaml").exists()
        assert Settings().model_full == "claude-opus-4-8"

    def test_空文件不报错(self, in_tmp_cwd: pathlib.Path) -> None:
        (in_tmp_cwd / "config" / "config.yaml").write_text("", encoding="utf-8")
        assert Settings().model_full == "claude-opus-4-8"

    def test_未知键被忽略(self, in_tmp_cwd: pathlib.Path) -> None:
        """extra="ignore"：写错分组名不该让进程起不来。"""
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "nonexistent:\n  whatever: 1\nmodel:\n  full: still-works\n")
        assert Settings().model_full == "still-works"

    def test_非法枚举值仍会报错(self, in_tmp_cwd: pathlib.Path) -> None:
        """忽略未知键不等于放过类型错误。"""
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "budget:\n  period: YEARLY\n")
        with pytest.raises(Exception, match="budget_period"):
            Settings()


class TestPrecedence:
    def test_环境变量盖过_yaml(self, in_tmp_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "model:\n  full: from-yaml\n")
        monkeypatch.setenv("MODEL_FULL", "from-env")
        assert Settings().model_full == "from-env"

    def test_dotenv_盖过_yaml(self, in_tmp_cwd: pathlib.Path) -> None:
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "model:\n  full: from-yaml\n")
        (in_tmp_cwd / ".env").write_text("MODEL_FULL=from-dotenv\n", encoding="utf-8")
        assert Settings().model_full == "from-dotenv"

    def test_yaml_盖过默认值(self, in_tmp_cwd: pathlib.Path) -> None:
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "model:\n  full: from-yaml\n")
        assert Settings().model_full == "from-yaml"

    def test_init_参数最高(self, in_tmp_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_yaml(in_tmp_cwd / "config" / "config.yaml", "model:\n  full: from-yaml\n")
        monkeypatch.setenv("MODEL_FULL", "from-env")
        assert Settings(model_full="from-init").model_full == "from-init"


class TestExampleFile:
    """config/config.example.yaml 是给人看的文档，它得跟 Settings 对得上。"""

    @staticmethod
    def _example() -> dict:
        root = pathlib.Path(__file__).resolve().parents[2]
        return yaml.safe_load((root / "config" / "config.example.yaml").read_text(encoding="utf-8")) or {}

    def test_示例文件存在且可解析(self) -> None:
        assert self._example(), "config/config.example.yaml 应存在且非空"

    def test_全部键都对应真实字段(self) -> None:
        """展平后的名字若与字段名对不上，写了也不生效 —— 静默失效最难查。"""
        flat = _flatten(self._example())
        unknown = set(flat) - set(Settings.model_fields)
        assert not unknown, f"示例里这些键展平后没有对应字段: {sorted(unknown)}"

    def test_示例值与默认值一致(self, in_tmp_cwd: pathlib.Path) -> None:
        """示例文件里写的应当就是当前默认值，否则它在误导读者。"""
        flat = _flatten(self._example())
        defaults = Settings()  # 临时 CWD 下无 config/config.yaml 与 .env，取到的是纯默认值
        mismatched = {
            key: (value, getattr(defaults, key))
            for key, value in flat.items()
            if getattr(defaults, key) != value
        }
        assert not mismatched, f"示例值与默认值不一致（键: (示例, 默认)）: {mismatched}"

    def test_不含凭据字段(self) -> None:
        """凭据只该出现在 .env —— 入库文件里放 token 字段名是泄露的开端。"""
        flat = _flatten(self._example())
        secret_like = [k for k in flat if any(w in k for w in ("token", "secret", "key", "password", "url"))]
        assert not secret_like, f"示例里出现了疑似凭据/连接串的键: {secret_like}"
