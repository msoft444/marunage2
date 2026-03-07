async function bootMarunageDashboard() {
  const root = document.getElementById('marunage-app');
  const output = document.getElementById('api-health-output');
  if (!root || !output) {
    return;
  }

  try {
    const response = await fetch('/api/v1/health');
    const payload = await response.json();
    root.dataset.status = payload.status;
    output.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    root.dataset.status = 'degraded';
    output.textContent = `dashboard bootstrap failed: ${String(error)}`;
  }
}

void bootMarunageDashboard();