/** Mutations for installing, removing, and enabling extension packages. */

async function updateInstalledExtension(button, onStatusUpdate) {
  if (!button || button.disabled) return;
  const id = button.dataset.extensionToggleId || '';
  const enabled = button.dataset.extensionNextEnabled === 'true';
  if (!id) return;

  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = enabled ? 'Enabling…' : 'Disabling…';
  try {
    const status = await api('/api/extensions/toggle', {
      method: 'POST',
      body: JSON.stringify({ id, enabled }),
    });
    showToast(enabled
      ? 'Extension enabled. Reload WebUI to apply changes.'
      : 'Extension disabled. Reload WebUI to apply changes.');
    onStatusUpdate(status);
  } catch (error) {
    button.disabled = false;
    button.textContent = previousText;
    showToast('Failed to update extension: ' + (error && error.message ? error.message : String(error)));
  }
}

export function bindInstalledExtensionActions(root, onStatusUpdate) {
  if (!root) return;
  root.querySelectorAll('[data-extension-toggle-id]').forEach(button => {
    button.addEventListener('click', () => updateInstalledExtension(button, onStatusUpdate));
  });
}

async function installExtension(button, entry, reloadCatalog) {
  if (!button || button.disabled) return;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = t('ext_gallery_installing');
  try {
    await api('/api/extensions/install', {
      method: 'POST',
      body: JSON.stringify({
        id: entry.id,
        download_url: entry.download_url || entry.download,
        sha256: entry.sha256,
      }),
    });
    const lifecycle = entry.lifecycle || {};
    const restartRequired = !!(lifecycle.restart_required || lifecycle.webui_restart_required);
    const hasFollowUp = !!(entry.post_install || lifecycle.sidecar_start_required || lifecycle.native_host_start_required);
    showToast(restartRequired
      ? t('ext_gallery_install_restart_required')
      : (hasFollowUp ? t('ext_gallery_install_followup') : t('ext_gallery_install_ok')));
    await reloadCatalog();
  } catch (error) {
    button.disabled = false;
    button.textContent = previousText;
    showToast('Install failed: ' + (error && error.message ? error.message : String(error)));
  }
}

async function uninstallExtension(button, id, reloadCatalog) {
  if (!button || button.disabled) return;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = 'Uninstalling…';
  try {
    await api('/api/extensions/uninstall', {
      method: 'POST',
      body: JSON.stringify({ id }),
    });
    showToast('Extension uninstalled.');
    await reloadCatalog();
  } catch (error) {
    button.disabled = false;
    button.textContent = previousText;
    showToast('Uninstall failed: ' + (error && error.message ? error.message : String(error)));
  }
}

export function bindCatalogInstallationActions(root, entries, reloadCatalog) {
  if (!root) return;
  const entriesById = new Map();
  if (Array.isArray(entries)) {
    entries.forEach(entry => {
      if (entry && entry.id) entriesById.set(String(entry.id), entry);
    });
  }

  root.querySelectorAll('[data-ext-install-id]').forEach(button => {
    const entry = entriesById.get(button.dataset.extInstallId);
    if (entry) button.addEventListener('click', () => installExtension(button, entry, reloadCatalog));
  });
  root.querySelectorAll('[data-ext-uninstall-id]').forEach(button => {
    button.addEventListener('click', () => uninstallExtension(button, button.dataset.extUninstallId, reloadCatalog));
  });
}
