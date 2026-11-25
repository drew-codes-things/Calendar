import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATH_CONSTANTS = re.compile(r"^(APP|DATA|CONF|UNIT|STORE_UNIT|SIGNIN|CADDYFILE|STATE)=/", re.MULTILINE)
CT_SIDE = re.compile(r"^  cat <<'CT_SIDE'\n(.*?)^CT_SIDE$", re.MULTILINE | re.DOTALL)
DISPATCH = re.compile(r"^case \"\$\{1:-\}\" in\n.*?^esac\n", re.MULTILINE | re.DOTALL)
STARFIRE = "http://starfire.example {\n\treverse_proxy 127.0.0.1:8090\n}\n"
TUNNEL_PEER = "10.0.0.10"
SITE_VALUES = {"SITE_HOST": "calendar.example.com", "SIGNED_IN_USER": "admin", "TUNNEL_PEER": TUNNEL_PEER, "CALDAV_USER": "owner"}
LAPTOP_SITE = {"PROXMOX": "proxmox-host", "CT": "4242", **SITE_VALUES}
LAPTOP_STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
STUBS = {
    "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\n',
    "caddy": '#!/bin/sh\necho "caddy $*" >> "$STUB_LOG"\nexit 0\n',
}


class Server:
    def __init__(self, root: Path):
        self.root = root
        self.stubs = root / "stubs"
        self.stubs.mkdir(parents=True)
        for name, body in STUBS.items():
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.log = root / "stub.log"
        self.log.touch()
        self.caddyfile = root / "etc/caddy/Caddyfile"
        self.caddyfile.parent.mkdir(parents=True)
        self.caddyfile.write_text(STARFIRE)
        (root / "root").mkdir()
        ct_side = CT_SIDE.search((REPO / "deploy.sh").read_text()).group(1)
        self.script = root / "calendar-ct.sh"
        self.script.write_text(PATH_CONSTANTS.sub(lambda match: f"{match.group(1)}={root}/", ct_side))
        deploy = root / "opt/calendar/deploy"
        deploy.mkdir(parents=True)
        (deploy / "Caddyfile.calendar").write_text((REPO / "deploy/Caddyfile.calendar").read_text())
        self.write_env((REPO / "deploy/calendar.env").read_text())

    def write_env(self, text: str) -> None:
        conf = self.root / "etc/calendar"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "calendar.env").write_text(text)

    def set_ports(self, **ports: int) -> None:
        text = (self.root / "etc/calendar/calendar.env").read_text()
        for name, value in ports.items():
            text = re.sub(rf"^{name}=.*$", f"{name}={value}", text, flags=re.MULTILINE)
        self.write_env(text)

    def _environment(self) -> dict:
        return {**os.environ, **SITE_VALUES, "PATH": f"{self.stubs}:{os.environ['PATH']}", "STUB_LOG": str(self.log)}

    def run(self, shell: str) -> subprocess.CompletedProcess:
        functions = self.root / "calendar-functions.sh"
        functions.write_text(DISPATCH.sub("", self.script.read_text()))
        return subprocess.run(["bash", "-c", f"source {functions}\n{shell}"], capture_output=True, text=True,
                              env=self._environment(), cwd=self.root, timeout=60)

    def apply_caddy(self) -> subprocess.CompletedProcess:
        return self.run("load_port\nadd_caddy_site 20261113-000001")

    def undo_first_install(self) -> subprocess.CompletedProcess:
        (self.root / "root/calendar-code-before").write_text("none\n")
        for unit in ("calendar.service", "calendar-store.service"):
            path = self.root / "etc/systemd/system" / unit
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Unit]\n")
        return subprocess.run(["bash", str(self.script), "undo"], capture_output=True, text=True,
                              env=self._environment(), cwd=self.root, timeout=60)

    def own_block(self) -> str:
        text = self.caddyfile.read_text()
        return text[text.index("# >>> calendar block"):text.index("# <<< calendar block")]


@pytest.fixture
def ct(tmp_path):
    return Server(tmp_path)


def test_the_block_fills_in_the_web_port_the_gate_port_and_the_tunnel(ct):
    ct.set_ports(PORT=9110, DAV_PORT=9111)
    result = ct.apply_caddy()
    assert result.returncode == 0, result.stdout + result.stderr
    block = ct.own_block()
    assert "reverse_proxy 127.0.0.1:9110 {" in block
    assert "\t\treverse_proxy 127.0.0.1:9111\n" in block
    assert f"@not_from_tunnel not remote_ip {TUNNEL_PEER}\n" in block
    assert "__" not in block
    checks = ct.run("load_port\nown_block_keeps_caldav_to_the_tunnel && own_block_replaces_the_user_header && echo both")
    assert checks.stdout.strip() == "both", checks.stdout + checks.stderr


def test_caldav_paths_skip_the_sign_in_redirect_but_nothing_else_does(ct):
    ct.apply_caddy()
    block = ct.own_block()
    signed_out = block[block.index("@signed_out {"):block.index("redir @signed_out")]
    assert "not path /signin/* /api/auth/login /dav /dav/* /.well-known/caldav /.well-known/caldav/*" in signed_out
    assert block.index("handle @caldav {") < block.index("handle {")


def test_the_tunnel_check_fails_when_the_block_was_edited(ct):
    ct.apply_caddy()
    ct.caddyfile.write_text(ct.caddyfile.read_text().replace(f"remote_ip {TUNNEL_PEER}", "remote_ip 0.0.0.0/0"))
    checks = ct.run("load_port\nown_block_keeps_caldav_to_the_tunnel && echo kept || echo open")
    assert checks.stdout.strip() == "open"


def test_a_new_gate_port_rewrites_the_block(ct):
    ct.apply_caddy()
    ct.set_ports(DAV_PORT=9311)
    result = ct.apply_caddy()
    assert "replaced" in result.stdout, result.stdout + result.stderr
    assert "\t\treverse_proxy 127.0.0.1:9311\n" in ct.own_block()


def test_apply_then_undo_gives_back_the_same_caddyfile_and_removes_both_units(ct):
    before = ct.caddyfile.read_text()
    assert ct.apply_caddy().returncode == 0
    result = ct.undo_first_install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert ct.caddyfile.read_text() == before
    assert "systemctl disable --now calendar calendar-store" in ct.log.read_text()
    assert not (ct.root / "etc/systemd/system/calendar.service").exists()
    assert not (ct.root / "etc/systemd/system/calendar-store.service").exists()
    assert list(ct.root.glob("opt/calendar-undone-*"))


@pytest.mark.parametrize("missing", ["DAV_PORT", "STORE_PORT"])
def test_an_env_file_without_every_port_is_refused(ct, missing):
    text = (REPO / "deploy/calendar.env").read_text()
    ct.write_env(re.sub(rf"^{missing}=.*\n", "", text, flags=re.MULTILINE))
    result = ct.run("load_port")
    assert result.returncode != 0
    assert "needs PORT=, DAV_PORT= and STORE_PORT=" in result.stdout


def test_the_shipped_env_file_keeps_caldav_off():
    text = (REPO / "deploy/calendar.env").read_text()
    assert re.search(r"^CALDAV_ENABLED=false$", text, flags=re.MULTILINE)
    assert re.search(r"^CALDAV_REQUIRE_CLOUDFLARE_HTTPS=true$", text, flags=re.MULTILINE)
    assert re.search(r"^ALLOWED_USERS=__SIGNED_IN_USER__$", text, flags=re.MULTILINE)


@pytest.mark.parametrize("caddyfile", [
    STARFIRE + "\nhttp://keep.example {\n\treverse_proxy 127.0.0.1:8095\n}\n",
    STARFIRE + "\n",
    STARFIRE + "\n\n",
    STARFIRE.rstrip("\n"),
])
def test_undo_gives_back_the_caddyfile_byte_for_byte(ct, caddyfile):
    ct.caddyfile.write_bytes(caddyfile.encode())
    assert ct.apply_caddy().returncode == 0
    result = ct.undo_first_install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert ct.caddyfile.read_bytes() == caddyfile.encode()


def test_a_refused_apply_leaves_no_pushed_bundles_behind(ct):
    bundles = [ct.root / "root/calendar-code.tgz", ct.root / "root/calendar-signin.tgz"]
    for bundle in bundles:
        bundle.write_bytes(b"bundle")
    result = subprocess.run(["bash", str(ct.script), "apply", *map(str, bundles)], capture_output=True, text=True,
                            env=ct._environment(), cwd=ct.root, timeout=60)
    assert result.returncode != 0 and "run backup first" in result.stdout
    assert [bundle.name for bundle in bundles if bundle.exists()] == []


def test_the_block_and_the_first_env_file_get_the_site_values(ct):
    assert ct.apply_caddy().returncode == 0
    block = ct.own_block()
    assert "http://calendar.example.com {\n" in block
    assert "header_up X-Forwarded-User admin\n" in block
    env = ct.run(f"fill_in_site_values {REPO / 'deploy/calendar.env'}").stdout
    assert re.search(r"^ALLOWED_USERS=admin$", env, flags=re.MULTILINE)
    assert re.search(r"^PUBLIC_URL=https://calendar.example.com$", env, flags=re.MULTILINE)
    assert re.search(r"^CALDAV_USER=owner$", env, flags=re.MULTILINE)
    assert "__" not in env


def run_laptop_side(tmp_path, site_text):
    stubs = tmp_path / "laptop-stubs"
    stubs.mkdir()
    for name in ("ssh", "scp"):
        (stubs / name).write_text(LAPTOP_STUB)
        (stubs / name).chmod(0o755)
    log = tmp_path / "laptop.log"
    log.touch()
    site = tmp_path / "site.env"
    if site_text is not None:
        site.write_text(site_text)
    environment = {name: value for name, value in os.environ.items() if name not in LAPTOP_SITE}
    environment.update(PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log), SITE_ENV=str(site))
    result = subprocess.run(["bash", str(REPO / "deploy.sh"), "check"], capture_output=True, text=True,
                            env=environment, timeout=60)
    return result, log.read_text()


def test_the_laptop_side_refuses_without_the_site_file(tmp_path):
    result, calls = run_laptop_side(tmp_path, None)
    assert result.returncode == 1
    assert "deploy/site.env.example" in result.stdout
    assert calls == ""


def test_the_laptop_side_passes_the_site_values_to_the_container(tmp_path):
    site_text = "".join(f"{name}={value}\n" for name, value in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("ssh -o BatchMode=yes proxmox-host pct exec 4242 -- env SITE_HOST=calendar.example.com "
            f"SIGNED_IN_USER=admin TUNNEL_PEER={TUNNEL_PEER} CALDAV_USER=owner bash /root/calendar-deploy-ct.sh check") in calls


@pytest.mark.parametrize("name, value", [("SITE_HOST", "calendar.example.com;reboot"), ("CT", ""),
                                         ("SIGNED_IN_USER", "admin user")])
def test_the_laptop_side_refuses_a_site_value_that_is_not_plain(tmp_path, name, value):
    site_text = "".join(f"{key}={value if key == name else default}\n" for key, default in LAPTOP_SITE.items())
    result, calls = run_laptop_side(tmp_path, site_text)
    assert result.returncode == 1
    assert f"{name} in" in result.stdout
    assert calls == ""


def test_the_caddy_refusal_names_starfire_calendar():
    block = (REPO / "deploy/Caddyfile.calendar").read_text()
    assert 'respond "Starfire Calendar is only for the admin account, so this account cannot open it. Sign in with the admin account instead, or ask the admin for what you need." 403' in block


def test_the_sign_in_page_is_built_as_starfire_calendar():
    assert "--first Starfire --second Calendar --tagline" in (REPO / "deploy.sh").read_text()


def test_the_caddy_refusal_says_who_the_app_is_for_and_what_to_do_instead():
    refusal = re.search(r'respond "([^"]+)" 403', (REPO / "deploy/Caddyfile.calendar").read_text()).group(1)
    assert refusal.startswith("Starfire Calendar is only for the admin account")
    assert "Sign in with the admin account instead" in refusal
    assert "ask the admin" in refusal
    assert len(refusal) < 200
