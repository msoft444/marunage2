from pathlib import Path

from security.compose_validator import ComposeValidator


def _validate_compose(
    tmp_path: Path,
    compose_text: str | None,
    *,
    compose_name: str = "compose.yml",
    validator_kwargs: dict | None = None,
):
    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    repo_root.mkdir()
    runtime_root.mkdir()
    if compose_text is not None:
        (repo_root / compose_name).write_text(compose_text, encoding="utf-8")
    result = ComposeValidator(repo_root=repo_root, runtime_root=runtime_root, **(validator_kwargs or {})).validate()
    return result, repo_root, runtime_root


def test_compose_validator_blocks_when_no_compose_file_exists(tmp_path):
    result, _, _ = _validate_compose(tmp_path, None)

    assert result["blocked"] is True
    assert result["compose_files"] == []
    assert result["violations"][0]["rule_id"] == "no_compose_file"


def test_compose_validator_blocks_yaml_parse_error(tmp_path):
    result, repo_root, _ = _validate_compose(
        tmp_path,
        "services:\n  web:\n    image: nginx\n      broken: true\n",
    )

    assert result["blocked"] is True
    assert result["compose_files"] == [str(repo_root / "compose.yml")]
    assert result["violations"][0]["rule_id"] == "yaml_parse_error"


def test_compose_validator_blocks_invalid_compose_model_without_services(tmp_path):
    result, _, _ = _validate_compose(tmp_path, 'version: "3.9"\n')

    assert result["blocked"] is True
    assert result["violations"][0]["rule_id"] == "invalid_compose_model"


def test_compose_validator_blocks_oversized_compose_file(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        "services:\n  web:\n    image: nginx\n" + ("# filler\n" * 40),
        validator_kwargs={"file_size_limit_bytes": 32},
    )

    assert result["blocked"] is True
    assert result["violations"][0]["rule_id"] == "file_too_large"


def test_compose_validator_aggregates_multiple_service_violations(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    privileged: true
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
""".strip()
        + "\n",
    )

    rule_ids = {violation["rule_id"] for violation in result["violations"]}
    assert result["blocked"] is True
    assert {"privileged_container", "host_network_mode", "docker_sock_mount"}.issubset(rule_ids)


def test_compose_validator_blocks_host_namespaces_and_short_form_host_ports(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    pid: host
    ipc: host
    userns_mode: host
    ports:
      - 0.0.0.0:8080:80
      - host_ip: 127.0.0.1
        target: 3000
        published: 3000
""".strip()
        + "\n",
    )

    rule_ids = [violation["rule_id"] for violation in result["violations"]]
    assert result["blocked"] is True
    assert "host_pid" in rule_ids
    assert "host_ipc" in rule_ids
    assert "host_userns_mode" in rule_ids
    assert rule_ids.count("host_namespace_port") == 2


def test_compose_validator_blocks_dangerous_and_outside_absolute_mounts(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    volumes:
      - /etc:/mnt/etc:ro
      - /tmp/compose-validator-audit:/mnt/tmp
""".strip()
        + "\n",
    )

    rule_ids = {violation["rule_id"] for violation in result["violations"]}
    assert result["blocked"] is True
    assert "dangerous_host_mount" in rule_ids
    assert "absolute_path_outside_allowed_roots" in rule_ids


def test_compose_validator_blocks_path_traversal_for_short_and_long_bind_mounts(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    volumes:
      - ../../etc/shadow:/mnt/shadow
      - type: bind
        source: ../../outside
        target: /mnt/outside
""".strip()
        + "\n",
    )

    traversal_violations = [violation for violation in result["violations"] if violation["rule_id"] == "path_traversal"]
    assert result["blocked"] is True
    assert len(traversal_violations) == 2


def test_compose_validator_blocks_symlink_escape(tmp_path):
    outside_root = Path(__file__).resolve().parents[1] / "src"
    result, repo_root, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    volumes:
      - ./linked-data:/app/data
""".strip()
        + "\n",
    )
    (repo_root / "linked-data").symlink_to(outside_root, target_is_directory=True)

    result = ComposeValidator(repo_root=repo_root, runtime_root=tmp_path / "runtime").validate()

    assert result["blocked"] is True
    assert any(violation["rule_id"] == "symlink_escape" for violation in result["violations"])


def test_compose_validator_blocks_unapproved_environment_variable_in_path_fields(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    volumes:
      - ${HOME}/data:/app/data
""".strip()
        + "\n",
    )

    assert result["blocked"] is True
    assert any(violation["rule_id"] == "unresolved_env_var" for violation in result["violations"])


def test_compose_validator_allows_runtime_dir_variable_within_runtime_root(tmp_path):
    result, repo_root, runtime_root = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    volumes:
      - ${TASK_RUNTIME_DIR}/config:/app/config:ro
""".strip()
        + "\n",
    )
    (runtime_root / "config").mkdir()

    result = ComposeValidator(repo_root=repo_root, runtime_root=runtime_root).validate()

    assert result["blocked"] is False
    assert result["violations"] == []
    assert result["validated_runtime_root"] == str(runtime_root.resolve())


def test_compose_validator_blocks_outside_file_references_in_build_env_config_and_secret(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    build:
      context: /tmp/outside-context
      dockerfile: /tmp/outside-context/Dockerfile
    env_file:
      - path: /tmp/outside-context/.env
        required: false
configs:
  app_config:
    file: /tmp/outside-context/config.yml
secrets:
  db_password:
    file: /tmp/outside-context/secret.txt
""".strip()
        + "\n",
    )

    rule_ids = {violation["rule_id"] for violation in result["violations"]}
    assert result["blocked"] is True
    assert "build_context_outside_repo" in rule_ids
    assert "dockerfile_outside_repo" in rule_ids
    assert "env_file_outside_repo" in rule_ids
    assert "config_file_outside_repo" in rule_ids
    assert "secret_file_outside_repo" in rule_ids


def test_compose_validator_blocks_external_resources_devices_and_dangerous_capabilities(tmp_path):
    result, _, _ = _validate_compose(
        tmp_path,
        """
services:
  web:
    image: nginx
    devices:
      - /dev/sda:/dev/sda
    cap_add:
      - SYS_ADMIN
networks:
  production_net:
    external: true
volumes:
  shared_data:
    external: true
""".strip()
        + "\n",
    )

    rule_ids = {violation["rule_id"] for violation in result["violations"]}
    assert result["blocked"] is True
    assert "host_device_exposure" in rule_ids
    assert "dangerous_capability" in rule_ids
    assert "external_network" in rule_ids
    assert "external_volume" in rule_ids


def test_compose_validator_blocks_when_any_candidate_file_is_invalid(tmp_path):
    result, repo_root, runtime_root = _validate_compose(tmp_path, "services:\n  web:\n    image: nginx\n")
    (repo_root / "docker-compose.yml").write_text(
        "services:\n  bad:\n    privileged: true\n",
        encoding="utf-8",
    )

    result = ComposeValidator(repo_root=repo_root, runtime_root=runtime_root).validate()

    assert result["blocked"] is True
    assert len(result["compose_files"]) == 2
    assert any(violation["rule_id"] == "privileged_container" for violation in result["violations"])