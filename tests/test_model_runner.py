from model_scheduler.model_runner import docker_stop_argv


def test_runner_only_stops_exact_manifest_container() -> None:
    assert docker_stop_argv("sms-thor-local-qwen-small", "sms-thor-local-qwen-small") == ["docker", "stop", "--time", "30", "sms-thor-local-qwen-small"]
    assert docker_stop_argv("other", "sms-thor-local-qwen-small") is None
