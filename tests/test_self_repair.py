import pytest


def test_DL_01_brain_disappearance(task_backend):
    resolved_status = task_backend.resolve_orphan_promote(parent_status="running", promote_status="succeeded")
    assert resolved_status in {"succeeded", "blocked"}


def test_DL_02_guardian_self_update(task_backend):
    sequence = task_backend.guardian_self_update_sequence()
    assert sequence == ["start_new", "handover", "stop_old"]


def test_DL_03_schema_migration_order(task_backend):
    sequence = task_backend.migration_plan()
    assert sequence == ["start_new", "apply_backward_compatible_migration", "stop_old"]


def test_DL_04_restart_promote_exclusion(task_backend):
    operations = task_backend.schedule_service_operations()
    assert operations == ["promote_release"]


def test_DL_05_stale_lease_write_is_rejected(task_backend):
    write_result = task_backend.write_result("worker-a", "worker-b", {"summary": "done"})
    assert write_result["status"] == "rejected"


def test_RC_01_port_reservation_releases_on_failed_boot(task_backend):
    result = task_backend.reserve_port_race()
    assert result["released_on_failure"] is True


def test_RC_02_port_exhaustion_alerts_and_recovers(task_backend):
    result = task_backend.port_exhaustion_policy()
    assert "port_exhaustion" in result["alerts"] and result["cleanup"] is True


def test_RC_03_port_candidate_selection_avoids_linear_collision(task_backend):
    candidates = task_backend.next_port_candidates(18080, 4)
    assert candidates != [18080, 18081, 18082, 18083]


def test_RC_04_container_name_conflict_retries(task_backend):
    strategy = task_backend.container_name_conflict_strategy()
    assert strategy == "retry"


def test_RC_05_network_cleanup_avoids_reuse(task_backend):
    policy = task_backend.network_cleanup_policy()
    assert policy == "replace_network"


def test_TS_01_terminal_state_cannot_regress(task_backend):
    task_backend.connection.tasks[1]["status"] = "succeeded"
    allowed = task_backend.transition_status(1, "succeeded", "running")
    assert allowed is False


def test_TS_02_double_lease_is_impossible(task_backend):
    owners = task_backend.lease_twice()
    assert len(set(owners)) == 1


def test_TS_02b_second_lease_attempt_hits_db_conflict(task_backend):
    task_backend.lease_twice()
    lease_updates = [statement for statement, _ in task_backend.connection.statements if "UPDATE tasks SET status = 'leased'" in statement]
    assert len(lease_updates) == 2


def test_TS_03_lease_policy_is_phase_aware(task_backend):
    policy = task_backend.lease_policy()
    assert policy["phase_3_seconds"] >= 900 and policy["docker_seconds"] >= 1800 and policy["heartbeat_seconds"] <= policy["phase_3_seconds"] // 3


def test_TS_04_failed_tasks_offer_recovery(task_backend):
    result = task_backend.failed_task_recovery()
    assert result["action"] == "waiting_approval" and result["approval"] is True


def test_TS_05_transition_rejects_stale_current_status(task_backend):
    task_backend.connection.tasks[1]["status"] = "running"
    with pytest.raises(RuntimeError):
        task_backend.transition_status(1, "queued", "succeeded")


def test_MC_01_contract_digest_uses_canonical_sha256(task_backend):
    spec = task_backend.compute_contract_digest_spec()
    assert spec == {"algorithm": "sha256", "canonicalized": True, "shared_library": True}


def test_MC_02_model_aliases_are_allowlisted(task_backend):
    result = task_backend.validate_model_alias("gpt-5.4", "gpt-5.4-2026-02")
    assert result["status"] == "model_validated"


def test_MC_03_invalid_contract_is_blocked(task_backend):
    result = task_backend.parse_contract('{"phase": 3')
    assert result["status"] == "blocked"


def test_GD_01_guardian_is_not_single_point_of_failure(task_backend):
    policy = task_backend.guardian_runtime_policy()
    assert policy["self_restart"] == "restart_always" and policy["health_source"] == "independent_monitor"


def test_GD_02_promote_requires_test_evidence(task_backend):
    result = task_backend.validate_promote_payload({"branch": "main", "commit": "abc123"})
    assert result["accepted"] is False and result["requires_test_evidence"] is True


def test_GD_03_blocked_tasks_requeue_when_dependencies_recover(task_backend):
    result = task_backend.recover_blocked_tasks({"docker": True, "mariadb": True, "copilot": True})
    assert result["requeued"] >= 1 and result["remaining_blocked"] is False


def test_CX_01_compound_failure_breaks_retry_loop(task_backend):
    strategy = task_backend.compound_failure_strategy()
    assert strategy == "block-and-escalate"


def test_CX_02_secret_false_positive_escalates(task_backend):
    result = task_backend.false_positive_resolution()
    assert result["action"] == "request_exception_review" and result["escalated"] is True


def test_CX_03_blue_green_defers_on_db_pool_pressure(task_backend):
    result = task_backend.blue_green_capacity()
    assert result["db_pool_exhausted"] is False and result["delayed"] is True
