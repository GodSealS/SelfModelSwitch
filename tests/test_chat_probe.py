"""CT01: the fixed-image chat probe.

These tests never touch a network, a model or Docker: the shell, the HTTP
surface and the clock are injected. What they prove is the part that a real run
cannot prove on its own — that a probe cannot be talked into writing `passed`
for something it did not observe, and that incomplete or ill-formed inputs stop
the run instead of producing material.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from model_scheduler.acceptance import chat_probe as probe_module
from model_scheduler.acceptance.chat_probe import (
    CaseContext,
    ChatProbe,
    HttpResponse,
    OutputDirectory,
    ProbeInputError,
    load_site_input,
    main,
    read_gguf_chat_template,
    render_probe_png,
)

SERVICE = "http://127.0.0.1:8090"
UPSTREAM_7B = "http://127.0.0.1:10002"
UPSTREAM_27B = "http://127.0.0.1:10003"
IMAGE_DIGEST = "sms-llama-cpp@sha256:" + "8e" * 32
VERSION_OUTPUT = "version: 0.4.1-dev (build 1, commit 4bc272f)\nbuilt with GNU 11.4.0 for Linux aarch64\n"
_TEMPLATE = b"{{ messages }}"
MODEL_BYTES = (b"GGUF" + struct.pack("<IQQ", 3, 0, 1)
                + struct.pack("<Q", len(b"tokenizer.chat_template")) + b"tokenizer.chat_template"
                + struct.pack("<IQ", 8, len(_TEMPLATE)) + _TEMPLATE)

HELP_OUTPUT = ("--jinja, --no-jinja    whether to use jinja template engine for chat\n"
               "--reasoning-format FORMAT  controls thought tags; - deepseek\n"
               "--reasoning-effort LEVEL   reasoning effort level\n"
               "--ignore-eos              ignore end of stream token\n")


# --------------------------------------------------------------------------- fakes


class FakeShell:
    def __init__(self, *, inspect_id: str = IMAGE_DIGEST, repo_digests: tuple[str, ...] = (IMAGE_DIGEST,),
                 version: str = VERSION_OUTPUT,
                 help_text: str = HELP_OUTPUT, git_head: str = "a" * 40, dirty: bool = False) -> None:
        self.inspect_id = inspect_id
        self.repo_digests = list(repo_digests)
        self.version = version
        self.help_text = help_text
        self.git_head = git_head
        self.dirty = dirty
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout: float):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if "image inspect" in joined:
            document = [{"Id": self.inspect_id, "RepoDigests": self.repo_digests}]
            return probe_module.CommandResult(tuple(argv), 0, json.dumps(document), "")
        if "--version" in argv:
            return probe_module.CommandResult(tuple(argv), 0, self.version, "")
        if "--help" in argv:
            return probe_module.CommandResult(tuple(argv), 0, self.help_text, "")
        if "rev-parse" in joined:
            return probe_module.CommandResult(tuple(argv), 0, self.git_head + "\n", "")
        if "status --porcelain" in joined:
            return probe_module.CommandResult(tuple(argv), 0, " dirty\n" if self.dirty else "", "")
        if "remote -v" in joined:
            return probe_module.CommandResult(tuple(argv), 0, "origin\thttps://example.invalid/x.git (fetch)\n", "")
        if "branch --show-current" in joined:
            return probe_module.CommandResult(tuple(argv), 0, "chore/test\n", "")
        return probe_module.CommandResult(tuple(argv), -1, "", "unavailable")


class FakeHttp:
    """Routes by URL; the probe must never reach an address the site did not name."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, bytes | None]] = []

    def request(self, method: str, url: str, *, headers=None, body=None, timeout: float) -> HttpResponse:
        self.calls.append((method, url, body))
        return self._handler(method, url, body)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 4, 0, 0, tzinfo=timezone.utc)

    def utc_now(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now

    def monotonic(self) -> float:
        return 0.0


# --------------------------------------------------------------------------- builders


def _gguf_bytes(template: str | None) -> bytes:
    payload = b"GGUF" + struct.pack("<IQQ", 3, 0, 0 if template is None else 1)
    if template is None:
        return payload
    key = b"tokenizer.chat_template"
    value = template.encode("utf-8")
    return (payload + struct.pack("<Q", len(key)) + key + struct.pack("<I", 8)
            + struct.pack("<Q", len(value)) + value)


def _completion(*, finish_reason: str = "stop", completion_tokens: int = 7, prompt_tokens: int = 11,
                content: str = "ok", reasoning: str | None = None, tool_calls: list | None = None) -> bytes:
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return json.dumps({
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }).encode("utf-8")


def _site_document(root: Path, *, phase: str = "baseline", request_limit: int = 64,
                   capabilities: tuple[str, ...] = ("chat", "vision"), template_sha: str | None = None,
                   policy_sha: str | None = None, fixture_sha: str | None = None) -> dict:
    model_root = root / "models"
    model_root.mkdir(exist_ok=True)
    # Real GGUF metadata, so the template the runtime would use can be read.
    for name in ("7b.gguf", "7b-mmproj.gguf", "27b.gguf", "27b-mmproj.gguf"):
        (model_root / name).write_bytes(MODEL_BYTES)
    (root / "hardware-raw.txt").write_text("hardware\n")
    envelope = {"ctx_size": 8192, "max_input_tokens": 4096, "max_output_tokens": 1024, "max_parallel": 1,
                "max_image_tokens": 1280, "max_image_edge_pixels": 1024, "max_images": 1}
    models = {
        "qwen25vl-7b": {"capabilities": list(capabilities), "runtime_id": "llama-cpp-1",
                        "profile_id": "llama-cpp-gguf-v1", "image_digest": IMAGE_DIGEST,
                        "model_path": str(model_root / "7b.gguf"),
                        "model_sha256": sha_of(MODEL_BYTES),
                        "projector_path": str(model_root / "7b-mmproj.gguf"),
                        "projector_sha256": sha_of(MODEL_BYTES), "template_sha256": template_sha,
                        "upstream_base_url": UPSTREAM_7B, "envelope": envelope,
                        "launch_argv": ["docker", "run", "--name", "sms-7b", IMAGE_DIGEST]},
        "qwen36-27b": {"capabilities": list(capabilities), "runtime_id": "llama-cpp-1",
                       "profile_id": "llama-cpp-gguf-v1", "image_digest": IMAGE_DIGEST,
                       "model_path": str(model_root / "27b.gguf"),
                       "model_sha256": sha_of(MODEL_BYTES),
                       "projector_path": str(model_root / "27b-mmproj.gguf"),
                       "projector_sha256": sha_of(MODEL_BYTES), "template_sha256": template_sha,
                       "upstream_base_url": UPSTREAM_27B, "envelope": envelope,
                       "launch_argv": ["docker", "run", "--name", "sms-27b", IMAGE_DIGEST]},
    }
    return {"schema_version": 1, "checkout": str(root / "checkout"), "remote_url": "https://example.invalid/x.git",
            "branch": "chore/test", "expected_sha": "a" * 40, "deployment_id": "sms-orin-lab2", "mode": "lab",
            "phase": phase, "service_base_url": SERVICE, "gateway_base_url": None, "auth_env": None,
            "hardware_file": "hardware-raw.txt", "models": models,
            "timeouts": {"connect_seconds": 5, "read_idle_seconds": 60, "total_seconds": 900},
            "request_limit": request_limit, "policy_source_sha256": policy_sha, "fixture_set_sha256": fixture_sha,
            "rollback_input": str(root / "rollback.json"), "rollback_sha": None, "client_evidence": []}


def sha_of(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _write_site(root: Path, **kwargs) -> Path:
    document = _site_document(root, **kwargs)
    (root / "checkout").mkdir(exist_ok=True)
    (root / "rollback.json").write_text("{}")
    path = root / "site-input.json"
    path.write_text(json.dumps(document, ensure_ascii=False))
    return path


def _always_200(*, finish_reason: str = "stop", completion_tokens: int = 7, tool_calls: list | None = None,
                reasoning: str | None = None):
    def handler(method: str, url: str, body: bytes | None) -> HttpResponse:
        if url.endswith("/props"):
            return HttpResponse(404, {}, b"{}")
        if url.endswith("/apply-template"):
            return HttpResponse(200, {}, json.dumps({"prompt": "prompt"}).encode())
        if url.endswith("/tokenize"):
            return HttpResponse(200, {}, json.dumps({"tokens": list(range(11))}).encode())
        return HttpResponse(200, {}, _completion(finish_reason=finish_reason, completion_tokens=completion_tokens,
                                                 tool_calls=tool_calls, reasoning=reasoning))

    return FakeHttp(handler)


def _build(root: Path, *, http, shell=None, phase: str = "baseline", request_limit: int = 64,
           capabilities: tuple[str, ...] = ("chat", "vision")) -> tuple[ChatProbe, OutputDirectory]:
    site_path = _write_site(root, phase=phase, request_limit=request_limit, capabilities=capabilities)
    site = load_site_input(site_path)
    out = OutputDirectory(probe_module.prepare_output(root / "out"))
    probe = ChatProbe(site, out, shell=shell or FakeShell(), http=http, clock=FakeClock(), phase=phase)
    return probe, out


# --------------------------------------------------------------------------- site input


def test_site_input_rejects_unknown_and_missing_fields(tmp_path):
    document = _site_document(tmp_path)
    _write_site(tmp_path)
    document["surprise"] = 1
    (tmp_path / "bad.json").write_text(json.dumps(document))
    with pytest.raises(ProbeInputError, match="unknown fields"):
        load_site_input(tmp_path / "bad.json")

    incomplete = _site_document(tmp_path)
    incomplete.pop("request_limit")
    (tmp_path / "missing.json").write_text(json.dumps(incomplete))
    with pytest.raises(ProbeInputError, match="missing fields"):
        load_site_input(tmp_path / "missing.json")


def test_site_input_rejects_bool_where_a_number_is_required(tmp_path):
    document = _site_document(tmp_path)
    document["request_limit"] = True
    (tmp_path / "bool.json").write_text(json.dumps(document))
    with pytest.raises(ProbeInputError):
        load_site_input(tmp_path / "bool.json")


def test_site_input_rejects_credentials_and_v1_suffix(tmp_path):
    _write_site(tmp_path)
    document = _site_document(tmp_path)
    document["service_base_url"] = "http://user:secret@127.0.0.1:8090"
    (tmp_path / "creds.json").write_text(json.dumps(document))
    with pytest.raises(ProbeInputError, match="credentials"):
        load_site_input(tmp_path / "creds.json")

    document = _site_document(tmp_path)
    document["service_base_url"] = "http://127.0.0.1:8090/v1"
    (tmp_path / "suffix.json").write_text(json.dumps(document))
    with pytest.raises(ProbeInputError, match="/v1"):
        load_site_input(tmp_path / "suffix.json")


def test_site_input_requires_a_projector_for_vision_and_a_hardware_file(tmp_path):
    _write_site(tmp_path)
    document = _site_document(tmp_path)
    document["models"]["qwen36-27b"]["projector_path"] = None
    document["models"]["qwen36-27b"]["projector_sha256"] = None
    (tmp_path / "no-proj.json").write_text(json.dumps(document))
    with pytest.raises(ProbeInputError, match="projector"):
        load_site_input(tmp_path / "no-proj.json")

    (tmp_path / "hardware-raw.txt").unlink()
    with pytest.raises(ProbeInputError, match="hardware_file"):
        load_site_input(tmp_path / "site-input.json")


def test_candidate_phase_requires_policy_fixture_and_template_identity(tmp_path):
    site = load_site_input(_write_site(tmp_path, phase="candidate"))
    with pytest.raises(ProbeInputError, match="policy_source_sha256"):
        probe_module.require_probe_ready(site, phase="candidate")

    site = load_site_input(_write_site(tmp_path, phase="candidate", policy_sha="f" * 64, fixture_sha="e" * 64))
    with pytest.raises(ProbeInputError, match="template_sha256"):
        probe_module.require_probe_ready(site, phase="candidate")

    good = load_site_input(_write_site(tmp_path, phase="candidate", policy_sha="f" * 64, fixture_sha="e" * 64,
                                       template_sha="d" * 64))
    probe_module.require_probe_ready(good, phase="candidate")
    with pytest.raises(ProbeInputError, match="does not match"):
        probe_module.require_probe_ready(good, phase="baseline")


# --------------------------------------------------------------------------- CLI guards


def test_cli_refuses_a_non_empty_output(tmp_path):
    site = _write_site(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "old-material.json").write_text("{}")
    assert main(["inspect", "--site", str(site), "--output", str(out)], shell=FakeShell(),
                http=_always_200(), clock=FakeClock()) == probe_module.EXIT_INPUT


def test_cli_rejects_unknown_arguments(tmp_path):
    site = _write_site(tmp_path)
    with pytest.raises(SystemExit) as raised:
        main(["inspect", "--site", str(site), "--output", str(tmp_path / "out"), "--bogus", "1"])
    assert raised.value.code == 2


def test_cli_exits_two_when_the_request_limit_cannot_cover_the_cases(tmp_path):
    site = _write_site(tmp_path, request_limit=3)
    assert main(["probe", "--site", str(site), "--phase", "baseline", "--output", str(tmp_path / "out")],
                shell=FakeShell(), http=_always_200(), clock=FakeClock()) == probe_module.EXIT_INPUT


# --------------------------------------------------------------------------- verdict discipline


def test_unknown_field_returning_200_is_never_passed(tmp_path):
    probe, out = _build(tmp_path, http=_always_200())
    report = probe.probe()
    case = next(row for row in report["cases"] if row["id"] == "D09")
    assert case["status"] != "passed"
    fields = case["actual"]["per_model"]["qwen36-27b"]["fields"]
    assert fields[probe_module.UNKNOWN_FIELD]["http_status"] == 200
    assert fields[probe_module.UNKNOWN_FIELD]["forwarded"] is True
    assert (out.root / "probe.json").is_file()


def test_a_short_answer_does_not_prove_a_budget(tmp_path):
    probe, _ = _build(tmp_path, http=_always_200(finish_reason="stop", completion_tokens=7))
    report = probe.probe()
    case = next(row for row in report["cases"] if row["id"] == "D02")
    assert case["status"] == "failed"
    assert case["reason"] == "not_proven"
    assert case["actual"]["supported_output_fields"] == []


def test_only_an_exhausted_budget_enters_the_supported_set(tmp_path):
    def handler(method: str, url: str, body: bytes | None) -> HttpResponse:
        if url.endswith("/props"):
            return HttpResponse(404, {}, b"{}")
        if url.endswith("/apply-template"):
            return HttpResponse(200, {}, json.dumps({"prompt": "prompt"}).encode())
        if url.endswith("/tokenize"):
            return HttpResponse(200, {}, json.dumps({"tokens": list(range(11))}).encode())
        payload = json.loads(body.decode("utf-8"))
        value = payload.get("max_tokens")
        if value is not None:
            return HttpResponse(200, {}, _completion(finish_reason="length", completion_tokens=value))
        return HttpResponse(200, {}, _completion(finish_reason="stop", completion_tokens=7))

    probe, _ = _build(tmp_path, http=FakeHttp(handler))
    report = probe.probe()
    case = next(row for row in report["cases"] if row["id"] == "D02")
    assert case["actual"]["supported_output_fields"] == ["max_tokens"]
    assert case["status"] == "failed"  # the aliases are still unproven
    assert case["actual"]["per_model"]["qwen36-27b"]["max_tokens-32"]["exhausted"] is True


def test_the_unbudgeted_default_is_sent_after_every_other_case(tmp_path):
    def handler(method: str, url: str, body: bytes | None) -> HttpResponse:
        if url.endswith("/props"):
            return HttpResponse(404, {}, b"{}")
        if url.endswith("/apply-template"):
            return HttpResponse(200, {}, json.dumps({"prompt": "prompt"}).encode())
        if url.endswith("/tokenize"):
            return HttpResponse(200, {}, json.dumps({"tokens": list(range(11))}).encode())
        return HttpResponse(200, {}, _completion(finish_reason="length", completion_tokens=32))

    http = FakeHttp(handler)
    probe, _ = _build(tmp_path, http=http)
    probe.probe()
    chats = [json.loads(body.decode("utf-8")) for method, url, body in http.calls if body and "chat/completions" in url]
    unbudgeted = [payload for payload in chats
                  if not any(field in payload for field in probe_module.BUDGET_FIELDS)]
    assert len(unbudgeted) == len(probe.site.models)
    assert chats[-len(unbudgeted):] == unbudgeted  # nothing follows an unbounded request


def test_a_model_nobody_could_serve_is_not_a_failed_verdict(tmp_path):
    def handler(method: str, url: str, body: bytes | None) -> HttpResponse:
        if url.endswith("/props"):
            return HttpResponse(404, {}, b"{}")
        return HttpResponse(503, {}, json.dumps({"error": {"code": "service_unavailable",
                                                           "message": "Service is not ready"}}).encode())

    probe, _ = _build(tmp_path, http=FakeHttp(handler))
    report = probe.probe()
    for case_id in ("D03", "D06"):
        case = next(row for row in report["cases"] if row["id"] == case_id)
        assert case["status"] == "not_run", (case_id, case["status"])
        assert case["reason"] == "the deployment could not serve the model during this case"


def test_send_chat_stops_at_the_request_limit(tmp_path):
    probe, _ = _build(tmp_path, http=_always_200(), request_limit=1)
    context = CaseContext(case_id="D02", out=probe.out, probe=probe)
    assert context.chat("first", "qwen36-27b", {"model": "qwen36-27b", "messages": []}) is not None
    assert context.chat("second", "qwen36-27b", {"model": "qwen36-27b", "messages": []}) is None
    assert probe.requests_used == 1


def test_candidate_phase_exits_three_while_a_case_is_not_passed(tmp_path):
    site = _write_site(tmp_path, phase="candidate", policy_sha="f" * 64, fixture_sha="e" * 64,
                       template_sha="d" * 64)
    assert main(["probe", "--site", str(site), "--phase", "candidate", "--output", str(tmp_path / "out")],
                shell=FakeShell(), http=_always_200(), clock=FakeClock()) == probe_module.EXIT_FAILED


# --------------------------------------------------------------------------- material shape


def test_probe_report_carries_every_case_and_hashed_artifacts(tmp_path):
    probe, out = _build(tmp_path, http=_always_200())
    report = probe.probe()
    assert [row["id"] for row in report["cases"]] == list(probe_module.CASE_IDS)
    for row in report["cases"]:
        assert row["status"] in probe_module.STATUS_CLOSURE
        assert set(row) == {"id", "phase", "status", "source_ref", "request_files", "response_files",
                            "observation_files", "expected", "actual", "reason", "started_at_utc",
                            "ended_at_utc"}
        assert row["started_at_utc"] <= row["ended_at_utc"]
    assert set(report) == {"schema_version", "site_sha256", "phase", "code_sha", "requests_used",
                           "request_limit", "cases", "artifacts"}
    assert report["requests_used"] <= report["request_limit"]
    for artifact in report["artifacts"]:
        assert set(artifact) == {"path", "size", "sha256"}
        assert (out.root / artifact["path"]).is_file()
        assert artifact["size"] == (out.root / artifact["path"]).stat().st_size


def test_identity_accepts_a_digest_resolved_to_its_bare_hex(tmp_path):
    # `docker image inspect` answers with `sha256:<hex>`, while the site names the
    # image as `repo@sha256:<hex>`; both must be recognised as the same image.
    probe, _ = _build(tmp_path, http=_always_200(), shell=FakeShell(inspect_id="sha256:" + "8e" * 32))
    report = probe.probe()
    case = next(row for row in report["cases"] if row["id"] == "D01")
    assert case["status"] == "passed", case["reason"]
    assert case["actual"]["image_digest_mismatch"] == []


def test_identity_mismatch_fails_d01(tmp_path):
    probe, _ = _build(tmp_path, http=_always_200(), shell=FakeShell(inspect_id="sha256:" + "0" * 64,
                                                                    repo_digests=("other@sha256:" + "0" * 32,)))
    report = probe.probe()
    case = next(row for row in report["cases"] if row["id"] == "D01")
    assert case["status"] == "failed"
    assert case["reason"] == "identity mismatch"


def test_diagnostics_go_to_the_upstream_and_never_to_the_compat_surface(tmp_path):
    http = _always_200()
    probe, _ = _build(tmp_path, http=http)
    probe.probe()
    hosts = {url for _, url, _ in http.calls}
    assert any(url.startswith(UPSTREAM_27B) for url in hosts)
    assert any(url.startswith(UPSTREAM_7B) for url in hosts)
    assert any(url.endswith("/apply-template") for url in hosts)
    assert not any(url.startswith(SERVICE) and "apply-template" in url for url in hosts)


# --------------------------------------------------------------------------- inspect


def test_inspect_reads_the_template_from_the_immutable_gguf_metadata(tmp_path):
    target = tmp_path / "model.gguf"
    target.write_bytes(_gguf_bytes("{{ messages }}"))
    assert read_gguf_chat_template(target) == b"{{ messages }}"

    # A model-units string array before the template: skipping fewer elements
    # than the array declares would misalign every following key.
    tokens = [b"a", b"bb", b"ccc"]

    def kv(key: bytes, type_id: int, payload: bytes) -> bytes:
        return struct.pack("<Q", len(key)) + key + struct.pack("<I", type_id) + payload

    def string(value: bytes) -> bytes:
        return struct.pack("<Q", len(value)) + value

    array = struct.pack("<IQ", 8, len(tokens)) + b"".join(string(token) for token in tokens)
    body = kv(b"tokenizer.ggml.tokens", 9, array) + kv(b"tokenizer.chat_template", 8, string(b"{% raw %}"))
    packed = tmp_path / "array.gguf"
    packed.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 2) + body)
    assert read_gguf_chat_template(packed) == b"{% raw %}"
    empty = tmp_path / "empty.gguf"
    empty.write_bytes(_gguf_bytes(None))
    assert read_gguf_chat_template(empty) is None
    assert read_gguf_chat_template(tmp_path / "not-gguf") is None


def test_inspect_reports_what_it_could_not_collect(tmp_path):
    probe, out = _build(tmp_path, http=_always_200(),
                        shell=FakeShell(version="", help_text="", git_head="b" * 40))
    document = probe.inspect()
    assert document["missing"]
    assert (out.root / "inspect.json").is_file()
    assert main(["inspect", "--site", str(_write_site(tmp_path)), "--output", str(tmp_path / "out2")],
                shell=FakeShell(version="", help_text="", git_head="b" * 40), http=_always_200(),
                clock=FakeClock()) == probe_module.EXIT_FAILED


def test_probe_png_is_deterministic_and_a_png(tmp_path):
    assert render_probe_png() == render_probe_png()
    payload = render_probe_png()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert payload.endswith(b"IEND\xaeB`\x82")
