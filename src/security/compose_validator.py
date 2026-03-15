from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


COMPOSE_FILENAMES = (
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)
ALLOWED_RUNTIME_VARS = {"TASK_RUNTIME_DIR", "MN2_RUNTIME_DIR"}
DANGEROUS_CAPABILITIES = {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "DAC_READ_SEARCH", "DAC_OVERRIDE"}
DANGEROUS_HOST_ROOTS = tuple(Path(path).resolve(strict=False) for path in ("/etc", "/var", "/dev", "/sys", "/proc"))
DOCKER_SOCK = Path("/var/run/docker.sock").resolve(strict=False)
OUTSIDE_PATH_VIOLATIONS = {"absolute_path_outside_allowed_roots", "path_traversal", "symlink_escape"}
SHORT_PORT_HOST_IP_PATTERN = re.compile(
    r"^\s*(\[[0-9A-Fa-f:.]+\]|(?:\d{1,3}\.){3}\d{1,3}|localhost):[^:]+:[^:]+(?:/(?:tcp|udp))?\s*$"
)


class ComposeValidator:
    def __init__(
        self,
        *,
        repo_root: str | Path,
        runtime_root: str | Path | None = None,
        env: dict[str, str] | None = None,
        file_size_limit_bytes: int = 1_000_000,
    ):
        self.repo_root = Path(repo_root).resolve(strict=False)
        self.runtime_root = Path(runtime_root).resolve(strict=False) if runtime_root is not None else None
        self.env = dict(os.environ)
        if env is not None:
            self.env.update(env)
        if self.runtime_root is not None:
            runtime_str = str(self.runtime_root)
            self.env.setdefault("TASK_RUNTIME_DIR", runtime_str)
            self.env.setdefault("MN2_RUNTIME_DIR", runtime_str)
        self.file_size_limit_bytes = file_size_limit_bytes

    @staticmethod
    def discover_compose_files(repo_root: str | Path) -> list[Path]:
        root = Path(repo_root).resolve(strict=False)
        return [candidate for name in COMPOSE_FILENAMES if (candidate := root / name).is_file()]

    def validate(self, extra_compose_files: list[str | Path] | None = None) -> dict[str, Any]:
        compose_files = self.discover_compose_files(self.repo_root)
        if extra_compose_files:
            for extra_file in extra_compose_files:
                candidate = Path(extra_file).resolve(strict=False)
                if candidate not in compose_files:
                    compose_files.append(candidate)

        violations: list[dict[str, Any]] = []
        if not compose_files:
            violations.append(
                self._violation(
                    rule_id="no_compose_file",
                    message="No compose candidate file found",
                )
            )
            return self._result(compose_files, violations)

        for compose_file in compose_files:
            self._validate_compose_file(compose_file, violations)
        return self._result(compose_files, violations)

    def _validate_compose_file(self, compose_file: Path, violations: list[dict[str, Any]]) -> None:
        try:
            size = compose_file.stat().st_size
        except OSError:
            violations.append(
                self._violation(
                    rule_id="yaml_parse_error",
                    compose_file=compose_file,
                    message="Compose file could not be read",
                )
            )
            return

        if size > self.file_size_limit_bytes:
            violations.append(
                self._violation(
                    rule_id="file_too_large",
                    compose_file=compose_file,
                    message="Compose file exceeds size limit",
                )
            )
            return

        try:
            document = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            violations.append(
                self._violation(
                    rule_id="yaml_parse_error",
                    compose_file=compose_file,
                    message="Compose file is not valid YAML",
                )
            )
            return

        if not isinstance(document, dict) or not isinstance(document.get("services"), dict) or not document["services"]:
            violations.append(
                self._violation(
                    rule_id="invalid_compose_model",
                    compose_file=compose_file,
                    message="Compose file must define at least one service",
                )
            )
            return

        self._validate_services(compose_file, document["services"], violations)
        self._validate_top_level_definitions(compose_file, document, violations)

    def _validate_services(self, compose_file: Path, services: dict[str, Any], violations: list[dict[str, Any]]) -> None:
        for service_name, service in services.items():
            if not isinstance(service, dict):
                violations.append(
                    self._violation(
                        rule_id="invalid_compose_model",
                        compose_file=compose_file,
                        service=service_name,
                        message="Service definition must be a mapping",
                    )
                )
                continue

            self._validate_host_mode_field(compose_file, service_name, service, "privileged", "privileged_container", violations)
            self._validate_host_mode_field(compose_file, service_name, service, "network_mode", "host_network_mode", violations, expected="host")
            self._validate_host_mode_field(compose_file, service_name, service, "pid", "host_pid", violations, expected="host")
            self._validate_host_mode_field(compose_file, service_name, service, "ipc", "host_ipc", violations, expected="host")
            self._validate_host_mode_field(compose_file, service_name, service, "userns_mode", "host_userns_mode", violations, expected="host")

            for volume in service.get("volumes") or []:
                source = self._extract_bind_source(volume)
                if source is None:
                    continue
                self._validate_path_reference(
                    compose_file,
                    service_name,
                    "volumes",
                    source,
                    violations,
                    rule_id_outside="absolute_path_outside_allowed_roots",
                )

            for device in service.get("devices") or []:
                violations.append(
                    self._violation(
                        rule_id="host_device_exposure",
                        compose_file=compose_file,
                        service=service_name,
                        field="devices",
                        raw_value=device,
                        message="Host devices are not allowed",
                    )
                )

            for capability in service.get("cap_add") or []:
                if isinstance(capability, str) and capability.upper() in DANGEROUS_CAPABILITIES:
                    violations.append(
                        self._violation(
                            rule_id="dangerous_capability",
                            compose_file=compose_file,
                            service=service_name,
                            field="cap_add",
                            raw_value=capability,
                            message="Dangerous capability is not allowed",
                        )
                    )

            build = service.get("build")
            if isinstance(build, str):
                self._validate_path_reference(
                    compose_file,
                    service_name,
                    "build.context",
                    build,
                    violations,
                    rule_id_outside="build_context_outside_repo",
                )
            elif isinstance(build, dict):
                if "context" in build:
                    self._validate_path_reference(
                        compose_file,
                        service_name,
                        "build.context",
                        build["context"],
                        violations,
                        rule_id_outside="build_context_outside_repo",
                    )
                if "dockerfile" in build:
                    self._validate_path_reference(
                        compose_file,
                        service_name,
                        "build.dockerfile",
                        build["dockerfile"],
                        violations,
                        rule_id_outside="dockerfile_outside_repo",
                    )

            env_file = service.get("env_file")
            for env_file_path in self._iter_env_file_paths(env_file):
                self._validate_path_reference(
                    compose_file,
                    service_name,
                    "env_file",
                    env_file_path,
                    violations,
                    rule_id_outside="env_file_outside_repo",
                )

            for port in service.get("ports") or []:
                if self._port_uses_host_namespace(port):
                    violations.append(
                        self._violation(
                            rule_id="host_namespace_port",
                            compose_file=compose_file,
                            service=service_name,
                            field="ports",
                            raw_value=port,
                            message="Host namespace port bindings are not allowed",
                        )
                    )

    def _validate_top_level_definitions(self, compose_file: Path, document: dict[str, Any], violations: list[dict[str, Any]]) -> None:
        for network_name, network in (document.get("networks") or {}).items():
            if isinstance(network, dict) and self._is_external_resource(network.get("external")):
                violations.append(
                    self._violation(
                        rule_id="external_network",
                        compose_file=compose_file,
                        field=f"networks.{network_name}",
                        raw_value=network,
                        message="External networks are not allowed",
                    )
                )

        for volume_name, volume in (document.get("volumes") or {}).items():
            if isinstance(volume, dict) and self._is_external_resource(volume.get("external")):
                violations.append(
                    self._violation(
                        rule_id="external_volume",
                        compose_file=compose_file,
                        field=f"volumes.{volume_name}",
                        raw_value=volume,
                        message="External volumes are not allowed",
                    )
                )

        for config_name, config in (document.get("configs") or {}).items():
            if isinstance(config, dict) and "file" in config:
                self._validate_path_reference(
                    compose_file,
                    None,
                    "configs.file",
                    config["file"],
                    violations,
                    rule_id_outside="config_file_outside_repo",
                    raw_field=f"configs.{config_name}.file",
                )

        for secret_name, secret in (document.get("secrets") or {}).items():
            if isinstance(secret, dict) and "file" in secret:
                self._validate_path_reference(
                    compose_file,
                    None,
                    "secrets.file",
                    secret["file"],
                    violations,
                    rule_id_outside="secret_file_outside_repo",
                    raw_field=f"secrets.{secret_name}.file",
                )

    def _validate_host_mode_field(
        self,
        compose_file: Path,
        service_name: str,
        service: dict[str, Any],
        field: str,
        rule_id: str,
        violations: list[dict[str, Any]],
        *,
        expected: str | bool = True,
    ) -> None:
        value = service.get(field)
        if value == expected:
            violations.append(
                self._violation(
                    rule_id=rule_id,
                    compose_file=compose_file,
                    service=service_name,
                    field=field,
                    raw_value=value,
                    message=f"{field} is not allowed",
                )
            )

    def _validate_path_reference(
        self,
        compose_file: Path,
        service_name: str | None,
        field: str,
        raw_value: Any,
        violations: list[dict[str, Any]],
        *,
        rule_id_outside: str,
        raw_field: str | None = None,
    ) -> None:
        if not isinstance(raw_value, str):
            return
        resolved_path, violation = self._resolve_path(raw_value)
        if violation is not None:
            emitted_rule_id = violation
            if rule_id_outside != "absolute_path_outside_allowed_roots" and violation in OUTSIDE_PATH_VIOLATIONS:
                emitted_rule_id = rule_id_outside
            violations.append(
                self._violation(
                    rule_id=emitted_rule_id,
                    compose_file=compose_file,
                    service=service_name,
                    field=raw_field or field,
                    raw_value=raw_value,
                    normalized_path=str(resolved_path) if resolved_path is not None else None,
                    message=f"{field} points outside allowed roots",
                )
            )
            return

        assert resolved_path is not None
        if self._is_within_allowed_roots(resolved_path):
            return

        if resolved_path == DOCKER_SOCK:
            violations.append(
                self._violation(
                    rule_id="docker_sock_mount",
                    compose_file=compose_file,
                    service=service_name,
                    field=raw_field or field,
                    raw_value=raw_value,
                    normalized_path=str(resolved_path),
                    message="docker.sock mount is not allowed",
                )
            )
            return

        if resolved_path == Path("/") or any(resolved_path == root or resolved_path.is_relative_to(root) for root in DANGEROUS_HOST_ROOTS):
            violations.append(
                self._violation(
                    rule_id="dangerous_host_mount",
                    compose_file=compose_file,
                    service=service_name,
                    field=raw_field or field,
                    raw_value=raw_value,
                    normalized_path=str(resolved_path),
                    message="Dangerous host path is not allowed",
                )
            )
            return

        violations.append(
            self._violation(
                rule_id=rule_id_outside,
                compose_file=compose_file,
                service=service_name,
                field=raw_field or field,
                raw_value=raw_value,
                normalized_path=str(resolved_path),
                message=f"{field} points outside repo/runtime roots",
            )
        )

    def _resolve_path(self, raw_value: str) -> tuple[Path | None, str | None]:
        expanded = raw_value
        for variable in ALLOWED_RUNTIME_VARS:
            expanded = expanded.replace(f"${{{variable}}}", self.env.get(variable, ""))
        if re.search(r"\$\{[^}]+\}", expanded):
            return None, "unresolved_env_var"

        candidate = Path(expanded)
        if not candidate.is_absolute() and ".." in candidate.parts:
            return None, "path_traversal"

        original_path = candidate
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate

        resolved = candidate.resolve(strict=False)
        if resolved == DOCKER_SOCK or resolved == Path("/") or any(
            resolved == root or resolved.is_relative_to(root) for root in DANGEROUS_HOST_ROOTS
        ):
            return resolved, None
        if original_path.is_absolute() and not self._is_within_allowed_roots(resolved):
            return resolved, "absolute_path_outside_allowed_roots"

        if not original_path.is_absolute() and not self._is_within_allowed_roots(resolved):
            if candidate.exists():
                return resolved, "symlink_escape"
            return resolved, "path_traversal"

        return resolved, None

    def _is_within_allowed_roots(self, candidate: Path) -> bool:
        if candidate.is_relative_to(self.repo_root):
            return True
        if self.runtime_root is not None and candidate.is_relative_to(self.runtime_root):
            return True
        return False

    @staticmethod
    def _iter_env_file_paths(env_file: Any) -> list[str]:
        if env_file is None:
            return []
        if not isinstance(env_file, list):
            env_file = [env_file]

        paths: list[str] = []
        for item in env_file:
            if isinstance(item, str) and item.strip():
                paths.append(item)
                continue
            if isinstance(item, dict):
                path = item.get("path")
                if isinstance(path, str) and path.strip():
                    paths.append(path)
        return paths

    @staticmethod
    def _port_uses_host_namespace(port: Any) -> bool:
        if isinstance(port, dict):
            return bool(port.get("host_ip"))
        if isinstance(port, str):
            return bool(SHORT_PORT_HOST_IP_PATTERN.match(port))
        return False

    @staticmethod
    def _extract_bind_source(volume: Any) -> str | None:
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) < 2:
                return None
            source = parts[0].strip()
            if not source:
                return None
            if source.startswith(("/", ".", "~", "${")) or "/" in source:
                return source
            return None

        if isinstance(volume, dict):
            if volume.get("type") != "bind":
                return None
            source = volume.get("source") or volume.get("src")
            return source if isinstance(source, str) and source.strip() else None

        return None

    @staticmethod
    def _is_external_resource(external: Any) -> bool:
        return external is True or isinstance(external, dict)

    def _result(self, compose_files: list[Path], violations: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "compose_files": [str(path) for path in compose_files],
            "violations": violations,
            "blocked": bool(violations),
            "validated_runtime_root": str(self.runtime_root) if self.runtime_root is not None else None,
        }

    @staticmethod
    def _violation(
        *,
        rule_id: str,
        message: str,
        compose_file: Path | None = None,
        service: str | None = None,
        field: str | None = None,
        raw_value: Any = None,
        normalized_path: str | None = None,
    ) -> dict[str, Any]:
        return {
            "compose_file": str(compose_file) if compose_file is not None else None,
            "service": service,
            "field": field,
            "rule_id": rule_id,
            "message": message,
            "normalized_path": normalized_path,
            "raw_value": raw_value,
        }