import { bindExtensionSettings, primeExtensionSettings } from './configuration.js';
import { installedExtensionsMarkup } from './installed.js';
import { bindInstalledExtensionActions } from './installations.js';
import {
  bindExtensionSidecarActions,
  extensionSidecarsMarkup,
  monitorExtensionSidecars,
} from './sidecars.js';

/** Projection of the sanitized extension status payload into diagnostics UI. */

function statusLabel(value) {
  return value ? 'Enabled' : 'Disabled';
}

function booleanBadge(value) {
  const cssClass = value ? 'extension-status-badge-on' : 'extension-status-badge-off';
  return `<span class="extension-status-badge ${cssClass}">${value ? 'true' : 'false'}</span>`;
}

function assetList(urls) {
  if (!Array.isArray(urls) || !urls.length) {
    return '<div class="extension-url-empty">None</div>';
  }
  return '<ul class="extension-url-list">'
    + urls.map(url => `<li><code>${esc(url)}</code></li>`).join('')
    + '</ul>';
}

function warningList(warnings) {
  if (!Array.isArray(warnings) || !warnings.length) {
    return '<div class="extension-url-empty">No warnings.</div>';
  }
  return '<ul class="extension-warning-list">' + warnings.map(item => {
    const rawCode = item && item.code || 'unknown_warning';
    const source = item && item.source || 'unknown';
    const hint = rawCode === 'extension_state_unknown_ids'
      ? '<span>Some saved disabled-extension overrides no longer match the current manifest; re-added extensions with the same id may stay disabled.</span>'
      : '';
    return `<li><code>${esc(rawCode)}</code><span>${esc(source)}</span>${hint}</li>`;
  }).join('') + '</ul>';
}

function countValue(counts, key, fallback) {
  if (counts && Number.isFinite(Number(counts[key]))) return Number(counts[key]);
  return Array.isArray(fallback) ? fallback.length : 0;
}

export function renderExtensionsDiagnostics(status, lifecycle) {
  const target = $('extensionsDiagnostics');
  const copyButton = $('extensionsCopyDiagnosticsBtn');
  if (!target) return;
  primeExtensionSettings(status);
  if (copyButton) copyButton.disabled = !status;

  const manifest = status && status.manifest || {};
  const counts = status && status.counts || {};
  const scripts = Array.isArray(status && status.script_urls) ? status.script_urls : [];
  const stylesheets = Array.isArray(status && status.stylesheet_urls) ? status.stylesheet_urls : [];
  const sidecars = Array.isArray(status && status.sidecars) ? status.sidecars : [];
  const extensions = Array.isArray(status && status.extensions) ? status.extensions : [];
  const enabled = !!(status && status.enabled);

  target.innerHTML = `<div class="provider-card extension-status-card ${enabled ? 'extension-card-enabled' : 'extension-card-disabled'}">
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">Extension runtime</div>
        <div class="provider-card-meta">Status from /api/extensions/status; toggles persist a local override for installed manifest entries.</div>
      </div>
      <span class="provider-card-badge ${enabled ? '' : 'plugin-card-badge-disabled'}">${statusLabel(enabled)}</span>
    </div>
    <div class="provider-card-body extension-card-body">
      <div class="extension-summary-grid">
        <div><span>Extension dir configured</span>${booleanBadge(!!(status && status.extension_dir_configured))}</div>
        <div><span>Extension dir valid</span>${booleanBadge(!!(status && status.extension_dir_valid))}</div>
        <div><span>Manifest configured</span>${booleanBadge(!!manifest.configured)}</div>
        <div><span>Manifest loaded</span>${booleanBadge(!!manifest.loaded)}</div>
        <div><span>Manifest status</span><code>${esc(manifest.status || 'unknown')}</code></div>
        <div><span>Manifest entries inspected</span><code>${Number(manifest.entry_count) || 0}</code></div>
        <div><span>Manifest script count</span><code>${Number(manifest.script_count) || 0}</code></div>
        <div><span>Manifest stylesheet count</span><code>${Number(manifest.stylesheet_count) || 0}</code></div>
        <div><span>Manifest sidecar count</span><code>${Number(manifest.sidecar_count) || 0}</code></div>
        <div><span>Final script count</span><code>${countValue(counts, 'script_urls', scripts)}</code></div>
        <div><span>Final stylesheet count</span><code>${countValue(counts, 'stylesheet_urls', stylesheets)}</code></div>
        <div><span>Loopback sidecar count</span><code>${countValue(counts, 'sidecars', sidecars)}</code></div>
        <div><span>Installed manifest extensions</span><code>${countValue(counts, 'manifest_extensions', extensions)}</code></div>
        <div><span>User-disabled extensions</span><code>${countValue(counts, 'user_disabled', [])}</code></div>
      </div>
    </div>
  </div>
  <div class="provider-card extension-installed-card">
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">Installed manifest extensions</div>
        <div class="provider-card-meta">Enable or disable already-present local extensions. Reload WebUI to apply injected asset changes to this browser tab.</div>
      </div>
    </div>
    <div class="provider-card-body extension-card-body">
      ${installedExtensionsMarkup(extensions, !!(status && status.extension_dir_configured))}
    </div>
  </div>
  <div class="provider-card extension-assets-card">
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">Final public asset URLs</div>
        <div class="provider-card-meta">Same-origin URLs that may be injected into the app shell.</div>
      </div>
    </div>
    <div class="provider-card-body extension-card-body">
      <div class="provider-card-label">Scripts</div>
      ${assetList(scripts)}
      <div class="provider-card-label extension-section-label">Stylesheets</div>
      ${assetList(stylesheets)}
    </div>
  </div>
  ${extensionSidecarsMarkup(sidecars)}
  <div class="provider-card extension-warnings-card">
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">Sanitized warnings</div>
        <div class="provider-card-meta">Codes and coarse sources only; paths and rejected values are not shown.</div>
      </div>
    </div>
    <div class="provider-card-body extension-card-body">${warningList(status && status.warnings)}</div>
  </div>`;

  bindInstalledExtensionActions(target, lifecycle.onStatusUpdate);
  bindExtensionSidecarActions(target, lifecycle.onStatusUpdate);
  bindExtensionSettings(target);
  monitorExtensionSidecars(sidecars, lifecycle.sequence, lifecycle.isCurrentSequence);
}
