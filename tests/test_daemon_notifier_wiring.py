"""Daemon auto-constructs the HA notifier from the home_assistant: block.

Production (`python -m wifi_shepard`) never passes the `ha` kwarg, so this
wiring is what makes PLAN.md §1/§4 notifications actually fire in a deployed
container. An injected kwarg (tests, alternate channels) must still win.
"""

from __future__ import annotations

from tests.conftest import FakeController, FakeHANotifier

_CONFIG_WITH_HA = """
controllers: []
home_assistant:
  url: http://example.invalid:8123
  token: dummy-token
  notify_service: dummy
scanner:
  dry_run: true
"""

_CONFIG_WITHOUT_HA = """
controllers: []
scanner:
  dry_run: true
"""


def _daemon(tmp_path, temp_db_path, config: str, **kwargs):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(config)
    return build_daemon(
        config_path=cfg_path,
        db_path=temp_db_path,
        controllers=[FakeController()],
        **kwargs,
    )


def test_daemon_builds_ha_notifier_from_config_when_kwarg_omitted(temp_db_path, tmp_path):
    from wifi_shepard.notify import HomeAssistantNotifier

    daemon = _daemon(tmp_path, temp_db_path, _CONFIG_WITH_HA)
    assert isinstance(daemon.ha, HomeAssistantNotifier)


def test_daemon_ha_is_none_without_home_assistant_block(temp_db_path, tmp_path):
    daemon = _daemon(tmp_path, temp_db_path, _CONFIG_WITHOUT_HA)
    assert daemon.ha is None


def test_injected_notifier_wins_over_config_block(temp_db_path, tmp_path):
    fake = FakeHANotifier()
    daemon = _daemon(tmp_path, temp_db_path, _CONFIG_WITH_HA, ha=fake)
    assert daemon.ha is fake
