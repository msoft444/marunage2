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
        <p class="task-meta">${escapeHtml(task.repository_path || 'repo 未指定')}</p>
      </div>
      <a class="detail-link" href="#/tasks/${encodeURIComponent(task.id)}">詳細を表示</a>
    </li>
  `).join('');
}

function renderTaskDetail(payload) {
  const title = document.getElementById('task-detail-title');
  const meta = document.getElementById('task-detail-meta');
  const logs = document.getElementById('task-detail-logs');
  const result = document.getElementById('task-detail-result');
  if (!title || !meta || !logs || !result) {
    return;
  }
  title.textContent = `Task #${payload.task.id}`;
  meta.innerHTML = `
    <div><dt>Status</dt><dd>${escapeHtml(payload.task.status)}</dd></div>
    <div><dt>Service</dt><dd>${escapeHtml(payload.task.assigned_service)}</dd></div>
    <div><dt>Type</dt><dd>${escapeHtml(payload.task.task_type)}</dd></div>
    <div><dt>Repository</dt><dd>${escapeHtml(payload.task.repository_path || 'repo 未指定')}</dd></div>
  `;
  logs.innerHTML = payload.logs.length
    ? payload.logs.map((log) => `<li><strong>${escapeHtml(log.service)}</strong> ${escapeHtml(log.event_type)}<br />${escapeHtml(log.message)}</li>`).join('')
    : '<li class="task-empty">ログはまだありません。</li>';
  result.innerHTML = payload.result_html || '<p>結果はまだありません。</p>';
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
  const state = document.getElementById('submit-state');
  const message = document.getElementById('task-submit-message');
  if (!title || !instruction || !repositoryPath || !state || !message) {
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
      }),
    });
    state.textContent = payload.task.status;
    message.textContent = `Task #${payload.task.id} を登録しました。`;
    window.location.hash = `#/tasks/${payload.task.id}`;
  } catch (error) {
    state.textContent = 'error';
    message.textContent = `投稿に失敗しました: ${String(error)}`;
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
  if (!root || !output || !form) {
    return;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    await submitTask();
  });
  window.addEventListener('hashchange', () => {
    void routeDashboard();
  });

  await updateHealth(root, output);
  await routeDashboard();
}

void bootMarunageDashboard();