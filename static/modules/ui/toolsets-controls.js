import { showToast } from './composer.js';
import { closeOtherComposerMenus, registerComposerMenu } from './composer-menu-registry.js';
import { $, S } from './state.js';


// ── Session toolsets chip (#493) ───────────────────────────────────────────
let _currentSessionToolsets = null; // null = active profile defaults, array = custom list
let _toolsetsCatalog = null;

function _applyToolsetsChip(toolsets) {
  _currentSessionToolsets = toolsets;
  const wrap = $('composerToolsetsWrap');
  const label = $('composerToolsetsLabel');
  const chip = $('composerToolsetsChip');
  if (!wrap || !label) return;
  // Visibility is controlled entirely by responsive CSS — the chip shows only
  // at wide composer-footer widths (>= 1100px container query). At narrower
  // widths the layout is too cramped (model + reasoning + profile + workspace
  // + context-ring + send) to add another chip. Cleared inline style so the
  // CSS @container query is the single source of truth. State is still
  // tracked so /api/session/toolsets continues to work for cron/scripted
  // callers regardless of UI visibility. (#1431)
  wrap.style.display = '';
  const hasCustom = Array.isArray(toolsets) && toolsets.length > 0;
  const isStaged = hasCustom
    && typeof S !== 'undefined'
    && S
    && !S.session
    && Array.isArray(S._pendingSessionToolsets);
  if (hasCustom) {
    const stagedSuffix = isStaged ? ' (staged)' : '';
    label.textContent = toolsets.join(', ') + stagedSuffix;
    chip.classList.add('has-custom');
    chip.title = t('session_toolsets') + ': ' + toolsets.join(', ') + stagedSuffix;
  } else {
    label.textContent = t('session_toolsets_profile_defaults');
    chip.classList.remove('has-custom');
    chip.title = t('session_toolsets') + ': ' + t('session_toolsets_profile_defaults');
  }
}

function _syncToolsetsChip() {
  if (typeof S === 'undefined' || !S || !S.session) {
    const stagedToolsets = (typeof S !== 'undefined' && S && Array.isArray(S._pendingSessionToolsets))
      ? S._pendingSessionToolsets
      : null;
    _applyToolsetsChip(stagedToolsets);
    return;
  }
  _applyToolsetsChip(S.session.enabled_toolsets || null);
}

function syncToolsetsChip() {
  _syncToolsetsChip();
}

function _normalizeToolsetsCatalog(payload) {
  const servers = payload && Array.isArray(payload.servers) ? payload.servers : [];
  const seen = new Set();
  const names = [];
  servers.forEach(function(server) {
    const name = String((server && server.name) || '').trim();
    if (!name || seen.has(name)) return;
    seen.add(name);
    names.push(name);
  });
  return names;
}

function _loadToolsetsCatalog() {
  if (Array.isArray(_toolsetsCatalog)) return Promise.resolve(_toolsetsCatalog);
  return api('/api/mcp/servers')
    .then(function(payload) {
      _toolsetsCatalog = _normalizeToolsetsCatalog(payload);
      return _toolsetsCatalog;
    })
    .catch(function() {
      _toolsetsCatalog = false;
      return [];
    });
}

function invalidateToolsetsCatalog(payload) {
  _toolsetsCatalog = payload && Array.isArray(payload.servers) ? _normalizeToolsetsCatalog(payload) : null;
}
if (typeof window !== 'undefined') window.invalidateToolsetsCatalog = invalidateToolsetsCatalog;

function _toolsetsInputList(input) {
  if (!input) return [];
  return input.value.split(',').map(s => s.trim()).filter(Boolean);
}

function _ensureToolsetsPresetSection() {
  const dd = $('composerToolsetsDropdown');
  if (!dd) return null;
  let section = $('toolsetsPresetSections');
  if (section) return section;
  section = document.createElement('div');
  section.id = 'toolsetsPresetSections';
  section.className = 'toolsets-preset-sections';
  const inputRow = dd.querySelector('.toolsets-dropdown-input-row');
  if (inputRow) dd.insertBefore(section, inputRow);
  else dd.appendChild(section);
  return section;
}

function _appendToolsetsLabel(section, text) {
  const label = document.createElement('div');
  label.className = 'toolsets-dropdown-desc';
  label.textContent = text;
  section.appendChild(label);
}

function _renderToolsetsPresetSections(opts) {
  const state = opts && opts.state;
  const input = opts && opts.input;
  const section = _ensureToolsetsPresetSection();
  if (!section || !state || !input) return;
  const selected = _toolsetsInputList(input);
  const selectedSet = new Set(selected);
  const hasCustom = selected.length > 0;
  state.textContent = hasCustom
    ? '🔧 ' + selected.join(', ')
    : '👤 ' + t('session_toolsets_profile_defaults');

  section.innerHTML = '';
  const defaultsBtn = document.createElement('button');
  defaultsBtn.type = 'button';
  defaultsBtn.id = 'toolsetsProfileDefaultsBtn';
  defaultsBtn.className = 'toolsets-action-btn toolsets-clear-btn';
  defaultsBtn.textContent = t('session_toolsets_use_profile_defaults');
  section.appendChild(defaultsBtn);

  _appendToolsetsLabel(section, t('session_toolsets_configured_servers'));
  if (_toolsetsCatalog === null) {
    _appendToolsetsLabel(section, t('session_toolsets_loading_servers'));
    return;
  }
  if (_toolsetsCatalog === false) {
    _appendToolsetsLabel(section, t('mcp_load_failed'));
    return;
  }
  if (!Array.isArray(_toolsetsCatalog) || !_toolsetsCatalog.length) {
    _appendToolsetsLabel(section, t('session_toolsets_no_configured_servers'));
    return;
  }
  _toolsetsCatalog.forEach(function(name) {
    const row = document.createElement('label');
    row.className = 'toolsets-server-option';
    row.style.display = 'flex';
    row.style.alignItems = 'center';
    row.style.gap = '6px';
    row.style.margin = '4px 0';
    row.style.fontSize = '12px';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'toolsets-server-checkbox';
    checkbox.value = name;
    checkbox.checked = selectedSet.has(name);
    row.appendChild(checkbox);
    row.appendChild(document.createTextNode(name));
    section.appendChild(row);
  });
}

function _populateToolsetsDropdown() {
  const desc = $('toolsetsDropdownDesc');
  const state = $('toolsetsDropdownState');
  const input = $('toolsetsInput');
  const applyBtn = $('toolsetsApplyBtn');
  const clearBtn = $('toolsetsClearBtn');
  if (!desc || !state || !input) return;
  desc.textContent = t('session_toolsets_desc');
  if (applyBtn) applyBtn.textContent = t('session_toolsets_apply');
  if (clearBtn) clearBtn.textContent = t('session_toolsets_clear');
  input.placeholder = t('session_toolsets_placeholder');
  // Escape key handler for toolsets input
  input.onkeydown = function(e) { if(e.key === 'Escape') closeToolsetsDropdown(); };
  input.oninput = function() { _renderToolsetsPresetSections({ state, input }); };
  const hasCustom = Array.isArray(_currentSessionToolsets) && _currentSessionToolsets.length > 0;
  if (hasCustom) {
    input.value = _currentSessionToolsets.join(', ');
  } else {
    input.value = '';
  }
  _renderToolsetsPresetSections({ state, input });
}

function _positionToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  const footer = document.querySelector('.composer-footer');
  if (!dd || !chip || !footer) return;
  // Defense: if the chip has been hidden by responsive CSS (e.g. resize across
  // 1100px container threshold while dropdown was open), don't try to anchor
  // to a zero-rect element — close the dropdown instead. (#1431)
  if (chip.offsetParent === null) { closeToolsetsDropdown(); return; }
  const chipRect = chip.getBoundingClientRect();
  const footerRect = footer.getBoundingClientRect();
  let left = chipRect.left - footerRect.left;
  const maxLeft = Math.max(0, footer.clientWidth - dd.offsetWidth);
  left = Math.max(0, Math.min(left, maxLeft));
  dd.style.left = left + 'px';
}

function toggleToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  if (!dd || !chip) return;
  // Don't open when the chip itself is hidden by responsive CSS (#1431).
  // offsetParent === null catches display:none on the element or any ancestor.
  if (chip.offsetParent === null) return;
  const open = dd.classList.contains('open');
  if (open) { closeToolsetsDropdown(); return; }
  if (typeof closeProfileDropdown === 'function') closeProfileDropdown();
  if (typeof closeWsDropdown === 'function') closeWsDropdown();
  closeOtherComposerMenus('toolsets');
  _syncToolsetsChip();
  _populateToolsetsDropdown();
  _loadToolsetsCatalog().then(function() {
    const stillOpen = dd && dd.classList.contains('open');
    if (stillOpen) {
      const state = $('toolsetsDropdownState');
      const input = $('toolsetsInput');
      _renderToolsetsPresetSections({ state, input });
    }
  });
  dd.classList.add('open');
  _positionToolsetsDropdown();
  chip.classList.add('active');
  // Focus the input after a tick so the layout has settled
  setTimeout(() => { const inp = $('toolsetsInput'); if (inp) inp.focus(); }, 50);
}

function closeToolsetsDropdown() {
  const dd = $('composerToolsetsDropdown');
  const chip = $('composerToolsetsChip');
  if (dd) dd.classList.remove('open');
  if (chip) chip.classList.remove('active');
}
registerComposerMenu('toolsets',closeToolsetsDropdown);

function _applySessionToolsets(toolsets) {
  if (typeof S === 'undefined' || !S) return;
  if (!S.session) {
    S._pendingSessionToolsets = toolsets;
    _applyToolsetsChip(toolsets);
    if (Array.isArray(toolsets) && toolsets.length) {
      showToast('🔧 ' + t('session_toolsets_applied') + ': ' + toolsets.join(', '));
    } else {
      showToast('🌍 ' + t('session_toolsets_cleared'));
    }
    return;
  }
  const sid = S.session.session_id;
  api('/api/session/toolsets', {
    method: 'POST',
    body: JSON.stringify({ session_id: sid, toolsets: toolsets })
  })
    .then(function(r) {
      if (r && r.ok) {
        S.session.enabled_toolsets = r.enabled_toolsets || null;
        _applyToolsetsChip(r.enabled_toolsets || null);
        if (r.enabled_toolsets && r.enabled_toolsets.length) {
          showToast('🔧 ' + t('session_toolsets_applied') + ': ' + r.enabled_toolsets.join(', '));
        } else {
          showToast('🌍 ' + t('session_toolsets_cleared'));
        }
      } else {
        showToast(t('session_toolsets_failed') + (r && r.error ? r.error : 'Unknown error'), 3000, 'error');
      }
    })
    .catch(function(err) {
      showToast(t('session_toolsets_failed') + (err.message || err), 3000, 'error');
    });
}

// Click-outside handler for toolsets dropdown
document.addEventListener('click', function(e) {
  if (
    !e.target.closest('#composerToolsetsChip') &&
    !e.target.closest('#composerToolsetsDropdown')
  ) closeToolsetsDropdown();
  // Active profile defaults button
  if (e.target.closest('#toolsetsProfileDefaultsBtn')) {
    _applySessionToolsets(null);
    closeToolsetsDropdown();
    return;
  }
  // Apply button
  if (e.target.closest('#toolsetsApplyBtn')) {
    const input = $('toolsetsInput');
    if (!input) return;
    const raw = input.value.trim();
    if (!raw) {
      showToast(t('session_toolsets_desc'), 2000);
      return;
    }
    const toolsets = raw.split(',').map(s => s.trim()).filter(Boolean);
    if (toolsets.length === 0) {
      showToast(t('session_toolsets_desc'), 2000);
      return;
    }
    _applySessionToolsets(toolsets);
    closeToolsetsDropdown();
  }
  // Clear button
  if (e.target.closest('#toolsetsClearBtn')) {
    _applySessionToolsets(null);
    closeToolsetsDropdown();
  }
});

document.addEventListener('change', function(e) {
  if (!e.target.closest('#toolsetsPresetSections')) return;
  if (!e.target.classList.contains('toolsets-server-checkbox')) return;
  const input = $('toolsetsInput');
  const state = $('toolsetsDropdownState');
  if (!input) return;
  const checked = Array.from(document.querySelectorAll('#toolsetsPresetSections .toolsets-server-checkbox:checked'))
    .map(el => String(el.value || '').trim())
    .filter(Boolean);
  const catalogSet = new Set(Array.isArray(_toolsetsCatalog) ? _toolsetsCatalog : []);
  const manual = _toolsetsInputList(input).filter(name => !catalogSet.has(name));
  input.value = checked.concat(manual).join(', ');
  _renderToolsetsPresetSections({ state, input });
});

// Position toolsets dropdown on resize, OR close it if the chip is no longer
// visible (e.g. resize crossed the 1100px container threshold while dropdown
// was open — the wrap is hidden by CSS but the dropdown sibling stays open
// without an anchor). (#1431)
window.addEventListener('resize', () => {
  const dd = $('composerToolsetsDropdown');
  if (!dd || !dd.classList.contains('open')) return;
  const chip = $('composerToolsetsChip');
  if (!chip || chip.offsetParent === null) { closeToolsetsDropdown(); return; }
  _positionToolsetsDropdown();
});

export {
  _applyToolsetsChip,
  _syncToolsetsChip,
  syncToolsetsChip,
  _normalizeToolsetsCatalog,
  _loadToolsetsCatalog,
  invalidateToolsetsCatalog,
  _toolsetsInputList,
  _ensureToolsetsPresetSection,
  _appendToolsetsLabel,
  _renderToolsetsPresetSections,
  _populateToolsetsDropdown,
  _positionToolsetsDropdown,
  toggleToolsetsDropdown,
  closeToolsetsDropdown,
  _applySessionToolsets,
  _currentSessionToolsets,
  _toolsetsCatalog,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _applyToolsetsChip: { enumerable: true, get: () => _applyToolsetsChip, set: (value) => { _applyToolsetsChip = value; } },
  _syncToolsetsChip: { enumerable: true, get: () => _syncToolsetsChip, set: (value) => { _syncToolsetsChip = value; } },
  syncToolsetsChip: { enumerable: true, get: () => syncToolsetsChip, set: (value) => { syncToolsetsChip = value; } },
  _normalizeToolsetsCatalog: { enumerable: true, get: () => _normalizeToolsetsCatalog, set: (value) => { _normalizeToolsetsCatalog = value; } },
  _loadToolsetsCatalog: { enumerable: true, get: () => _loadToolsetsCatalog, set: (value) => { _loadToolsetsCatalog = value; } },
  invalidateToolsetsCatalog: { enumerable: true, get: () => invalidateToolsetsCatalog, set: (value) => { invalidateToolsetsCatalog = value; } },
  _toolsetsInputList: { enumerable: true, get: () => _toolsetsInputList, set: (value) => { _toolsetsInputList = value; } },
  _ensureToolsetsPresetSection: { enumerable: true, get: () => _ensureToolsetsPresetSection, set: (value) => { _ensureToolsetsPresetSection = value; } },
  _appendToolsetsLabel: { enumerable: true, get: () => _appendToolsetsLabel, set: (value) => { _appendToolsetsLabel = value; } },
  _renderToolsetsPresetSections: { enumerable: true, get: () => _renderToolsetsPresetSections, set: (value) => { _renderToolsetsPresetSections = value; } },
  _populateToolsetsDropdown: { enumerable: true, get: () => _populateToolsetsDropdown, set: (value) => { _populateToolsetsDropdown = value; } },
  _positionToolsetsDropdown: { enumerable: true, get: () => _positionToolsetsDropdown, set: (value) => { _positionToolsetsDropdown = value; } },
  toggleToolsetsDropdown: { enumerable: true, get: () => toggleToolsetsDropdown, set: (value) => { toggleToolsetsDropdown = value; } },
  closeToolsetsDropdown: { enumerable: true, get: () => closeToolsetsDropdown, set: (value) => { closeToolsetsDropdown = value; } },
  _applySessionToolsets: { enumerable: true, get: () => _applySessionToolsets, set: (value) => { _applySessionToolsets = value; } },
  _currentSessionToolsets: { enumerable: true, get: () => _currentSessionToolsets, set: (value) => { _currentSessionToolsets = value; } },
  _toolsetsCatalog: { enumerable: true, get: () => _toolsetsCatalog, set: (value) => { _toolsetsCatalog = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
