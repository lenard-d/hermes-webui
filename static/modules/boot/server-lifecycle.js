async function shutdownServer() {
  const ok = await showConfirmDialog({
    title: (typeof t === 'function' ? t('settings_shutdown_confirm_title') : 'Stop Hermes WebUI'),
    message: (typeof t === 'function' ? t('settings_shutdown_confirm_message') : 'Stop the Hermes WebUI server?'),
    confirmLabel: (typeof t === 'function' ? t('settings_shutdown_confirm_btn') : 'Stop'),
    danger: true,
  });
  if (!ok) return;
  localStorage.setItem('hermes-webui-server-stopped', '1');
  try { var bc = new BroadcastChannel('hermes-webui-shutdown'); bc.postMessage('stop'); bc.close(); } catch(_) {}
  showServerStopped();
  try { await api('/api/shutdown', { method: 'POST' }); } catch (_) {}
}

function showServerStopped() {
  const stoppedMsg = typeof t === 'function'
    ? t('settings_shutdown_stopped_message')
    : 'Server stopped. You can close this tab.';
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--muted);font-family:system-ui,ui-sans-serif;font-size:14px"><p>' + stoppedMsg + '</p></div>';
}

export {showServerStopped,shutdownServer};
