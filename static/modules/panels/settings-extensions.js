import { extensionCatalogMarkup } from './extensions/catalog.js';
import { bindExtensionSettings, primeExtensionSettings } from './extensions/configuration.js';
import { renderExtensionsDiagnostics } from './extensions/diagnostics.js';
import { installedExtensionsMarkup } from './extensions/installed.js';
import {
  bindCatalogInstallationActions,
  bindInstalledExtensionActions,
} from './extensions/installations.js';

/** Lifecycle owner for the Extensions settings section. */

let currentStatus = null;
let sidecarMonitorSequence = 0;
let gallerySnapshot = null;
let galleryLoaded = false;
let activeTab = 'gallery';

function renderStatus(status, sequence) {
  currentStatus = status || null;
  renderExtensionsDiagnostics(status, {
    sequence,
    isCurrentSequence: candidate => candidate === sidecarMonitorSequence,
    onStatusUpdate: acceptStatusUpdate,
  });
}

function renderGallery(entries, status) {
  const gallery = $('extensionsGallery');
  const installed = $('extensionsInstalled');
  primeExtensionSettings(status);
  if (gallery) {
    gallery.innerHTML = extensionCatalogMarkup(entries, status);
    bindCatalogInstallationActions(gallery, entries, reloadExtensionsGallery);
  }
  if (installed) {
    installed.innerHTML = installedExtensionsMarkup(
      status && status.extensions,
      !!(status && status.extension_dir_configured),
    );
    bindInstalledExtensionActions(installed, acceptStatusUpdate);
    bindExtensionSettings(installed);
  }
}

function acceptStatusUpdate(status) {
  const sequence = ++sidecarMonitorSequence;
  renderStatus(status, sequence);
  if (gallerySnapshot) {
    gallerySnapshot.status = status;
    renderGallery(gallerySnapshot.entries, status);
  }
}

async function reloadExtensionsGallery() {
  galleryLoaded = false;
  return loadExtensionsGallery();
}

export async function loadExtensionsGallery() {
  galleryLoaded = true;
  const gallery = $('extensionsGallery');
  const installed = $('extensionsInstalled');
  if (gallery) gallery.innerHTML = '<div class="extensions-loading">Loading gallery…</div>';
  if (installed) installed.innerHTML = '<div class="extensions-loading">Loading installed extensions…</div>';

  try {
    const [registry, status] = await Promise.all([
      api('/api/extensions/registry'),
      api('/api/extensions/status'),
    ]);
    const entries = Array.isArray(registry && registry.entries) ? registry.entries : [];
    gallerySnapshot = { entries, status };
    renderGallery(entries, status);
  } catch (error) {
    galleryLoaded = false;
    const message = esc(error && error.message ? error.message : String(error));
    if (gallery) gallery.innerHTML = '<div class="extensions-error">Failed to load gallery: ' + message + '</div>';
    if (installed) installed.innerHTML = '<div class="extensions-error">Failed to load extension status.</div>';
  }
}

export async function loadExtensionsPanel(options) {
  const target = $('extensionsDiagnostics');
  const copyButton = $('extensionsCopyDiagnosticsBtn');
  if (!target) return;
  const preserveExisting = !!(
    options && options.preserveExisting && target.innerHTML.trim()
    && !target.querySelector('.extensions-loading,.extensions-error')
  );
  if (copyButton && !preserveExisting) copyButton.disabled = true;
  const sequence = ++sidecarMonitorSequence;
  if (!preserveExisting) {
    target.innerHTML = '<div class="extensions-loading">Loading extension diagnostics…</div>';
  }

  try {
    const status = await api('/api/extensions/status');
    if (sequence !== sidecarMonitorSequence) return;
    renderStatus(status, sequence);
  } catch (error) {
    if (sequence !== sidecarMonitorSequence) return;
    if (preserveExisting && target.innerHTML.trim()) return;
    currentStatus = null;
    if (copyButton) copyButton.disabled = true;
    target.innerHTML = '<div class="extensions-error">Failed to load extension diagnostics: '
      + esc(error && error.message ? error.message : String(error)) + '</div>';
  }

  if (activeTab === 'gallery' && !galleryLoaded) loadExtensionsGallery();
}

export function switchExtensionsTab(tab) {
  activeTab = tab;
  document.querySelectorAll('[data-extensions-tab]').forEach(button => {
    button.classList.toggle('extensions-tab-active', button.dataset.extensionsTab === tab);
  });
  document.querySelectorAll('[data-extensions-pane]').forEach(pane => {
    pane.hidden = pane.dataset.extensionsPane !== tab;
  });
  if (tab === 'diagnostics') loadExtensionsPanel({ preserveExisting: true });
  if (tab === 'gallery' && !galleryLoaded) loadExtensionsGallery();
}

export async function copyExtensionsDiagnostics() {
  if (!currentStatus) return;
  const text = JSON.stringify(currentStatus, null, 2);
  const success = () => showToast(t('copied') || 'Copied!');
  const failure = () => showToast(t('copy_failed') || 'Copy failed');
  if (typeof _copyText === 'function') {
    _copyText(text).then(success).catch(failure);
    return;
  }
  if (typeof navigator !== 'undefined' && navigator && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(success).catch(failure);
  } else {
    failure();
  }
}
