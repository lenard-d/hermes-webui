import { MAIN_VIEW_PANELS } from './core.js';
import { state } from './state.js';

/** Settings panel for installed Hermes plugins and their isolated pages. */

let currentPluginPage = null;

export async function handlePluginEnableToggle(pluginKey, checked) {
  try {
    const body = { dashboard_plugins: { [pluginKey]: !!checked } };
    await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    loadPluginsPanel();
  } catch (error) {
    showToast(t('settings_save_failed') + error.message);
  }
}

function _pluginActivationState(plugin) {
  const activation = plugin && typeof plugin.activation === 'string'
    ? plugin.activation
    : (plugin && plugin.enabled === false ? 'disabled' : 'enabled');
  if (plugin&&plugin.is_active_provider===true) return 'provider';
  if (activation==='exclusive' || activation==='provider') {
    return plugin && plugin.is_active_provider === false ? 'disabled' : 'provider';
  }
  return activation === 'enabled' ? 'enabled' : 'disabled';
}

function _partitionPluginsActiveFirst(plugins) {
  const active = [];
  const inactive = [];
  for (const plugin of plugins) {
    if (_pluginActivationState(plugin) === 'disabled') inactive.push(plugin);
    else active.push(plugin);
  }
  return active.concat(inactive);
}

export async function loadPluginsPanel() {
  const list = $('pluginsList');
  const empty = $('pluginsEmpty');
  if (!list) return;
  try {
    const data = await api('/api/plugins');
    const plugins = Array.isArray(data && data.plugins) ? data.plugins : [];
    const tabButton = document.querySelector('[data-settings-section="plugins"]');
    if (tabButton) tabButton.style.display = data && data.empty ? 'none' : '';
    list.innerHTML = '';
    if (!plugins.length) {
      list.style.display = 'none';
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    list.style.display = '';
    for (const plugin of _partitionPluginsActiveFirst(plugins)) {
      list.appendChild(_buildPluginCard(plugin));
    }
  } catch (error) {
    list.innerHTML = '<div style="color:var(--error);padding:12px;font-size:13px">'
      + t('plugins_load_failed') + esc(error.message || String(error)) + '</div>';
  }
}

function _buildPluginCard(plugin) {
  const card = document.createElement('div');
  card.className = 'provider-card plugin-card';
  card.dataset.plugin = plugin && plugin.key || '';
  const activation = plugin && typeof plugin.activation === 'string'
    ? plugin.activation
    : (plugin && plugin.enabled === false ? 'disabled' : 'enabled');
  const isProvider=activation==='exclusive'||activation==='provider';
  const hooks = Array.isArray(plugin && plugin.hooks) ? plugin.hooks : [];
  const hookMarkup = hooks.length
    ? hooks.map(hook => `<span class="plugin-hook-badge">${esc(hook)}</span>`).join('')
    : '<span class="plugin-hook-empty">' + t(isProvider ? 'plugins_provider_no_hooks' : 'plugins_no_hooks') + '</span>';
  const version = plugin && plugin.version ? ' · v' + esc(plugin.version) : '';
  const description = plugin && plugin.description ? esc(plugin.description) : t('plugins_no_description');
  const enabled = plugin && plugin.enabled !== false;
  const tab = plugin && plugin.tab;
  const isDashboardPlugin = !!(tab && tab.path);
  const openButton = enabled&&tab&&tab.path
    ? `<a href="${esc(tab.path)}" class="plugin-open-btn">${esc(tab.label || plugin.name || 'Open')} \u2197</a>`
    : '';
  const toggleMarkup = isDashboardPlugin
    ? `<div class="plugin-card-footer-row">
         <span class="plugin-toggle-label">${t('plugins_enable_toggle') || (enabled ? 'Enabled' : 'Enable')}</span>
         <label class="plugin-toggle-switch">
           <input type="checkbox" class="plugin-enable-toggle"${enabled ? ' checked' : ''}>
           <span class="plugin-toggle-slider"></span>
         </label>
       </div>`
    : '';
  const activeProvider = plugin && typeof plugin.is_active_provider === 'boolean'
    ? plugin.is_active_provider
    : isProvider;
  let badgeText;
  let badgeClass;
  if (activeProvider) {
    badgeText = t('plugins_active_provider');
    badgeClass = 'plugin-card-badge-provider';
  } else if (activation === 'enabled') {
    badgeText = t('plugins_enabled');
    badgeClass = '';
  } else {
    badgeText = t('plugins_disabled');
    badgeClass = 'plugin-card-badge-disabled';
  }

  card.innerHTML = `<div class="provider-card-header plugin-card-header">
    <div class="provider-card-info">
      <div class="provider-card-name">${esc(plugin && plugin.name || t('plugins_unnamed'))}</div>
      <div class="provider-card-meta">${esc(plugin && plugin.key || 'plugin')}${version}</div>
    </div>
    <span class="provider-card-badge ${badgeClass}">${badgeText}</span>
  </div>
  <div class="provider-card-body plugin-card-body">
    <div class="provider-card-hint">${description}</div>
    <div class="provider-card-label">${t('plugins_registered_hooks')}</div>
    <div class="plugin-hook-list">${hookMarkup}</div>
    ${openButton ? `<div class="plugin-card-footer">${openButton}</div>` : ''}
    ${toggleMarkup}
  </div>`;

  if (tab && tab.path) {
    const openElement = card.querySelector('.plugin-open-btn');
    if (openElement) {
      const path = tab.path;
      const label = tab.label || plugin.name;
      openElement.addEventListener('click', event => switchPluginPage(event, path, label));
    }
  }
  if (isDashboardPlugin) {
    const toggle = card.querySelector('.plugin-enable-toggle');
    if (toggle) {
      const key = plugin.key;
      toggle.addEventListener('change', () => handlePluginEnableToggle(key, toggle.checked));
    }
  }
  return card;
}

// ── Plugin pages ─────────────────────────────────────────────────────────────

export async function switchPluginPage(event, path, label) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!currentPluginPage || currentPluginPage.path !== path) {
    await _loadPluginPage(path, label);
  }
  state._currentPanel = 'plugin';
  const main = document.querySelector('main.main');
  if (main) {
    MAIN_VIEW_PANELS.forEach(panel => {
      main.classList.toggle('showing-' + panel, panel === 'plugin');
    });
  }
}

async function _loadPluginPage(path, label) {
  const container = $('pluginPageContainer');
  const title = $('pluginPageTitle');
  if (!container) return;
  if (title) title.textContent = label || path;
  container.innerHTML = '';

  const iframe = document.createElement('iframe');
  iframe.src = path;
  iframe.style.cssText = 'width:100%;height:100%;border:none;display:block;';
  iframe.setAttribute('title', label || 'Plugin');
  iframe.setAttribute('sandbox', 'allow-scripts allow-forms allow-popups');
  container.appendChild(iframe);
  currentPluginPage = { path, label };
}

// ── Providers panel ──────────────────────────────────────────────────────────
