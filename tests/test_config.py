import os
import stat
import threading

import pytest

from calendarapp.config import ConfigError, load_settings
from calendarapp.store import prepare_store, store_config_text, store_password


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name in ("AUTH_MODE", "BIND_HOST", "PORT", "DAV_PORT", "STORE_PORT", "CALDAV_ENABLED", "CALDAV_USER", "ARTY_URL",
                    "PUBLIC_URL", "TRUSTED_PROXIES", "TIME_ZONE", "DATA_DIR", "ARTY_APPROVE_URL"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return monkeypatch


def test_caldav_is_off_unless_switched_on(clean_env):
    assert load_settings().caldav_enabled is False
    clean_env.setenv("CALDAV_ENABLED", "true")
    assert load_settings().caldav_enabled is True


@pytest.mark.parametrize("name, value", [
    ("CALDAV_ENABLED", "maybe"), ("PORT", "8112"), ("CALDAV_USER", "Owner/../x"), ("ARTY_URL", "ftp://x"),
    ("TRUSTED_PROXIES", "0.0.0.0/0"), ("TIME_ZONE", "Mars/Olympus"), ("PUBLIC_URL", "not a url")])
def test_bad_settings_are_refused(clean_env, name, value):
    clean_env.setenv(name, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_local_mode_must_stay_on_loopback(clean_env):
    clean_env.setenv("AUTH_MODE", "local")
    clean_env.setenv("BIND_HOST", "0.0.0.0")
    with pytest.raises(ConfigError, match="BIND_HOST"):
        load_settings()


def test_a_fresh_install_names_only_this_machine(clean_env):
    settings = load_settings()
    assert settings.public_url == "http://localhost:8110"
    assert settings.arty_approve_url == ""
    clean_env.setenv("PORT", "8200")
    assert load_settings().public_url == "http://localhost:8200"


def test_linking_arty_needs_the_approve_page(clean_env):
    clean_env.setenv("ARTY_URL", "https://arty.example")
    with pytest.raises(ConfigError, match="ARTY_APPROVE_URL"):
        load_settings()
    clean_env.setenv("ARTY_APPROVE_URL", "https://sign-in.example/arty-devices.html")
    assert load_settings().arty_approve_url == "https://sign-in.example/arty-devices.html"


def test_store_files_are_private_and_radicale_is_locked_down(clean_env):
    settings = load_settings()
    config_path = prepare_store(settings)
    for name in ("store_password", "store_users", "radicale.conf"):
        assert stat.S_IMODE(os.stat(settings.data_dir / name).st_mode) == 0o600
    text = store_config_text(settings)
    assert "hosts = 127.0.0.1:" in text
    assert "type = owner_only" in text and "[web]\ntype = none" in text and "[sharing]\ntype = none" in text
    first = (settings.data_dir / "store_password").read_text()
    prepare_store(settings)
    assert (settings.data_dir / "store_password").read_text() == first
    assert config_path.exists()


def test_web_app_and_store_starting_together_agree_on_the_store_password(tmp_path):
    starters = 8
    for attempt in range(20):
        data_dir = tmp_path / f"data-{attempt}"
        barrier = threading.Barrier(starters)
        answers = []

        def start():
            barrier.wait()
            answers.append(store_password(data_dir))

        threads = [threading.Thread(target=start) for _ in range(starters)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(answers)) == 1
        assert answers[0] == (data_dir / "store_password").read_text()


def test_a_broken_store_password_file_is_replaced(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "store_password").write_text("short")
    password = store_password(data_dir)
    assert len(password) == 64
    assert (data_dir / "store_password").read_text() == password
    assert store_password(data_dir) == password


@pytest.mark.parametrize("address, accepted", [
    ("https://arty.example", True), ("http://127.0.0.1:8081", True), ("http://localhost:8081", True), ("http://[::1]:8081", True),
    ("http://10.0.0.20:8081", False), ("http://arty.example", False)])
def test_arty_is_reached_over_https_or_on_this_machine_only(clean_env, address, accepted):
    clean_env.setenv("ARTY_URL", address)
    clean_env.setenv("ARTY_APPROVE_URL", "https://sign-in.example/arty-devices.html")
    if accepted:
        assert load_settings().arty_url == address
    else:
        with pytest.raises(ConfigError, match="ARTY_URL"):
            load_settings()
