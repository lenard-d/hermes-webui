import { extensionSettingsMarkup } from './configuration.js';

/** Presentation for extensions already present in the configured bundle. */

function entryStatusLabel(entry) {
  const status = entry && entry.status || '';
  if (status === 'manifest_disabled') return 'Disabled in manifest';
  if (status === 'user_disabled') return 'Disabled';
  if (status === 'enabled') return 'Enabled';
  return 'Unknown';
}

function entryStatusBadge(entry) {
  const enabled = !!(entry && entry.effective_enabled);
  const cssClass = enabled ? 'extension-status-badge-on' : 'extension-status-badge-off';
  return `<span class="extension-status-badge ${cssClass}">${esc(entryStatusLabel(entry))}</span>`;
}

export function installedExtensionsMarkup(extensions, extensionDirConfigured) {
  const entries = Array.isArray(extensions) ? extensions : [];
  if (!entries.length) {
    if (!extensionDirConfigured) {
      return '<div class="extension-url-empty">No extension directory is configured.</div>';
    }
    return '<div class="extension-url-empty">No manifest extensions are installed in the configured bundle.</div>';
  }

  return `<div class="extension-installed-list">${entries.map(entry => {
    const id = entry && entry.id || '';
    const name = entry && entry.name || id || 'Unnamed extension';
    const canToggle = !!(entry && entry.can_toggle);
    const userEnabled = !!(entry && entry.user_enabled);
    const disabled = canToggle ? '' : ' disabled aria-disabled="true"';
    const note = canToggle
      ? 'Toggles the WebUI-managed override for the next app load.'
      : 'Manifest-disabled entries cannot be enabled from WebUI.';

    return `<div class="extension-installed-row" data-extension-id="${esc(id)}">
      <div class="extension-installed-main">
        <div class="extension-installed-title-row">
          <div class="extension-installed-title">${esc(name)}</div>
          ${entryStatusBadge(entry)}
        </div>
        <div class="extension-installed-meta"><code>${esc(id)}</code><span>${esc(note)}</span></div>
        ${extensionSettingsMarkup(entry)}
      </div>
      <button class="sm-btn extension-toggle-btn" type="button" data-extension-toggle-id="${esc(id)}" data-extension-next-enabled="${userEnabled ? 'false' : 'true'}"${disabled}>${esc(userEnabled ? 'Disable' : 'Enable')}</button>
    </div>`;
  }).join('')}</div>`;
}
