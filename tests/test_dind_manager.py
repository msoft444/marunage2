from pathlib import Path

import pytest

from backend.dind_manager import DindConfigurationError, DindManager, DindStartupError


def _prepare_workspace(tmp_path: Path, task_id: int = 42) -> tuple[Path, Path, Path]:
    workspace = tmp_path / str(task_id)
    repo = workspace / "repo"
    artifacts = workspace / "artifacts"
    repo.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    return workspace, repo, artifacts


def test_dind_manager_starts_with_deterministic_names_runtime_spec_and_limits(tmp_path):
    commands: list[tuple[str, ...]] = []
    logs: list[tuple[str, dict | None]] = []
    released: list[tuple[int, list[int]]] = []
    probe_results = iter([False, True])

    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=lambda command: commands.append(tuple(command)),
        reserve_ports=lambda task_id, count: [41080],
        release_ports=lambda task_id, ports: released.append((task_id, list(ports))),
        readiness_probe=lambda spec: next(probe_results),
        sleep=lambda _: None,
        log_callback=lambda event, _message, details=None: logs.append((event, details)),
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)

    spec = manager.start(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    assert spec.container_name == "mn2-task-42-dind"
    assert spec.network_name == "mn2-task-42-net"
    assert spec.proxy_ports == (41080,)
    assert spec.runtime_spec_json["dind"]["validation_profile"] == "trusted_dind"
    assert spec.runtime_spec_json["dind"]["resource_limits"]["memory"] == "2048m"
    assert spec.runtime_spec_json["dind"]["host_uid"] >= 0
    assert spec.runtime_spec_json["dind"]["host_gid"] >= 0
    run_command = next(command for command in commands if command[:3] == ("docker", "run", "-d"))
    assert "--cpus" in run_command
    assert "--memory" in run_command
    assert "--pids-limit" in run_command
    assert "--network" in run_command
    assert "-p" in run_command
    assert f"{spec.repo_path}:/workspace/repo" in run_command
    assert released == []
    assert [event for event, _ in logs] == [
        "dind_proxy_port_reserved",
        "dind_container_start_requested",
        "dind_container_started",
        "dind_dockerd_wait_started",
        "dind_dockerd_ready",
    ]


def test_dind_manager_rejects_docker_socket_and_outside_workspace_mounts(tmp_path):
    manager = DindManager(workspace_root=tmp_path)
    workspace, repo, artifacts = _prepare_workspace(tmp_path)

    with pytest.raises(DindConfigurationError, match="docker socket"):
        manager.build_runtime_spec(
            task_id=42,
            workspace_path=workspace,
            repo_path=repo,
            artifacts_path=artifacts,
            extra_mounts=["/var/run/docker.sock"],
        )

    outside_mount = Path(__file__).resolve().parents[1]
    with pytest.raises(DindConfigurationError, match="outside allowed workspace"):
        manager.build_runtime_spec(
            task_id=42,
            workspace_path=workspace,
            repo_path=repo,
            artifacts_path=artifacts,
            extra_mounts=[outside_mount],
        )


def test_dind_manager_times_out_on_readiness_and_releases_ports(tmp_path):
    commands: list[tuple[str, ...]] = []
    logs: list[str] = []
    released: list[tuple[int, list[int]]] = []
    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=lambda command: commands.append(tuple(command)),
        reserve_ports=lambda task_id, count: [41081],
        release_ports=lambda task_id, ports: released.append((task_id, list(ports))),
        readiness_probe=lambda spec: False,
        sleep=lambda _: None,
        max_readiness_attempts=3,
        log_callback=lambda event, _message, details=None: logs.append(event),
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)

    with pytest.raises(DindStartupError, match="readiness timed out"):
        manager.start(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    assert (42, [41081]) in released
    assert ("docker", "rm", "-f", "mn2-task-42-dind") in commands
    assert ("docker", "network", "rm", "mn2-task-42-net") in commands
    assert "dind_dockerd_wait_timeout" in logs
    assert "dind_container_cleanup_started" in logs


def test_dind_manager_uses_macos_safe_default_readiness_budget(tmp_path):
    manager = DindManager(workspace_root=tmp_path)

    assert manager.max_readiness_attempts == 15
    assert manager.readiness_interval_sec == 2.0


def test_dind_manager_cleanup_continues_after_compose_down_failure_and_releases_ports(tmp_path):
    commands: list[tuple[str, ...]] = []
    logs: list[str] = []
    released: list[tuple[int, list[int]]] = []

    def docker_runner(command):
        command_tuple = tuple(command)
        commands.append(command_tuple)
        if command_tuple[:2] == ("docker", "exec"):
            raise RuntimeError("compose down failed")

    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=docker_runner,
        reserve_ports=lambda task_id, count: [41082],
        release_ports=lambda task_id, ports: released.append((task_id, list(ports))),
        readiness_probe=lambda spec: True,
        sleep=lambda _: None,
        log_callback=lambda event, _message, details=None: logs.append(event),
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)
    spec = manager.build_runtime_spec(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    errors = manager.cleanup(spec)

    assert errors == ["compose_down_failed:compose down failed"]
    assert ("docker", "rm", "-f", "mn2-task-42-dind") in commands
    assert ("docker", "network", "rm", "mn2-task-42-net") in commands
    assert released == [(42, [41082])]
    assert logs == ["dind_container_cleanup_started", "dind_container_cleanup_failed"]


# --- F-01: --privileged flag must be present in docker run command ---


def test_dind_manager_run_command_contains_privileged_flag(tmp_path):
    commands: list[tuple[str, ...]] = []
    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=lambda command: commands.append(tuple(command)),
        reserve_ports=lambda task_id, count: [41090],
        readiness_probe=lambda spec: True,
        sleep=lambda _: None,
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)
    manager.start(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    run_command = next(c for c in commands if c[:3] == ("docker", "run", "-d"))
    assert "--privileged" in run_command


# --- F-02: docker network create must be called during start ---


def test_dind_manager_start_calls_docker_network_create(tmp_path):
    commands: list[tuple[str, ...]] = []
    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=lambda command: commands.append(tuple(command)),
        reserve_ports=lambda task_id, count: [41091],
        readiness_probe=lambda spec: True,
        sleep=lambda _: None,
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)
    manager.start(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    assert ("docker", "network", "create", "mn2-task-42-net") in commands


# --- F-03: port allocation shortage raises DindStartupError ---


def test_dind_manager_raises_on_proxy_port_allocation_shortage(tmp_path):
    released: list[tuple[int, list[int]]] = []
    manager = DindManager(
        workspace_root=tmp_path,
        proxy_port_count=2,
        reserve_ports=lambda task_id, count: [41092],  # returns 1 but 2 requested
        release_ports=lambda task_id, ports: released.append((task_id, list(ports))),
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)

    with pytest.raises(DindStartupError, match="proxy port allocation failed"):
        manager.build_runtime_spec(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)
    assert released == [(42, [41092])]


# --- F-04: dangerous host path raises distinct error message ---


def test_dind_manager_dangerous_host_path_gives_distinct_error_message(tmp_path):
    manager = DindManager(workspace_root=tmp_path)
    workspace, repo, _ = _prepare_workspace(tmp_path)

    with pytest.raises(DindConfigurationError, match="dangerous host path"):
        manager.build_runtime_spec(
            task_id=42,
            workspace_path=workspace,
            repo_path=repo,
            extra_mounts=["/etc"],
        )


# --- F-05: MN2_HOST_UID / MN2_HOST_GID env vars in docker run ---


def test_dind_manager_run_command_contains_uid_gid_env_vars(tmp_path):
    commands: list[tuple[str, ...]] = []
    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=lambda command: commands.append(tuple(command)),
        reserve_ports=lambda task_id, count: [41093],
        readiness_probe=lambda spec: True,
        sleep=lambda _: None,
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)
    spec = manager.start(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    run_command = next(c for c in commands if c[:3] == ("docker", "run", "-d"))
    assert f"MN2_HOST_UID={spec.host_uid}" in " ".join(run_command)
    assert f"MN2_HOST_GID={spec.host_gid}" in " ".join(run_command)


# --- F-06: cleanup operations execute in correct order ---


def test_dind_manager_cleanup_executes_operations_in_order(tmp_path):
    operations: list[str] = []
    released: list[str] = []

    def docker_runner(command):
        command_tuple = tuple(command)
        if command_tuple[:2] == ("docker", "exec"):
            operations.append("compose_down")
        elif command_tuple[:3] == ("docker", "rm", "-f"):
            operations.append("container_rm")
        elif command_tuple[:3] == ("docker", "network", "rm"):
            operations.append("network_rm")

    manager = DindManager(
        workspace_root=tmp_path,
        docker_runner=docker_runner,
        reserve_ports=lambda task_id, count: [41094],
        release_ports=lambda task_id, ports: released.append("port_release"),
        readiness_probe=lambda spec: True,
        sleep=lambda _: None,
    )
    workspace, repo, artifacts = _prepare_workspace(tmp_path)
    spec = manager.build_runtime_spec(task_id=42, workspace_path=workspace, repo_path=repo, artifacts_path=artifacts)

    manager.cleanup(spec)

    assert operations == ["compose_down", "container_rm", "network_rm"]
    assert released == ["port_release"]
