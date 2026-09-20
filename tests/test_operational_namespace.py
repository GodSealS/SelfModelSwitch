"""P30 O02: the storage faults really stay inside a private mount namespace.

The port talks to `o02_probe` over a line protocol, so these tests pin two
things down: the port turns what the probe answers into a case verdict (a probe
that cannot isolate is `not_run`, a refused step is a failure, never a pass),
and the probe itself reports every refusal as a JSON line instead of dying.
"""
from __future__ import annotations

import io
import json

import pytest

from model_scheduler.acceptance import o02_probe
from model_scheduler.acceptance import operational_cases as oc
from model_scheduler.acceptance import operational_ports as op
from model_scheduler.acceptance.operational_ports import NamespaceDiskFaultPort, PortError

HEALTHY = {
    "isolate": {"available": True, "private_mount_namespace": True, "unmounted_shared_disk": False,
                "baseline_ready": True, "baseline_files": ["qwen-small"],
                "workspace": "/tmp/sms-o02-x", "workspace_filesystem": "tmpfs"},
    "fail_model_disk": {"model_disk_unavailable": True, "reason": "the assets stopped verifying",
                        "root_disk_writes": 0},
    "fill_scratch": {"dedicated_quota_fs": True, "scratch_full": True, "root_disk_writes": 0},
    "recover": {"recovered": True, "rehashed": True, "root_disk_writes": 0},
}


class FakeProbe:
    """Stands in for the namespace process: one answer per action, all recorded."""

    def __init__(self, answers: dict[str, dict] | None = None) -> None:
        self.answers = {**HEALTHY, **(answers or {})}
        self.sent: list[dict] = []
        self.closed = False

    def send(self, command: dict) -> dict:
        self.sent.append(dict(command))
        action = str(command.get("action") or "")
        answer = self.answers.get(action)
        if answer is None:
            return {"error": f"the namespace refused {action!r}"}
        return dict(answer)

    def close(self) -> None:
        self.closed = True
        self.sent.append({"action": "teardown"})
        self.sent.append({"action": "quit"})


def _port(answers: dict[str, dict] | None = None) -> tuple[NamespaceDiskFaultPort, FakeProbe]:
    probe = FakeProbe(answers)
    return NamespaceDiskFaultPort(config_path="scheduler.yaml", filesystem="ext4",
                                  probe_factory=lambda: probe), probe


def test_o02_passes_through_a_healthy_namespace_probe() -> None:
    port, probe = _port()
    result = oc.run_o02(port, deployment_id="sms-lab", quota_bytes=64 * 1024**2)

    assert result.status == "passed"
    assert [command["action"] for command in probe.sent] == [
        "isolate", "fail_model_disk", "fill_scratch", "recover"]
    assert probe.sent[2]["quota_bytes"] == 64 * 1024**2
    assert result.facts["isolation"]["baseline_ready"] is True  # the fault is attributable
    assert oc.recompute_objections("O02", result.facts) == []


def test_a_namespace_that_cannot_isolate_is_not_run() -> None:
    port, _ = _port({"isolate": {"error": "unshare: operation not permitted"}})
    result = oc.run_o02(port, deployment_id="sms-lab", quota_bytes=1024)

    assert result.status == "not_run"
    assert "unshare" in result.facts["isolation"]["reason"]
    assert result.facts["isolation"]["available"] is False


def test_a_disk_that_was_never_verified_cannot_claim_a_fault() -> None:
    port, _ = _port({"isolate": {"available": False, "baseline_ready": False, "baseline_files": [],
                                 "reason": "the model disk does not verify inside this namespace: "
                                           "mount_identity_mismatch"}})
    result = oc.run_o02(port, deployment_id="sms-lab", quota_bytes=1024)

    assert result.status == "not_run"
    assert "mount_identity_mismatch" in result.facts["isolation"]["reason"]


def test_a_refused_step_is_a_failure_and_never_a_pass() -> None:
    port, _ = _port({"fail_model_disk": {"error": "mount --bind is not permitted in this namespace"}})
    result = oc.run_o02(port, deployment_id="sms-lab", quota_bytes=1024)

    assert result.status == "failed"
    assert any("fail_model_disk" in failure for failure in result.failures)
    assert result.facts["model_disk"] == {}


def test_a_probe_that_reports_a_machine_wide_unmount_fails() -> None:
    port, _ = _port({"isolate": {"available": True, "private_mount_namespace": True,
                                 "unmounted_shared_disk": True}})
    result = oc.run_o02(port, deployment_id="sms-lab", quota_bytes=1024)

    assert result.status == "failed"
    assert any("machine-wide disk" in problem for problem in result.problems)
    assert any("machine-wide disk" in problem for problem in oc.recompute_objections("O02", result.facts))


def test_a_probe_that_writes_outside_the_quota_fails() -> None:
    port, _ = _port({"fill_scratch": {"dedicated_quota_fs": True, "scratch_full": True,
                                      "root_disk_writes": 2}})
    result = oc.run_o02(port, deployment_id="sms-lab", quota_bytes=1024)

    assert result.status == "failed"
    assert any("root_disk_writes" in problem for problem in result.problems)


def test_the_port_closes_the_namespace_exactly_once() -> None:
    port, probe = _port()
    oc.run_o02(port, deployment_id="sms-lab", quota_bytes=1024)

    port.close()
    assert probe.closed is True
    assert [command["action"] for command in probe.sent[-2:]] == ["teardown", "quit"]

    port.close()  # a second close must not open another namespace
    assert probe.sent.count({"action": "teardown"}) == 1


def test_a_port_that_never_ran_opens_no_namespace() -> None:
    calls: list[int] = []

    def factory() -> FakeProbe:
        calls.append(1)
        return FakeProbe()

    NamespaceDiskFaultPort(config_path="scheduler.yaml", filesystem="ext4", probe_factory=factory).close()

    assert calls == []


def test_a_non_object_answer_is_refused() -> None:
    class NotAnObject:
        def send(self, command: dict):
            return ["a list is not a probe answer"]

        def close(self) -> None:
            return None

    port = NamespaceDiskFaultPort(config_path="scheduler.yaml", filesystem="ext4",
                                  probe_factory=NotAnObject)

    with pytest.raises(PortError):
        port.fail_model_disk()


def test_o03_reads_what_the_machine_and_the_service_really_say() -> None:
    """The fault, the health and the untouched neighbours are all measured, never assumed."""
    calls: list[list[str]] = []
    running = ["sms-sms-orin-lab-qwen-small", "postgres"]
    down: list[bool] = []

    def runner(argv, *, timeout=None):
        calls.append(list(argv))
        if argv[:3] == ["sudo", "systemctl", "stop"]:
            down.append(True)
            return 0, ""
        if argv[:3] == ["sudo", "systemctl", "start"]:
            down.clear()
            return 0, ""
        if argv[:2] == ["docker", "ps"]:
            return (1, "cannot connect to the docker daemon") if down else (0, "\n".join(running))
        if argv[:2] == ["docker", "run"]:
            running.append(argv[argv.index("--name") + 1])
            return 0, "container-id"
        if argv[:2] == ["docker", "rm"]:
            name = argv[-1]
            if name in running:
                running.remove(name)
            return 0, name
        return 1, "unexpected"

    ticks = {"now": 0.0}

    def clock() -> float:
        ticks["now"] += 1000.0  # every poll of the stalled stop jumps past its deadline
        return ticks["now"]

    port = op.DockerFaultPort(deployment_id="sms-orin-lab", base_url="http://127.0.0.1:8090",
                              model_id="qwen-small", image="sms-llama-cpp@sha256:" + "8" * 64,
                              runner=runner, post=lambda *a, **k: _Posted(200),
                              clock=clock, wait=lambda seconds: None)

    def statuses(url, **kwargs):
        return _Posted(503 if url.endswith("/health") else 200,
                       {"models": {"qwen-small": {"state": "ready", "last_error": None}},
                        "readiness_reason": None})

    import model_scheduler.acceptance.operational_ports as ports_module

    original = ports_module.httpx
    try:
        ports_module.httpx = type("httpx", (), {"get": staticmethod(statuses), "post": staticmethod(statuses)})
        unreachable = port.make_docker_unreachable()
        stranger = port.present_unknown_instance()
        stalled = port.stall_stop("qwen-small")
    finally:
        ports_module.httpx = original

    assert unreachable["docker_unreachable"] is True and unreachable["docker_restored"] is True
    assert unreachable["health_status"] == 503 and unreachable["budget_kept"] is True
    assert unreachable["other_containers_untouched"] is True
    assert unreachable["foreign_containers"] == ["postgres"]  # a neighbour that must survive
    assert stranger["unknown_recorded"] is True and stranger["stranger_running"] is True
    assert stranger["foreign_containers"] == ["postgres"]
    assert stalled["stop_timeout_recorded"] is False  # it never proved: the service said "ready"
    assert stalled["budget_kept"] is True
    assert any(argv[:2] == ["docker", "rm"] for argv in calls)  # the strangers are cleaned up


class _Posted:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def test_o03_refuses_a_fault_it_cannot_stage() -> None:
    port = op.DockerFaultPort(deployment_id="sms-orin-lab", base_url="http://127.0.0.1:8090",
                              model_id="qwen-small", image="sms-llama-cpp@sha256:" + "8" * 64,
                              runner=lambda argv, *, timeout=None: (1, "permission denied"))

    with pytest.raises(op.PortError, match="container listing is not readable"):
        port.make_docker_unreachable()


def test_the_probe_reports_every_refusal_as_a_line(monkeypatch: pytest.MonkeyPatch) -> None:
    class LoopProbe:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def isolate(self) -> dict:
            return {"private_mount_namespace": True, "baseline_ready": True}

        def fill_scratch(self, quota_bytes: int) -> dict:
            return {"bytes_written": quota_bytes}

        def fail_model_disk(self) -> dict:
            raise RuntimeError("mount --bind is not permitted")

    monkeypatch.setattr(o02_probe, "Probe", LoopProbe)
    stdin = io.StringIO("\n".join([
        "not json at all",
        json.dumps({"action": "nonsense"}),
        json.dumps({"action": "isolate"}),
        json.dumps({"action": "fail_model_disk"}),
        json.dumps({"action": "fill_scratch", "quota_bytes": 4096}),
        json.dumps({"action": "quit"}),
    ]) + "\n")
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)

    assert o02_probe.main(["--config", "scheduler.yaml", "--filesystem", "ext4"]) == 0

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"] == "the command is not JSON"
    assert "nonsense" in lines[1]["error"]
    assert lines[2]["private_mount_namespace"] is True
    assert "mount --bind is not permitted" in lines[3]["error"]
    assert lines[4]["bytes_written"] == 4096
    assert lines[5]["bye"] is True
