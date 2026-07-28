"""Tests for config-dir resolution, the pointer file, unknown-key
validation, blank path settings, and config write-back helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from nerve import paths
from nerve.config import (
    CronConfig,
    NerveConfig,
    ProxyConfig,
    SSLConfig,
    TelegramConfig,
    WorkflowRunsConfig,
    append_telegram_allowed_user,
    load_config,
    read_config_pointer,
    resolve_config_dir,
    validate_config_keys,
    write_config_pointer,
)


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    """A directory that looks like a real install."""
    d = tmp_path / "install"
    d.mkdir()
    (d / "config.yaml").write_text("workspace: ~/ws\n")
    (d / "config.local.yaml").write_text("anthropic_api_key: sk-ant-test\n")
    return d


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """An empty directory with no config files."""
    d = tmp_path / "elsewhere"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("NERVE_CONFIG_DIR", raising=False)


class TestResolveConfigDir:
    def test_explicit_flag_wins(self, configured, elsewhere, monkeypatch):
        monkeypatch.setenv("NERVE_CONFIG_DIR", str(configured))
        d, source = resolve_config_dir(str(elsewhere))
        assert d == elsewhere
        assert source == "flag"

    def test_env_var(self, configured, elsewhere, monkeypatch):
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("NERVE_CONFIG_DIR", str(configured))
        d, source = resolve_config_dir()
        assert d == configured
        assert source == "env"

    def test_cwd_with_config(self, configured, monkeypatch):
        monkeypatch.chdir(configured)
        d, source = resolve_config_dir()
        assert d == configured
        assert source == "cwd"

    def test_pointer_used_from_other_cwd(self, configured, elsewhere, monkeypatch):
        """The classic footgun: install configured in ~/nerve, command run from $HOME."""
        write_config_pointer(configured)
        monkeypatch.chdir(elsewhere)
        d, source = resolve_config_dir()
        assert d == configured.resolve()
        assert source == "pointer"

    def test_cwd_config_beats_pointer(self, configured, tmp_path, monkeypatch):
        """Dev workflow: a checkout with its own config wins over the pointer."""
        write_config_pointer(configured)
        dev = tmp_path / "dev-checkout"
        dev.mkdir()
        (dev / "config.yaml").write_text("workspace: ~/dev-ws\n")
        monkeypatch.chdir(dev)
        d, source = resolve_config_dir()
        assert d == dev
        assert source == "cwd"

    def test_stale_pointer_falls_back(self, tmp_path, elsewhere, monkeypatch):
        gone = tmp_path / "gone"
        gone.mkdir()
        write_config_pointer(gone)
        gone.rmdir()
        monkeypatch.chdir(elsewhere)
        d, source = resolve_config_dir()
        assert d == elsewhere
        assert source == "default"

    def test_pointer_without_config_files_ignored(self, elsewhere, tmp_path, monkeypatch):
        empty_install = tmp_path / "empty-install"
        empty_install.mkdir()
        write_config_pointer(empty_install)
        monkeypatch.chdir(elsewhere)
        d, source = resolve_config_dir()
        assert source == "default"

    def test_fresh_install_default(self, elsewhere, monkeypatch):
        monkeypatch.chdir(elsewhere)
        d, source = resolve_config_dir()
        assert d == elsewhere
        assert source == "default"


class TestConfigPointer:
    def test_round_trip(self, configured):
        write_config_pointer(configured)
        assert read_config_pointer() == configured.resolve()

    def test_missing_returns_none(self):
        assert read_config_pointer() is None

    def test_deleted_dir_returns_none(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        write_config_pointer(d)
        d.rmdir()
        assert read_config_pointer() is None


class TestLoadConfigDir:
    def test_load_config_records_dir(self, configured):
        config = load_config(configured)
        assert config.config_dir == configured

    def test_load_config_uses_resolution(self, configured, elsewhere, monkeypatch):
        """load_config(None) resolves via the waterfall, not bare CWD."""
        write_config_pointer(configured)
        monkeypatch.chdir(elsewhere)
        config = load_config()
        assert config.anthropic_api_key == "sk-ant-test"


class TestValidateConfigKeys:
    def test_clean_config_no_warnings(self):
        merged = {
            "workspace": "~/ws",
            "timezone": "UTC",
            "anthropic_api_key": "sk-ant-x",
            "telegram": {
                "enabled": True,
                "dm_policy": "pairing",
                "allowed_users": [1],
                "stream_mode": "partial",
            },
            "agent": {"model": "claude-opus-4-8"},
            "auth": {"jwt_secret": "s"},
        }
        assert validate_config_keys(merged) == []

    def test_unknown_top_level_key(self):
        warnings = validate_config_keys({"workspaec": "~/ws"})
        assert len(warnings) == 1
        assert "workspaec" in warnings[0]

    def test_unknown_nested_key_with_dotted_path(self):
        warnings = validate_config_keys({"telegram": {"dm_policyy": "pairing"}})
        assert len(warnings) == 1
        assert "telegram.dm_policyy" in warnings[0]

    def test_docker_entrypoint_keys_allowed(self):
        merged = {"claude_oauth_token": "tok", "github_token": "ghp_x"}
        assert validate_config_keys(merged) == []

    def test_opaque_subtrees_not_flagged(self):
        merged = {
            "mcp_servers": {"my-server": {"command": "x", "anything": 1}},
            "memory": {"categories": [{"name": "a", "description": "b"}]},
        }
        assert validate_config_keys(merged) == []


class TestTelegramDmPolicy:
    def test_default_is_pairing(self):
        assert TelegramConfig.from_dict({}).dm_policy == "pairing"

    def test_open_accepted(self):
        assert TelegramConfig.from_dict({"dm_policy": "open"}).dm_policy == "open"

    def test_invalid_falls_back_to_pairing(self):
        assert TelegramConfig.from_dict({"dm_policy": "weird"}).dm_policy == "pairing"

    def test_allowed_users_coerced_to_int(self):
        cfg = TelegramConfig.from_dict({"allowed_users": ["123", 456]})
        assert cfg.allowed_users == [123, 456]


class TestAppendTelegramAllowedUser:
    def test_creates_file_when_missing(self, tmp_path):
        assert append_telegram_allowed_user(tmp_path, 42) is True
        data = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
        assert data["telegram"]["allowed_users"] == [42]

    def test_preserves_existing_keys(self, tmp_path):
        (tmp_path / "config.local.yaml").write_text(
            yaml.safe_dump({
                "anthropic_api_key": "sk-ant-keep",
                "telegram": {"bot_token": "123:abc"},
            })
        )
        assert append_telegram_allowed_user(tmp_path, 42) is True
        data = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
        assert data["anthropic_api_key"] == "sk-ant-keep"
        assert data["telegram"]["bot_token"] == "123:abc"
        assert data["telegram"]["allowed_users"] == [42]

    def test_duplicate_returns_false(self, tmp_path):
        append_telegram_allowed_user(tmp_path, 42)
        assert append_telegram_allowed_user(tmp_path, 42) is False
        data = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
        assert data["telegram"]["allowed_users"] == [42]

    def test_appends_second_user(self, tmp_path):
        append_telegram_allowed_user(tmp_path, 1)
        append_telegram_allowed_user(tmp_path, 2)
        data = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
        assert data["telegram"]["allowed_users"] == [1, 2]

    def test_file_permissions(self, tmp_path):
        append_telegram_allowed_user(tmp_path, 42)
        mode = stat.S_IMODE(os.stat(tmp_path / "config.local.yaml").st_mode)
        assert mode == 0o600


class TestBlankPathSettingsMeanUnset:
    """A path setting left blank must fall back to its default, not to ".".

    ``Path("")`` is ``Path(".")`` and truthy, so a key written as ``runs_dir:``
    or ``cert: ""`` would otherwise sail straight past the ``or <default>``
    fallback and resolve to the working directory the daemon was started in —
    silently, since that directory usually exists and is writable. One test per
    key, because each one goes wrong in its own way: writing state where nobody
    will look for it, importing whatever ``.py`` files happen to be lying
    around, or serving TLS with no certificate.
    """

    def test_gateway_ssl_blank_cert_and_key_disable_tls(self):
        ssl = SSLConfig.from_dict({"cert": "", "key": ""})
        assert (ssl.cert, ssl.key) == (None, None)
        assert ssl.enabled is False

    def test_gateway_ssl_blank_cert_alone_disables_tls(self):
        """``enabled`` means "both files are set" — half-configured is off."""
        ssl = SSLConfig.from_dict({"cert": "", "key": "/etc/ssl/nerve.key"})
        assert ssl.enabled is False

    def test_workflows_runs_dir(self):
        """Run journals belong under the state dir, not next to the daemon."""
        cfg = WorkflowRunsConfig.from_dict({"runs_dir": ""})
        assert cfg.runs_dir == paths.nerve_path("workflow-runs")

    def test_proxy_binary_path(self):
        cfg = ProxyConfig.from_dict({"binary_path": ""})
        assert cfg.binary_path == paths.nerve_path("bin", "cli-proxy-api")

    def test_proxy_auth_dir(self):
        """Proxy OAuth material must not be dropped into the cwd."""
        cfg = ProxyConfig.from_dict({"auth_dir": ""})
        assert cfg.auth_dir == paths.nerve_path("cli-proxy-auth")

    def test_proxy_log_file(self):
        cfg = ProxyConfig.from_dict({"log_file": ""})
        assert cfg.log_file == paths.nerve_path("proxy.log")

    def test_cron_gate_plugins_dir(self):
        """This one executes what it finds — every ``*.py`` in the directory."""
        cfg = CronConfig.from_dict({"gate_plugins_dir": ""})
        assert cfg.gate_plugins_dir == paths.cron_dir() / "gates"

    def test_cron_jobs_file(self):
        cfg = CronConfig.from_dict({"jobs_file": ""})
        assert cfg.jobs_file == paths.cron_dir() / "jobs.yaml"

    def test_cron_system_file(self):
        cfg = CronConfig.from_dict({"system_file": ""})
        assert cfg.system_file == paths.cron_dir() / "system.yaml"

    def test_workspace(self):
        cfg = NerveConfig.from_dict({"workspace": ""})
        assert cfg.workspace == paths.default_workspace()

    @pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t "])
    def test_whitespace_only_counts_as_blank(self, blank):
        """A stray space after the colon is the likeliest way to write this."""
        cfg = CronConfig.from_dict({"gate_plugins_dir": blank})
        assert cfg.gate_plugins_dir == paths.cron_dir() / "gates"

    def test_an_env_var_that_expands_to_nothing_is_blank_too(self, monkeypatch):
        """``NERVE_TEST_RUNS_DIR=`` in a unit file or compose env block."""
        monkeypatch.setenv("NERVE_TEST_RUNS_DIR", "")
        cfg = WorkflowRunsConfig.from_dict({"runs_dir": "$NERVE_TEST_RUNS_DIR"})
        assert cfg.runs_dir == paths.nerve_path("workflow-runs")

    def test_a_configured_path_is_still_honored(self):
        cfg = CronConfig.from_dict({"gate_plugins_dir": "/opt/nerve/gates"})
        assert cfg.gate_plugins_dir == Path("/opt/nerve/gates")

    def test_tilde_still_expands(self):
        cfg = WorkflowRunsConfig.from_dict({"runs_dir": "~/runs"})
        assert cfg.runs_dir == Path.home() / "runs"

    def test_surrounding_whitespace_is_trimmed_not_baked_in(self):
        cfg = WorkflowRunsConfig.from_dict({"runs_dir": "  ~/runs  "})
        assert cfg.runs_dir == Path.home() / "runs"

    def test_a_set_env_var_still_expands(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NERVE_TEST_RUNS_DIR", str(tmp_path / "runs"))
        cfg = WorkflowRunsConfig.from_dict({"runs_dir": "$NERVE_TEST_RUNS_DIR"})
        assert cfg.runs_dir == tmp_path / "runs"
