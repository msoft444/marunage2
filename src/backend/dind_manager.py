from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Iterable, Sequence

from security.compose_validator import DANGEROUS_HOST_ROOTS, DOCKER_SOCK
from security.sandbox import WorkspaceSandbox


class DindConfigurationError(ValueError):
    pass


class DindStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class DindResourceLimits:
    cpus: str = "2.0"
    memory: str = "2048m"
    pids_limit: int = 512


@dataclass(frozen=True)
class DindRuntimeSpec:
    task_id: int
    container_name: str
    network_name: str
    workspace_path: str
    repo_path: str
    runtime_root: str
    artifacts_path: str | None
    proxy_ports: tuple[int, ...]
    validation_profile: str
    host_uid: int
    host_gid: int
    resource_limits: DindResourceLimits
    runtime_spec_json: dict[str, Any]


def _run_docker_command(command: Sequence[str]) -> None:
    subprocess.run(list(command), check=True, capture_output=True, text=True)


def _default_readiness_probe(spec: DindRuntimeSpec, docker_runner: Callable[[Sequence[str]], None]) -> bool:
    try:
        docker_runner(("docker", "exec", spec.container_name, "docker", "info"))
    except Exception:
        return False
    return True


class DindManager:
    def __init__(
        self,
        *,
        workspace_root: str | Path = "/workspace",
        base_image: str = "docker:27-dind",
        proxy_port_count: int = 1,
        resource_limits: DindResourceLimits | None = None,
        max_readiness_attempts: int = 15,
        readiness_interval_sec: float = 2.0,
        sandbox: WorkspaceSandbox | None = None,
        docker_runner: Callable[[Sequence[str]], None] | None = None,
        reserve_ports: Callable[[int, int], Iterable[int]] | None = None,
        release_ports: Callable[[int, Sequence[int]], None] | None = None,
        readiness_probe: Callable[[DindRuntimeSpec], bool] | None = None,
        sleep: Callable[[float], None] | None = None,
        log_callback: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.base_image = base_image
        self.proxy_port_count = proxy_port_count
        self.resource_limits = resource_limits or DindResourceLimits()
        self.max_readiness_attempts = max_readiness_attempts
        self.readiness_interval_sec = readiness_interval_sec
        self.sandbox = sandbox or WorkspaceSandbox(str(self.workspace_root))
        self.docker_runner = docker_runner or _run_docker_command
        self.reserve_ports = reserve_ports or self._default_reserve_ports
        self.release_ports = release_ports or self._default_release_ports
        self.readiness_probe = readiness_probe or (lambda spec: _default_readiness_probe(spec, self.docker_runner))
        self.sleep = sleep or time.sleep
        self.log_callback = log_callback or (lambda event, message, details=None: None)

    def build_runtime_spec(
        self,
        *,
        task_id: int,
        workspace_path: str | Path,
        repo_path: str | Path,
        artifacts_path: str | Path | None = None,
        validation_profile: str = "trusted_dind",
        extra_mounts: Sequence[str | Path] | None = None,
    ) -> DindRuntimeSpec:
        workspace = self._normalize_workspace_path(workspace_path)
        repo = self._normalize_mount_source(repo_path)
        if not repo.is_relative_to(workspace):
            raise DindConfigurationError("repo path must stay inside the task workspace")

        runtime_root = (workspace / "runtime").resolve(strict=False)
        runtime_root.mkdir(parents=True, exist_ok=True)

        artifacts = None
        if artifacts_path is not None:
            artifacts = self._normalize_mount_source(artifacts_path)
            artifacts.mkdir(parents=True, exist_ok=True)

        mounts = [repo, runtime_root]
        if artifacts is not None:
            mounts.append(artifacts)
        for mount in extra_mounts or ():
            mounts.append(self._normalize_mount_source(mount))

        container_name = self.container_name(task_id)
        network_name = self.network_name(task_id)
        proxy_ports = tuple(int(port) for port in self.reserve_ports(task_id, self.proxy_port_count))
        if len(proxy_ports) != self.proxy_port_count:
            self.release_ports(task_id, proxy_ports)
            raise DindStartupError("proxy port allocation failed")

        host_uid = os.getuid()
        host_gid = os.getgid()
        runtime_spec_json = {
            "dind": {
                "enabled": True,
                "container_name": container_name,
                "network_name": network_name,
                "proxy_ports": list(proxy_ports),
                "runtime_root": str(runtime_root),
                "validation_profile": validation_profile,
                "host_uid": host_uid,
                "host_gid": host_gid,
                "resource_limits": {
                    "cpus": self.resource_limits.cpus,
                    "memory": self.resource_limits.memory,
                    "pids_limit": self.resource_limits.pids_limit,
                },
            }
        }
        return DindRuntimeSpec(
            task_id=task_id,
            container_name=container_name,
            network_name=network_name,
            workspace_path=str(workspace),
            repo_path=str(repo),
            runtime_root=str(runtime_root),
            artifacts_path=str(artifacts) if artifacts is not None else None,
            proxy_ports=proxy_ports,
            validation_profile=validation_profile,
            host_uid=host_uid,
            host_gid=host_gid,
            resource_limits=self.resource_limits,
            runtime_spec_json=runtime_spec_json,
        )

    def start(
        self,
        *,
        task_id: int,
        workspace_path: str | Path,
        repo_path: str | Path,
        artifacts_path: str | Path | None = None,
        validation_profile: str = "trusted_dind",
        extra_mounts: Sequence[str | Path] | None = None,
    ) -> DindRuntimeSpec:
        spec = self.build_runtime_spec(
            task_id=task_id,
            workspace_path=workspace_path,
            repo_path=repo_path,
            artifacts_path=artifacts_path,
            validation_profile=validation_profile,
            extra_mounts=extra_mounts,
        )
        self.log_callback(
            "dind_proxy_port_reserved",
            "DinD proxy ports reserved",
            {"task_id": spec.task_id, "proxy_ports": list(spec.proxy_ports)},
        )
        self.log_callback(
            "dind_container_start_requested",
            "DinD container start requested",
            spec.runtime_spec_json,
        )

        network_created = False
        container_started = False
        try:
            self.docker_runner(("docker", "network", "create", spec.network_name))
            network_created = True
            self.docker_runner(self._build_run_command(spec))
            container_started = True
            self.log_callback(
                "dind_container_started",
                "DinD container started",
                {"container_name": spec.container_name, "network_name": spec.network_name},
            )
            self._wait_for_dockerd(spec)
            return spec
        except Exception as exc:
            self.cleanup(
                spec,
                compose_down=container_started,
                remove_container=container_started,
                remove_network=network_created,
            )
            if isinstance(exc, DindStartupError):
                raise
            raise DindStartupError(str(exc)) from exc

    def cleanup(
        self,
        spec: DindRuntimeSpec,
        *,
        compose_down: bool = True,
        remove_container: bool = True,
        remove_network: bool = True,
    ) -> list[str]:
        self.log_callback(
            "dind_container_cleanup_started",
            "DinD cleanup started",
            {"container_name": spec.container_name, "network_name": spec.network_name},
        )
        errors: list[str] = []

        if compose_down:
            try:
                self.docker_runner(
                    (
                        "docker",
                        "exec",
                        "-w",
                        "/workspace/repo",
                        spec.container_name,
                        "docker",
                        "compose",
                        "down",
                        "--remove-orphans",
                    )
                )
            except Exception as exc:
                errors.append(f"compose_down_failed:{exc}")

        if remove_container:
            try:
                self.docker_runner(("docker", "rm", "-f", spec.container_name))
            except Exception as exc:
                errors.append(f"container_remove_failed:{exc}")

        if remove_network:
            try:
                self.docker_runner(("docker", "network", "rm", spec.network_name))
            except Exception as exc:
                errors.append(f"network_remove_failed:{exc}")

        self.release_ports(spec.task_id, spec.proxy_ports)

        if errors:
            self.log_callback(
                "dind_container_cleanup_failed",
                "DinD cleanup completed with errors",
                {"errors": list(errors), "proxy_ports": list(spec.proxy_ports)},
            )
            return errors

        self.log_callback(
            "dind_container_cleanup_finished",
            "DinD cleanup finished",
            {"proxy_ports": list(spec.proxy_ports)},
        )
        return []

    def container_name(self, task_id: int) -> str:
        return f"mn2-task-{task_id}-dind"

    def network_name(self, task_id: int) -> str:
        return f"mn2-task-{task_id}-net"

    def _wait_for_dockerd(self, spec: DindRuntimeSpec) -> None:
        self.log_callback(
            "dind_dockerd_wait_started",
            "Waiting for DinD dockerd readiness",
            {"max_attempts": self.max_readiness_attempts, "interval_sec": self.readiness_interval_sec},
        )
        for attempt in range(1, self.max_readiness_attempts + 1):
            if self.readiness_probe(spec):
                self.log_callback(
                    "dind_dockerd_ready",
                    "DinD dockerd is ready",
                    {"attempt": attempt},
                )
                return
            if attempt < self.max_readiness_attempts:
                self.sleep(self.readiness_interval_sec)
        self.log_callback(
            "dind_dockerd_wait_timeout",
            "DinD dockerd readiness timed out",
            {"max_attempts": self.max_readiness_attempts},
        )
        raise DindStartupError("DinD dockerd readiness timed out")

    def _build_run_command(self, spec: DindRuntimeSpec) -> tuple[str, ...]:
        command: list[str] = [
            "docker",
            "run",
            "-d",
            "--privileged",
            "--name",
            spec.container_name,
            "--hostname",
            spec.container_name,
            "--network",
            spec.network_name,
            "--cpus",
            spec.resource_limits.cpus,
            "--memory",
            spec.resource_limits.memory,
            "--pids-limit",
            str(spec.resource_limits.pids_limit),
            "-e",
            "DOCKER_TLS_CERTDIR=",
            "-e",
            f"MN2_TASK_ID={spec.task_id}",
            "-e",
            f"MN2_HOST_UID={spec.host_uid}",
            "-e",
            f"MN2_HOST_GID={spec.host_gid}",
        ]
        for port in spec.proxy_ports:
            command.extend(["-p", f"{port}:{port}"])

        command.extend(["-v", f"{spec.repo_path}:/workspace/repo"])
        command.extend(["-v", f"{spec.runtime_root}:/workspace/runtime"])
        if spec.artifacts_path is not None:
            command.extend(["-v", f"{spec.artifacts_path}:/workspace/artifacts"])
        command.append(self.base_image)
        return tuple(command)

    def _normalize_workspace_path(self, workspace_path: str | Path) -> Path:
        normalized = Path(workspace_path).resolve(strict=False)
        if not self.sandbox.validate_workspace_path(str(normalized)):
            raise DindConfigurationError("workspace path escapes workspace root")
        return normalized

    def _normalize_mount_source(self, path: str | Path) -> Path:
        normalized = Path(path).resolve(strict=False)
        if normalized == DOCKER_SOCK:
            raise DindConfigurationError("docker socket mount is not allowed")
        if self.sandbox.validate_mount_source(str(normalized)):
            return normalized
        if normalized == Path("/") or any(normalized == root or normalized.is_relative_to(root) for root in DANGEROUS_HOST_ROOTS):
            raise DindConfigurationError("dangerous host path mount is not allowed")
        raise DindConfigurationError("mount path is outside allowed workspace roots")

    @staticmethod
    def _default_reserve_ports(task_id: int, count: int) -> Iterable[int]:
        _ = task_id
        return tuple(40000 + index for index in range(count))

    @staticmethod
    def _default_release_ports(task_id: int, ports: Sequence[int]) -> None:
        _ = (task_id, ports)
