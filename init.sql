CREATE DATABASE IF NOT EXISTS marunage2
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE marunage2;

CREATE TABLE IF NOT EXISTS tasks (
  id BIGINT NOT NULL AUTO_INCREMENT,
  parent_task_id BIGINT NULL,
  root_task_id BIGINT NOT NULL,
  task_type VARCHAR(64) NOT NULL,
  phase TINYINT NOT NULL,
  status VARCHAR(32) NOT NULL,
  requested_by_role VARCHAR(32) NOT NULL,
  assigned_role VARCHAR(32) NOT NULL,
  assigned_service VARCHAR(32) NOT NULL,
  assigned_model VARCHAR(64) NULL,
  model_contract_json JSON NULL,
  priority INT NOT NULL DEFAULT 0,
  workspace_path VARCHAR(255) NULL,
  target_repo VARCHAR(255) NULL,
  target_ref VARCHAR(255) NULL,
  working_branch VARCHAR(255) NULL,
  runtime_spec_json JSON NULL,
  payload_json JSON NULL,
  result_summary_md MEDIUMTEXT NULL,
  lease_owner VARCHAR(128) NULL,
  lease_expires_at DATETIME NULL,
  retry_count INT NOT NULL DEFAULT 0,
  max_retry INT NOT NULL DEFAULT 0,
  approval_required BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  started_at DATETIME NULL,
  finished_at DATETIME NULL,
  PRIMARY KEY (id),
  KEY idx_tasks_queue (assigned_service, status, priority, created_at),
  KEY idx_tasks_root (root_task_id, phase, status),
  KEY idx_tasks_lease (status, lease_expires_at),
  KEY idx_tasks_type (task_type, status),
  CONSTRAINT chk_tasks_phase CHECK (phase BETWEEN 0 AND 99),
  CONSTRAINT chk_tasks_status CHECK (
    status IN ('queued', 'leased', 'running', 'waiting_approval', 'succeeded', 'failed', 'blocked', 'cancelled')
  )
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT NOT NULL AUTO_INCREMENT,
  task_id BIGINT NOT NULL,
  root_task_id BIGINT NOT NULL,
  phase TINYINT NOT NULL,
  sender_role VARCHAR(32) NOT NULL,
  receiver_role VARCHAR(32) NOT NULL,
  message_kind VARCHAR(32) NOT NULL,
  content_md MEDIUMTEXT NOT NULL,
  content_redaction_json JSON NULL,
  artifact_refs_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_messages_task (task_id, created_at),
  KEY idx_messages_root (root_task_id, phase, created_at),
  CONSTRAINT fk_messages_task FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS logs (
  id BIGINT NOT NULL AUTO_INCREMENT,
  task_id BIGINT NULL,
  root_task_id BIGINT NULL,
  service VARCHAR(32) NOT NULL,
  component VARCHAR(64) NOT NULL,
  level VARCHAR(16) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  message TEXT NOT NULL,
  details_json JSON NULL,
  redaction_state VARCHAR(16) NOT NULL DEFAULT 'clean',
  trace_id VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_logs_task (task_id, created_at),
  KEY idx_logs_service (service, level, created_at),
  KEY idx_logs_event (event_type, created_at),
  CONSTRAINT fk_logs_task FOREIGN KEY (task_id) REFERENCES tasks(id),
  CONSTRAINT chk_logs_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR', 'AUDIT')),
  CONSTRAINT chk_logs_redaction CHECK (redaction_state IN ('clean', 'redacted', 'blocked'))
);

CREATE TABLE IF NOT EXISTS port_allocator (
  id BIGINT NOT NULL AUTO_INCREMENT,
  service_name VARCHAR(32) NOT NULL,
  port_range_start INT NOT NULL,
  port_range_end INT NOT NULL,
  last_allocated_port INT NULL,
  reservation_state_json JSON NULL,
  lease_owner VARCHAR(128) NULL,
  lease_expires_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_port_allocator_service (service_name),
  CONSTRAINT chk_port_allocator_range CHECK (port_range_start > 0 AND port_range_end >= port_range_start)
);

INSERT INTO port_allocator (
  service_name,
  port_range_start,
  port_range_end,
  last_allocated_port,
  reservation_state_json,
  lease_owner,
  lease_expires_at
)
VALUES
  ('dashboard', 18080, 18179, NULL, JSON_OBJECT(), NULL, NULL),
  ('librarian', 18180, 18279, NULL, JSON_OBJECT(), NULL, NULL),
  ('verification_mariadb', 18300, 18399, NULL, JSON_OBJECT(), NULL, NULL)
ON DUPLICATE KEY UPDATE
  port_range_start = VALUES(port_range_start),
  port_range_end = VALUES(port_range_end),
  reservation_state_json = VALUES(reservation_state_json);