/** Read-only catalog projection for the curated extension registry. */

function safeHttpUrl(value) {
  if (!value) return '';
  const raw = String(value).trim();
  if (!/^https?:\/\//i.test(raw)) return '';
  try {
    const url = new URL(raw);
    if (url.username || url.password) return '';
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
  } catch (_error) {
    return '';
  }
}

function registrySourceUrl(entryPath) {
  const raw = String(entryPath || '').trim();
  if (!raw || raw.startsWith('/') || raw.includes('\\') || raw.includes('\0')) return '';
  const parts = raw.split('/').filter(Boolean);
  if (!parts.length || parts.some(part => part === '.' || part === '..')) return '';
  const folder = parts.length > 1 ? parts.slice(0, -1) : parts;
  return 'https://github.com/hermes-webui/hermes-webui-extensions/tree/main/'
    + folder.map(encodeURIComponent).join('/');
}

function sourceUrl(entry) {
  if (!entry || typeof entry !== 'object') return '';
  const candidates = [
    entry.homepage,
    entry.repository_url,
    entry.repo_url,
    entry.source_url,
    entry.source,
  ];
  if (typeof entry.repository === 'string') {
    candidates.push(entry.repository);
  } else if (entry.repository && typeof entry.repository === 'object') {
    candidates.push(entry.repository.url, entry.repository.html_url);
  }
  for (const candidate of candidates) {
    const safe = safeHttpUrl(candidate);
    if (safe) return safe;
  }
  return safeHttpUrl(registrySourceUrl(entry.entry_path || entry.runtime_manifest_path));
}

function sourceLink(entry) {
  const url = sourceUrl(entry);
  if (!url) return '';
  return `<a class="extension-gallery-source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Source</a>`;
}

function permissionList(value) {
  if (!Array.isArray(value)) return '';
  const items = value.map(item => String(item || '').trim()).filter(Boolean);
  return items.length ? items.join(', ') : '';
}

function permissionRows(permissions) {
  if (!permissions || typeof permissions !== 'object') return [];
  const rows = [];
  const webuiApi = permissions.webui_api && typeof permissions.webui_api === 'object'
    ? permissions.webui_api
    : {};
  const apiReads = permissionList(webuiApi.read);
  const apiWrites = permissionList(webuiApi.write);
  if (apiReads) rows.push(['WebUI API reads', apiReads]);
  if (apiWrites) rows.push(['WebUI API writes', apiWrites]);
  if (permissions.webui_navigation === true) rows.push(['Navigation', 'Can open or switch WebUI views']);

  const sidecarCommands = permissions.sidecar_commands && typeof permissions.sidecar_commands === 'object'
    ? permissions.sidecar_commands
    : {};
  const commandLabels = [
    ['from_loopback', 'accepts loopback commands'],
    ['can_switch_sessions', 'switch sessions'],
    ['can_write_drafts', 'write drafts'],
    ['can_autosend', 'auto-send drafts'],
    ['can_respond_approval', 'respond to approvals'],
    ['can_respond_clarify', 'respond to clarifications'],
  ];
  const commands = commandLabels
    .filter(([key]) => sidecarCommands[key] === true)
    .map(([, label]) => label);
  if (commands.length) rows.push(['Sidecar commands', commands.join(', ')]);

  const dom = permissions.dom && typeof permissions.dom === 'object' ? permissions.dom : {};
  const domItems = [];
  if (dom.owned === true) domItems.push('renders extension-owned UI');
  if (dom.mutates_core_views === true) domItems.push('can alter core WebUI views');
  if (domItems.length) rows.push(['DOM access', domItems.join(', ')]);

  const storage = permissions.storage && typeof permissions.storage === 'object' ? permissions.storage : {};
  const ownedStorage = permissionList(storage.owned || storage.owned_keys);
  const sharedStorage = permissionList(storage.shared_webui_keys);
  if (ownedStorage) rows.push(['Owned storage keys', ownedStorage]);
  if (sharedStorage) rows.push(['Shared WebUI storage', sharedStorage]);
  if (permissions.loopback_sidecar === true) rows.push(['Loopback sidecar', 'Can contact a declared local loopback helper']);
  if (permissions.native_host === true) rows.push(['Native host', 'Requires a local native host or desktop app']);

  const filesystem = permissions.filesystem && typeof permissions.filesystem === 'object'
    ? permissions.filesystem
    : {};
  if (filesystem.arbitrary === true) {
    rows.push(['Filesystem', 'Can access arbitrary filesystem paths']);
  } else if (filesystem.serves_bundled_assets === true) {
    rows.push(['Filesystem', 'Serves bundled extension assets only']);
  }
  if (permissions.network_external === true || permissions.external_network === true) {
    rows.push(['External network', 'Can contact external network origins']);
  }
  return rows;
}

function permissionSummary(permissions) {
  const rows = permissionRows(permissions);
  const body = rows.length
    ? '<div class="extension-gallery-permission-list">' + rows.map(([label, value]) => `
      <div class="extension-gallery-permission-row">
        <span class="extension-gallery-permission-label">${esc(label)}</span>
        <span class="extension-gallery-permission-value">${esc(value)}</span>
      </div>`).join('') + '</div>'
    : `<div class="extension-gallery-permission-empty">${esc(t('ext_gallery_permissions_empty'))}</div>`;
  return `<details class="extension-gallery-perms">
    <summary>${esc(t('ext_gallery_permissions_show'))}</summary>
    ${body}
  </details>`;
}

function postInstallNote(entry, isInstalled) {
  const lifecycle = entry && entry.lifecycle && typeof entry.lifecycle === 'object' ? entry.lifecycle : {};
  const postInstall = entry && entry.post_install && typeof entry.post_install === 'object'
    ? entry.post_install
    : null;
  const needsSidecar = !!lifecycle.sidecar_start_required;
  const needsNativeHost = !!lifecycle.native_host_start_required;
  const summary = postInstall && postInstall.summary
    ? String(postInstall.summary)
    : ((needsSidecar || needsNativeHost) ? t('ext_gallery_local_component_required') : '');
  if (!summary) return '';

  const docsUrl = safeHttpUrl(postInstall && postInstall.docs_url);
  const localAppLabel = postInstall && postInstall.local_app_label
    ? String(postInstall.local_app_label)
    : t('ext_gallery_local_app_label');
  const requirements = [];
  if (postInstall && postInstall.requires_local_app === true) {
    requirements.push(t('ext_gallery_required_suffix', localAppLabel));
  }
  if (needsSidecar) requirements.push(t('ext_gallery_sidecar_required'));
  if (needsNativeHost) requirements.push(t('ext_gallery_native_host_required'));
  const requirementMarkup = requirements.length
    ? '<div class="extension-gallery-next-chips">' + requirements.map(item => `<span>${esc(item)}</span>`).join('') + '</div>'
    : '';
  const docsMarkup = docsUrl
    ? `<a class="extension-gallery-next-link" href="${esc(docsUrl)}" target="_blank" rel="noopener noreferrer">${esc(t('ext_gallery_open_setup_guide'))}</a>`
    : '';

  return `<div class="extension-gallery-next-step">
    <div class="extension-gallery-next-label">${esc(t(isInstalled ? 'ext_gallery_next_step' : 'ext_gallery_after_install'))}</div>
    <div class="extension-gallery-next-summary">${esc(summary)}</div>
    ${requirementMarkup}
    ${docsMarkup}
  </div>`;
}

function installedIds(status) {
  const ids = new Set();
  if (status && status.gallery_installed) {
    Object.keys(status.gallery_installed).forEach(id => ids.add(id));
  }
  if (status && Array.isArray(status.extensions)) {
    status.extensions.forEach(entry => {
      if (entry && entry.id) ids.add(String(entry.id));
    });
  }
  return ids;
}

export function extensionCatalogMarkup(entries, status) {
  if (!Array.isArray(entries) || !entries.length) {
    return '<div class="extensions-empty">No extensions found in the registry.</div>';
  }
  const installed = installedIds(status);
  return entries.map(entry => {
    const rawId = String(entry.id || '');
    const id = esc(rawId);
    const name = esc(String(entry.name || rawId));
    const author = esc(String(entry.author || ''));
    const version = esc(String(entry.version || ''));
    const description = esc(String(entry.description || ''));
    const capabilities = Array.isArray(entry.capabilities) ? entry.capabilities : [];
    const isInstalled = installed.has(rawId);
    const badges = capabilities.map(capability => `<span class="extension-gallery-badge">${esc(String(capability))}</span>`).join('');
    const metadata = [];
    if (author) metadata.push('by ' + author);
    if (version) metadata.push('v' + version);
    const source = sourceLink(entry);
    const metadataMarkup = metadata.length || source
      ? `<div class="extension-gallery-meta">${metadata.length ? `<span>${metadata.join(' · ')}</span>` : ''}${source}</div>`
      : '';
    const action = isInstalled
      ? `<button class="extension-gallery-uninstall-btn" data-ext-uninstall-id="${id}" type="button" data-i18n="ext_gallery_uninstall">Uninstall</button>`
      : `<button class="extension-gallery-install-btn" data-ext-install-id="${id}" type="button" data-i18n="ext_gallery_install">Install</button>`;
    const installedBadge = isInstalled ? '<span class="extension-gallery-installed-badge">Installed</span>' : '';

    return `<div class="extension-gallery-card">
      <div class="extension-gallery-head">
        <div class="extension-gallery-info">
          <div class="extension-gallery-name">${name}${installedBadge}</div>
          ${metadataMarkup}
        </div>
      </div>
      <div class="extension-gallery-desc">${description}</div>
      ${badges ? `<div class="extension-gallery-badge-row">${badges}</div>` : ''}
      ${postInstallNote(entry, isInstalled)}
      ${entry.permissions ? permissionSummary(entry.permissions) : ''}
      <div class="extension-gallery-actions">${action}</div>
    </div>`;
  }).join('');
}
