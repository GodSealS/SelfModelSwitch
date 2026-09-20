"""P27: the compat port opens to one named network, and only to that one.

The script is exercised against a fake `iptables` so the rules it would install
are readable, and the renderer is checked to carry the same two values into the
unit instead of a template default.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from model_scheduler.deploy import DeployError, ServiceInputs, render_service_units

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "open-firewall.sh"
CIDR = "192.168.55.0/24"
PORT = "8090"


def _fake_iptables(tmp_path: Path) -> tuple[Path, Path]:
    """An `iptables` that records every call; `-C` answers from what was added."""
    log = tmp_path / "calls.log"
    state = tmp_path / "rules.txt"
    state.write_text("", encoding="utf-8")
    shim = tmp_path / "iptables"
    shim.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{log}"
        case "$1" in
          -C) grep -qxF -- "$(echo "$@" | sed 's/^-C INPUT //')" "{state}" ;;
          -A) echo "$(echo "$@" | sed 's/^-A INPUT //')" >> "{state}" ;;
          -D) grep -vxF -- "$(echo "$@" | sed 's/^-D INPUT //')" "{state}" > "{state}.tmp"; mv "{state}.tmp" "{state}" ;;
          *) exit 4 ;;
        esac
        """), encoding="utf-8")
    shim.chmod(0o755)
    return shim, log


def _run(script_args: list[str], *, shim: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/sh", str(SCRIPT), *script_args], capture_output=True, text=True, check=False,
                          env={**os.environ, "IPTABLES": str(shim),
                               "PATH": f"{shim.parent}:{os.environ['PATH']}"})


def test_the_rule_is_added_once_and_only_once(tmp_path: Path) -> None:
    shim, log = _fake_iptables(tmp_path)

    first = _run(["--cidr", CIDR, "--port", PORT], shim=shim)
    second = _run(["--cidr", CIDR, "--port", PORT], shim=shim)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0 and "already present" in second.stdout
    added = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("-A")]
    assert len(added) == 1, added  # a restart never stacks duplicates
    assert added[0] == f"-A INPUT -p tcp -s {CIDR} --dport {PORT} -j ACCEPT"


def test_two_named_networks_get_one_rule_each(tmp_path: Path) -> None:
    shim, log = _fake_iptables(tmp_path)

    result = _run(["--cidr", "192.168.55.0/24,192.168.1.0/24", "--port", PORT], shim=shim)
    again = _run(["--cidr", "192.168.55.0/24,192.168.1.0/24", "--port", PORT], shim=shim)

    assert result.returncode == 0, result.stderr
    assert "already present" in again.stdout and again.stdout.count("already present") == 2
    added = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("-A")]
    assert added == [f"-A INPUT -p tcp -s 192.168.55.0/24 --dport {PORT} -j ACCEPT",
                     f"-A INPUT -p tcp -s 192.168.1.0/24 --dport {PORT} -j ACCEPT"]


def test_remove_and_check_report_the_truth(tmp_path: Path) -> None:
    shim, _log = _fake_iptables(tmp_path)

    _run(["--cidr", CIDR, "--port", PORT], shim=shim)
    assert _run(["--cidr", CIDR, "--port", PORT, "--check"], shim=shim).returncode == 0
    removed = _run(["--cidr", CIDR, "--port", PORT, "--remove"], shim=shim)
    assert removed.returncode == 0 and "removed" in removed.stdout
    assert _run(["--cidr", CIDR, "--port", PORT, "--check"], shim=shim).returncode == 1


@pytest.mark.parametrize("arguments", [
    ["--port", PORT],                       # no network named
    ["--cidr", CIDR],                       # no port named
    ["--cidr", "192.168.55.0", "--port", PORT],
    ["--cidr", "everywhere", "--port", PORT],
    ["--cidr", CIDR, "--port", "8090a"],
    ["--cidr", CIDR, "--port", "0"],
    ["--cidr", CIDR, "--port", "70000"],
    ["--cidr", "0.0.0.0/0", "--port", PORT],  # the internet is not a deployment input
])
def test_what_it_refuses(tmp_path: Path, arguments: list[str]) -> None:
    shim, _log = _fake_iptables(tmp_path)

    result = _run(arguments, shim=shim)

    assert result.returncode == 2, (arguments, result.stdout)
    assert shim.parent.joinpath("rules.txt").read_text(encoding="utf-8") == ""  # nothing was installed


def test_the_whole_internet_needs_an_explicit_flag(tmp_path: Path) -> None:
    shim, _log = _fake_iptables(tmp_path)

    allowed = _run(["--cidr", "0.0.0.0/0", "--port", PORT, "--allow-any"], shim=shim)

    assert allowed.returncode == 0, allowed.stderr


def test_an_unusable_iptables_is_reported_not_swallowed(tmp_path: Path) -> None:
    result = _run(["--cidr", CIDR, "--port", PORT], shim=Path("/nonexistent/iptables"))

    assert result.returncode == 3


def _inputs(**overrides) -> ServiceInputs:
    values = dict(service_user="model-scheduler", service_group="model-scheduler", client_uid=1003,
                  client_group="sms-client", socket_path="/run/self-model-switch/control.sock",
                  model_mount="/media/disk", model_directory="/media/disk/models", mount_unit="media-disk.mount",
                  blob_root="/var/lib/self-model-switch/blobs", blob_disk_uuid="0f9b2c31-1111-2222-3333-444455556666",
                  blob_quota_bytes=17179869184, release_root="/opt/self-model-switch/releases",
                  config_path="/etc/self-model-switch/config.yaml",
                  swap_config_path="/etc/self-model-switch/llama-swap.yaml")
    values.update(overrides)
    return ServiceInputs(**values)


def test_the_unit_opens_only_the_network_the_deployment_names(tmp_path: Path) -> None:
    output = tmp_path / "units"
    result = render_service_units(inputs=_inputs(allow_cidr=CIDR, scheduler_port=8090), output=output)

    unit = (output / "model-scheduler.service").read_text(encoding="utf-8")
    assert f"Environment=SMS_ALLOW_CIDR={CIDR}" in unit
    assert "Environment=SMS_SCHEDULER_PORT=8090" in unit
    assert "open-firewall.sh --cidr ${SMS_ALLOW_CIDR} --port ${SMS_SCHEDULER_PORT}" in unit
    facts = json.loads((output / "service-facts.json").read_text(encoding="utf-8"))
    assert facts["allow_cidr"] == CIDR and facts["scheduler_port"] == 8090
    assert result["units"]


def test_the_renderer_refuses_the_internet_and_a_bad_network(tmp_path: Path) -> None:
    with pytest.raises(DeployError, match="allow_public"):
        _inputs(allow_cidr="0.0.0.0/0").validate()
    with pytest.raises(DeployError, match="allow_cidr"):
        _inputs(allow_cidr="everywhere").validate()
    with pytest.raises(DeployError, match="scheduler_port"):
        _inputs(scheduler_port=70000).validate()
    assert _inputs(allow_cidr="0.0.0.0/0", allow_public=True).validate() is None


def test_the_script_is_executable() -> None:
    assert SCRIPT.is_file() and os.access(SCRIPT, os.X_OK)
    assert sys.platform != "win32"  # the unit runs it on the target, never here
