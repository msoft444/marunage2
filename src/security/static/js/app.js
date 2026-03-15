async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `request_failed:${response.status}`);
  }
  return payload;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function toStatusClass(status) {
  return String(status).replaceAll(/[^a-zA-Z0-9_-]/g, '-');
}

function localizeErrorMessage(error, fallback) {
  const raw = String(error || '');
  const mappings = [
    ['working_branch_not_found', '作業ブランチが見つかりません。'],
    ['merge_target_not_found', '元ブランチが見つかりません。'],
    ['orchestration_payload_invalid', 'タスク情報の整合性に問題があります。'],
    ['phase_rework_limit_exceeded', '差し戻し回数の上限に達しました。'],
    ['root_task_not_found', '親タスクが見つかりません。'],
    ['task_not_found', 'タスクが見つかりません。'],
    ['request_failed:404', '対象データが見つかりません。'],
    ['request_failed:409', '現在の状態ではこの操作を実行できません。'],
    ['request_failed:500', 'サーバーでエラーが発生しました。'],
  ];

  for (const [needle, message] of mappings) {
    if (raw.includes(needle)) {
      return message;
    }
  }

  return fallback;
}

function renderTaskList(tasks) {
  const list = document.getElementById('task-list');
  if (!list) {
    return;
  }
  if (!tasks.length) {
    list.innerHTML = '<li class="task-empty">まだタスクはありません。</li>';
    return;
  }
  list.innerHTML = tasks.map((task) => `
    <li class="task-item task-status-${toStatusClass(task.status)}">
      <div>
        <p class="task-title">#${escapeHtml(task.id)} ${escapeHtml(task.task_type)}</p>
        <p class="task-meta">${escapeHtml(task.assigned_service)} / ${escapeHtml(task.status)}</p>
        <p class="task-meta">現在フェーズ: ${escapeHtml(task.current_phase ?? '-')} / 最終完了フェーズ: ${escapeHtml(task.last_completed_phase ?? '-')}</p>
        <p class="task-meta">${escapeHtml(task.repository_path || 'repo 未指定')}</p>
      </div>
      <a class="detail-link" href="#/tasks/${encodeURIComponent(task.id)}">詳細を表示</a>
    </li>
  `).join('');
}

function setTextContent(element, value, fallback = '') {
  if (!element) {
    return;
  }
  element.textContent = value == null || value === '' ? fallback : String(value);
}

function getLogDetails(log) {
  if (!log || typeof log !== 'object') {
    return null;
  }
  const details = log.details ?? log.details_json ?? null;
  return details && typeof details === 'object' ? details : null;
}

function formatLogValue(value) {
  if (value == null || value === '') {
    return '-';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value);
}

function renderViolationRows(violations) {
  return violations.map((violation) => `
    <tr>
      <td>${escapeHtml(formatLogValue(violation.compose_file))}</td>
      <td>${escapeHtml(formatLogValue(violation.service))}</td>
      <td>${escapeHtml(formatLogValue(violation.field))}</td>
      <td>${escapeHtml(formatLogValue(violation.rule_id))}</td>
      <td>${escapeHtml(formatLogValue(violation.raw_value))}</td>
      <td>${escapeHtml(formatLogValue(violation.message))}</td>
    </tr>
  `).join('');
}

function renderLogItem(log) {
  const details = getLogDetails(log);
  const violations = Array.isArray(details?.violations) ? details.violations : [];
  if (log.event_type === 'compose_validation_blocked' || violations.length) {
    const blockedLabel = details?.blocked_reason === 'compose_validation' || violations.length
      ? 'Compose Validation により安全側でブロック'
      : (details?.blocked_reason_label || log.event_type);
    const violationContent = violations.length
      ? `
        <div class="log-violation-wrap">
          <table class="log-violation-table">
            <thead>
              <tr>
                <th>ファイル</th>
                <th>サービス</th>
                <th>フィールド</th>
                <th>ルール</th>
                <th>値</th>
                <th>メッセージ</th>
              </tr>
            </thead>
            <tbody>${renderViolationRows(violations)}</tbody>
          </table>
        </div>
      `
      : '<p class="task-empty">violation 詳細なし</p>';
    return `
      <li class="log-blocked-reason">
        <div class="log-blocked-title">${escapeHtml(blockedLabel)}</div>
        <p class="log-blocked-meta"><strong>${escapeHtml(log.service || '-')}</strong> ${escapeHtml(log.event_type || '-')}</p>
        <p class="log-blocked-message">${escapeHtml(log.message || 'Compose validation failed')}</p>
        ${violationContent}
      </li>
    `;
  }
  return `<li><strong>${escapeHtml(log.service)}</strong> ${escapeHtml(log.event_type)}<br />${escapeHtml(log.message)}</li>`;
}

function renderLogItems(logs) {
  return logs.map((log) => renderLogItem(log)).join('');
}

function renderSubtaskAccordion(task) {
  const llmModel = task.llm_model || 'N/A';
  const handoffMessage = task.handoff_message || '引き継ぎ事項なし';
  const phaseSummary = task.phase_summary || '要約なし';
  const resultSummary = task.result_summary_md || '結果なし';
  const logs = Array.isArray(task.logs) && task.logs.length
    ? `<ul class="log-list">${renderLogItems(task.logs)}</ul>`
    : '<p class="task-empty">ログなし</p>';

  return `
    <li class="subtask-item task-status-${toStatusClass(task.status)}">
      <details class="subtask-accordion">
        <summary class="subtask-summary">
          <span>Phase ${escapeHtml(task.phase ?? '-')}</span>
          <span>${escapeHtml(task.task_type)}</span>
          <span>${escapeHtml(task.status)}</span>
          <span>${escapeHtml(llmModel)}</span>
        </summary>
        <div class="subtask-details-grid">
          <div class="detail-text-group">
            <h5 class="detail-section-title">要約</h5>
            <pre class="result-card detail-text-block detail-reading-panel">${escapeHtml(phaseSummary)}</pre>
          </div>
          <div class="detail-text-group">
            <h5 class="detail-section-title">引き継ぎ事項</h5>
            <pre class="result-card detail-text-block detail-reading-panel">${escapeHtml(handoffMessage)}</pre>
          </div>
          <div class="detail-text-group">
            <h5 class="detail-section-title">結果</h5>
            <pre class="result-card detail-text-block detail-reading-panel">${escapeHtml(resultSummary)}</pre>
          </div>
          <div class="detail-text-group">
            <h5 class="detail-section-title">ログ</h5>
            ${logs}
          </div>
        </div>
      </details>
    </li>
  `;
}

function renderSubtasks(subtasks) {
  const panel = document.getElementById('task-subtasks-panel');
  const list = document.getElementById('task-subtasks');
  const empty = document.getElementById('task-subtasks-empty');
  if (!panel || !list || !empty) {
    return;
  }
  list.className = 'subtask-list';
  panel.hidden = false;
  if (!subtasks || !subtasks.length) {
    empty.hidden = false;
    list.innerHTML = '';
    return;
  }
  empty.hidden = true;
  list.innerHTML = subtasks.map((task) => renderSubtaskAccordion(task)).join('');
}

function renderTaskDetail(payload) {
  const title = document.getElementById('task-detail-title');
  const meta = document.getElementById('task-detail-meta');
  const logs = document.getElementById('task-detail-logs');
  const result = document.getElementById('task-detail-result');
  const instructionPanel = document.getElementById('task-detail-instruction-panel');
  const instruction = document.getElementById('task-detail-instruction');
  if (!title || !meta || !logs || !result || !instructionPanel || !instruction) {
    return;
  }
  title.textContent = `タスク #${payload.task.id}`;
  meta.innerHTML = `
    <div><dt>状態</dt><dd>${escapeHtml(payload.task.status)}</dd></div>
    <div><dt>担当サービス</dt><dd>${escapeHtml(payload.task.assigned_service)}</dd></div>
    <div><dt>種別</dt><dd>${escapeHtml(payload.task.task_type)}</dd></div>
    <div><dt>現在フェーズ</dt><dd>${escapeHtml(payload.task.current_phase ?? '-')}</dd></div>
    <div><dt>最終完了フェーズ</dt><dd>${escapeHtml(payload.task.last_completed_phase ?? '-')}</dd></div>
    <div><dt>対象リポジトリ</dt><dd>${escapeHtml(payload.task.repository_path || 'repo 未指定')}</dd></div>
    <div><dt>元ブランチ</dt><dd>${escapeHtml(payload.task.target_ref || '未指定')}</dd></div>
  `;
  if (payload.task.instruction) {
    instructionPanel.hidden = false;
    setTextContent(instruction, payload.task.instruction);
  } else {
    instructionPanel.hidden = true;
    setTextContent(instruction, '');
  }
  renderSubtasks(payload.subtasks || []);
  logs.innerHTML = payload.logs.length
    ? renderLogItems(payload.logs)
    : '<li class="task-empty">ログなし</li>';
  result.innerHTML = payload.result_html || '<p>結果なし</p>';
  void renderApprovalPanel(payload.task);
}

function hideApprovalPanel(message) {
  const panel = document.getElementById('task-approval-panel');
  const approveButton = document.getElementById('task-approve');
  const rejectButton = document.getElementById('task-reject');
  const state = document.getElementById('task-approval-state');
  const diffPreview = document.getElementById('task-diff-preview');
  if (!panel || !approveButton || !rejectButton || !state || !diffPreview) {
    return;
  }
  approveButton.disabled = true;
  rejectButton.disabled = true;
  state.textContent = message || 'マージ済みまたは却下済みのため、承認操作はできません。';
  diffPreview.textContent = '';
  panel.hidden = true;
}

async function loadTaskDetail(taskId) {
  return fetchJson(`/api/v1/tasks/${taskId}`);
}

async function renderApprovalPanel(task) {
  const panel = document.getElementById('task-approval-panel');
  const approveButton = document.getElementById('task-approve');
  const rejectButton = document.getElementById('task-reject');
  const state = document.getElementById('task-approval-state');
  const diffPreview = document.getElementById('task-diff-preview');
  if (!panel || !approveButton || !rejectButton || !state || !diffPreview) {
    return;
  }
  if (task.status !== 'waiting_approval') {
    hideApprovalPanel();
    return;
  }

  panel.hidden = false;
  approveButton.disabled = false;
  rejectButton.disabled = false;
  state.textContent = '差分を読み込み中...';

  try {
    const loadDiff = async () => {
      state.textContent = '差分を取得中...';
      const diffPayload = await fetchJson(`/api/v1/tasks/${task.id}/diff`);
      diffPreview.textContent = diffPayload.diff || '差分はありません。';
      state.textContent = `${task.target_ref || '保存済みブランチ'} との差分を表示中`;
    };

    approveButton.onclick = async () => {
      state.textContent = '承認処理中...';
      approveButton.disabled = true;
      rejectButton.disabled = true;
      const approvalPayload = await fetchJson(`/api/v1/tasks/${task.id}/approve`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({}),
      });
      const refreshedPayload = approvalPayload.task ? await loadTaskDetail(approvalPayload.task.id) : await loadTaskDetail(task.id);
      renderTaskDetail(refreshedPayload);
    };

    rejectButton.onclick = async () => {
      state.textContent = '却下処理中...';
      approveButton.disabled = true;
      rejectButton.disabled = true;
      const rejectPayload = await fetchJson(`/api/v1/tasks/${task.id}/reject`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ reason: 'manual rejection' }),
      });
      const refreshedPayload = rejectPayload.task ? await loadTaskDetail(rejectPayload.task.id) : await loadTaskDetail(task.id);
      renderTaskDetail(refreshedPayload);
    };

    await loadDiff();
  } catch (error) {
    if (String(error).includes('working_branch_not_found')) {
      const refreshedPayload = await loadTaskDetail(task.id);
      if (refreshedPayload.task.status !== 'waiting_approval') {
        renderTaskDetail(refreshedPayload);
        return;
      }
      state.textContent = '作業ブランチが存在しません。状態が不整合のため、承認操作はできません。';
      approveButton.disabled = true;
      rejectButton.disabled = true;
    } else {
      state.textContent = localizeErrorMessage(error, '承認情報の取得に失敗しました。');
    }
    diffPreview.textContent = '';
  }
}

async function loadRepositoryBranches() {
  const repositoryPath = document.getElementById('task-repository-path');
  const targetRef = document.getElementById('task-target-ref');
  const message = document.getElementById('task-submit-message');
  if (!repositoryPath || !targetRef || !message) {
    return;
  }

  const repositoryValue = repositoryPath.value.trim();
  if (!repositoryValue.startsWith('https://github.com/')) {
    targetRef.innerHTML = '<option value="">候補ブランチを選択</option>';
    targetRef.disabled = true;
    return;
  }

  try {
    const payload = await fetchJson(`/api/v1/repositories/branches?repository_url=${encodeURIComponent(repositoryValue)}`);
    const branches = payload.branches || [];
    targetRef.innerHTML = ['<option value="">候補ブランチを選択</option>']
      .concat(branches.map((branch) => `<option value="${escapeHtml(branch)}">${escapeHtml(branch)}</option>`))
      .join('');
    targetRef.disabled = false;
    if (payload.default_branch) {
      targetRef.value = payload.default_branch;
    }
    if (!branches.length) {
      message.textContent = '候補ブランチがありません。';
    }
  } catch (error) {
    targetRef.innerHTML = '<option value="">候補ブランチを選択</option>';
    targetRef.disabled = true;
    message.textContent = localizeErrorMessage(error, 'ブランチ一覧の取得に失敗しました。');
  }
}

async function updateHealth(root, output) {
  try {
    const payload = await fetchJson('/api/v1/health');
    root.dataset.status = payload.status;
    output.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    root.dataset.status = 'degraded';
    output.textContent = `dashboard bootstrap failed: ${String(error)}`;
  }
}

async function submitTask() {
  const title = document.getElementById('task-title');
  const instruction = document.getElementById('task-instruction');
  const repositoryPath = document.getElementById('task-repository-path');
  const targetRef = document.getElementById('task-target-ref');
  const state = document.getElementById('submit-state');
  const message = document.getElementById('task-submit-message');
  if (!title || !instruction || !repositoryPath || !targetRef || !state || !message) {
    return;
  }
  state.textContent = 'sending';
  try {
    const payload = await fetchJson('/api/v1/tasks', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        task: title.value,
        instruction: instruction.value,
        repository_path: repositoryPath.value,
        target_ref: targetRef.value,
      }),
    });
    state.textContent = payload.task.status;
    message.textContent = `Task #${payload.task.id} を登録しました。`;
    window.location.hash = `#/tasks/${payload.task.id}`;
  } catch (error) {
    state.textContent = 'error';
    message.textContent = localizeErrorMessage(error, '投稿に失敗しました。');
  }
}

async function routeDashboard() {
  const listView = document.getElementById('task-list-view');
  const detailView = document.getElementById('task-detail-view');
  if (!listView || !detailView) {
    return;
  }
  const route = window.location.hash || '#/tasks';
  const taskMatch = route.match(/^#\/tasks\/(\d+)$/);
  if (taskMatch) {
    const payload = await fetchJson(`/api/v1/tasks/${taskMatch[1]}`);
    listView.hidden = true;
    detailView.hidden = false;
    renderTaskDetail(payload);
    return;
  }

  const payload = await fetchJson('/api/v1/tasks');
  detailView.hidden = true;
  listView.hidden = false;
  renderTaskList(payload.tasks);
}

async function bootMarunageDashboard() {
  const root = document.getElementById('marunage-app');
  const output = document.getElementById('api-health-output');
  const form = document.getElementById('task-form');
  const repositoryPath = document.getElementById('task-repository-path');
  if (!root || !output || !form || !repositoryPath) {
    return;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    await submitTask();
  });
  repositoryPath.addEventListener('change', () => {
    void loadRepositoryBranches();
  });
  repositoryPath.addEventListener('blur', () => {
    void loadRepositoryBranches();
  });
  window.addEventListener('hashchange', () => {
    void routeDashboard();
  });

  await updateHealth(root, output);
  await routeDashboard();
}

void bootMarunageDashboard();