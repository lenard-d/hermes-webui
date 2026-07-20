/** Loopback sidecar projection, consent mutation, and browser health lifecycle. */

function healthBadge(status, label) {
  const safeStatus = ['checking', 'healthy', 'unhealthy', 'blocked'].includes(status)
    ? status
    : 'checking';
  return `<span class="extension-sidecar-status-badge extension-sidecar-status-${safeStatus}">${esc(label || safeStatus)}</span>`;
}

function runtimeStatus(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return ['running', 'connected', 'waiting', 'stale', 'unloaded', 'stopped', 'not_registered', 'unknown'].includes(normalized)
    ? normalized
    : 'unknown';
}

function runtimeStatusLabel(value) {
  const normalized = runtimeStatus(value);
  return normalized === 'not_registered' ? 'not registered' : normalized.replace(/_/g, ' ');
}

function runtimeLastSeen(value) {
  const text = String(value ?? '').trim();
  if (!/^\d+(?:\.\d+)?$/.test(text)) return '';
  const raw = Number(text);
  if (!Number.isFinite(raw) || raw <= 0) return '';
  const seconds = raw > 1000000000000 ? raw / 1000 : raw;
  const now = Math.floor(Date.now() / 1000);
  if (seconds > now + 300) return '';
  const age = Math.max(0, Math.floor(now - seconds));
  if (age < 5) return 'just now';
  if (age < 60) return `${age}s ago`;
  const minutes = Math.floor(age / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function runtimeOrigin(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  try {
    const parsed = new URL(text);
    if (parsed.protocol === 'http:' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost')) {
      return parsed.origin;
    }
  } catch (_error) {}
  return '';
}

function runtimeRows(runtime) {
  if (!runtime || typeof runtime !== 'object') return [];
  const rows = [];
  if (Object.prototype.hasOwnProperty.call(runtime, 'sidecar')) {
    rows.push(['Sidecar', runtimeStatusLabel(runtime.sidecar)]);
  }
  if (Object.prototype.hasOwnProperty.call(runtime, 'native_host')) {
    rows.push(['Native host', runtimeStatusLabel(runtime.native_host)]);
  }
  if (Object.prototype.hasOwnProperty.call(runtime, 'bridge')) {
    rows.push(['Bridge', runtimeStatusLabel(runtime.bridge)]);
  }
  const lastSeen = runtimeLastSeen(runtime.last_seen_at);
  if (lastSeen) rows.push(['Last update', lastSeen]);
  const origin = runtimeOrigin(runtime.webui_origin);
  if (origin) rows.push(['WebUI origin', origin]);
  return rows;
}

function runtimeDetails(runtime) {
  return runtimeRows(runtime)
    .map(([label, value]) => `<div><span>${esc(label)}</span><code>${esc(value)}</code></div>`)
    .join('');
}

export function extensionSidecarsMarkup(sidecars) {
  const entries = Array.isArray(sidecars) ? sidecars : [];
  const body = entries.length
    ? `<div class="extension-sidecar-list">${entries.map((sidecar, index) => {
      const id = sidecar && sidecar.id || '';
      const name = sidecar && sidecar.name || '';
      const title = name || id || 'Unnamed extension';
      const metadata = name && id ? id : sidecar && sidecar.type || 'loopback';
      const origin = sidecar && sidecar.origin || '';
      const healthPath = sidecar && sidecar.health_path || '';
      const healthUrl = sidecar && sidecar.health_url || '';
      const proxy = sidecar && sidecar.proxy && typeof sidecar.proxy === 'object' ? sidecar.proxy : {};
      const proxyAvailable = proxy.available === true;
      const proxyConsented = proxy.consented === true;
      const proxyStatus = proxyConsented
        ? 'consented'
        : (proxy.origin_changed === true
          ? 'reconfirm required'
          : (proxy.consent_required === true ? 'approval required' : 'unavailable'));
      const proxyButton = proxyAvailable && id
        ? `<button class="sm-btn extension-toggle-btn" type="button" data-extension-sidecar-proxy-id="${esc(id)}" data-extension-sidecar-proxy-approved="${proxyConsented ? 'false' : 'true'}">${esc(proxyConsented ? 'Revoke proxy consent' : 'Approve proxy consent')}</button>`
        : '';

      return `<div class="extension-sidecar-row" data-sidecar-index="${index}">
        <div class="extension-sidecar-row-head">
          <div class="extension-sidecar-title">${esc(title)}</div>
          <span id="extensionSidecarHealth${index}" data-sidecar-health-index="${index}">${healthBadge('checking', 'checking')}</span>
        </div>
        <div class="extension-sidecar-meta">${esc(metadata)}</div>
        <div class="extension-sidecar-fields">
          <div><span>Origin</span><code>${esc(origin)}</code></div>
          <div><span>Health path</span><code>${esc(healthPath)}</code></div>
          <div><span>Health URL</span><code>${esc(healthUrl)}</code></div>
          <div><span>Proxy</span><code>${esc(proxyStatus)}</code></div>
          <div><span>Proxy path</span><code>${esc(proxy.path || '')}</code></div>
        </div>
        <div class="extension-sidecar-actions">${proxyButton}</div>
        <div class="extension-sidecar-runtime" data-sidecar-runtime-index="${index}" hidden></div>
      </div>`;
    }).join('')}</div>`
    : '<div class="extension-url-empty">No loopback sidecars declared.</div>';

  return `<div class="provider-card extension-sidecars-card">
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">Loopback sidecars</div>
        <div class="provider-card-meta">Declared local companions; health is checked directly from this browser with WebUI credentials omitted.</div>
      </div>
    </div>
    <div class="provider-card-body extension-card-body">${body}</div>
  </div>`;
}

function setHealth(index, status, label) {
  const element = document.querySelector(`[data-sidecar-health-index="${index}"]`);
  if (element) element.innerHTML = healthBadge(status, label);
}

function setRuntime(index, runtime) {
  const element = document.querySelector(`[data-sidecar-runtime-index="${index}"]`);
  if (!element) return;
  const details = runtimeDetails(runtime);
  element.hidden = !details;
  element.innerHTML = details;
}

async function checkHealth(sidecar, index, sequence, isCurrentSequence) {
  const healthUrl = sidecar && sidecar.health_url;
  if (!healthUrl) {
    setHealth(index, 'blocked', 'unreachable / blocked');
    setRuntime(index, null);
    return;
  }

  let controller = null;
  let timeoutId = null;
  try {
    if (typeof AbortController !== 'undefined') {
      controller = new AbortController();
      timeoutId = setTimeout(() => controller.abort(), 2500);
    }
    const response = await fetch(healthUrl, {
      credentials: 'omit',
      cache: 'no-store',
      signal: controller ? controller.signal : undefined,
    });
    if (!isCurrentSequence(sequence)) return;
    if (!response.ok) {
      setHealth(index, 'unhealthy', 'unhealthy');
      setRuntime(index, null);
      return;
    }

    setHealth(index, 'healthy', 'healthy');
    let body = null;
    try {
      body = await response.json();
    } catch (_error) {}
    if (!isCurrentSequence(sequence)) return;
    setRuntime(index, body && typeof body === 'object' ? body.runtime : null);
  } catch (_error) {
    if (!isCurrentSequence(sequence)) return;
    setHealth(index, 'blocked', 'unreachable / blocked');
    setRuntime(index, null);
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
}

export function monitorExtensionSidecars(sidecars, sequence, isCurrentSequence) {
  if (!Array.isArray(sidecars) || !sidecars.length) return;
  sidecars.forEach((sidecar, index) => checkHealth(sidecar, index, sequence, isCurrentSequence));
}

async function updateProxyConsent(button, onStatusUpdate) {
  if (!button || button.disabled) return;
  const id = button.dataset.extensionSidecarProxyId || '';
  const approved = button.dataset.extensionSidecarProxyApproved === 'true';
  if (!id) return;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = approved ? 'Approving…' : 'Revoking…';
  try {
    const status = await api('/api/extensions/sidecar-proxy-consent', {
      method: 'POST',
      body: JSON.stringify({ id, approved }),
    });
    showToast(approved ? 'Extension sidecar proxy approved.' : 'Extension sidecar proxy consent revoked.');
    onStatusUpdate(status);
  } catch (error) {
    button.disabled = false;
    button.textContent = previousText;
    showToast('Failed to update extension sidecar proxy consent: '
      + (error && error.message ? error.message : String(error)));
  }
}

export function bindExtensionSidecarActions(root, onStatusUpdate) {
  if (!root) return;
  root.querySelectorAll('[data-extension-sidecar-proxy-id]').forEach(button => {
    button.addEventListener('click', () => updateProxyConsent(button, onStatusUpdate));
  });
}
