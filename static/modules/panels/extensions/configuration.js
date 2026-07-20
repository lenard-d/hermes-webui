/** Browser-local settings owned by an installed extension. */

export function primeExtensionSettings(status) {
  if (!window.HermesExtensionSettings || !status || !Array.isArray(status.extensions)) return;
  window.HermesExtensionSettings.primeFromStatus({ extensions: status.extensions });
}

function settingFieldMarkup(field, value) {
  const key = String(field && field.key || '');
  const type = String(field && field.type || '');
  const label = String(field && field.label || key);
  const description = String(field && field.description || '');
  const dataAttributes = `data-extension-setting-input="${esc(key)}" data-extension-setting-type="${esc(type)}"`;
  let control = '';

  if (type === 'boolean') {
    control = `<label class="extension-setting-check"><input type="checkbox" ${dataAttributes}${value ? ' checked' : ''}> <span>${esc(label)}</span></label>`;
  } else if (type === 'number' || type === 'integer') {
    const step = type === 'integer' ? '1' : 'any';
    control = `<label><span>${esc(label)}</span><input type="number" step="${step}" ${dataAttributes} value="${esc(String(value ?? ''))}"></label>`;
  } else if (type === 'enum') {
    const options = Array.isArray(field.options) ? field.options : [];
    control = `<label><span>${esc(label)}</span><select ${dataAttributes}>${options.map(option => {
      const optionValue = String(option && option.value || '');
      const optionLabel = String(option && option.label || optionValue);
      return `<option value="${esc(optionValue)}"${optionValue === value ? ' selected' : ''}>${esc(optionLabel)}</option>`;
    }).join('')}</select></label>`;
  } else {
    control = `<label><span>${esc(label)}</span><input type="text" ${dataAttributes} value="${esc(String(value ?? ''))}"></label>`;
  }

  return `<div class="extension-setting-field">${control}${description ? `<div class="extension-setting-desc">${esc(description)}</div>` : ''}</div>`;
}

export function extensionSettingsMarkup(entry) {
  const id = entry && entry.id || '';
  if (!(entry && entry.storage_owned)) {
    return '<div class="extension-settings-empty">No extension-owned browser storage permission.</div>';
  }

  const settings = window.HermesExtensionSettings && id
    ? window.HermesExtensionSettings.settingsForExtension(id)
    : null;
  if (!settings || !settings.trusted) {
    return '<div class="extension-settings-empty">Reload WebUI after enabling or installing this extension to edit browser-local settings.</div>';
  }

  const schema = Array.isArray(settings.schema) ? settings.schema : [];
  const values = settings.values;
  const fields = schema.length
    ? schema.map(field => settingFieldMarkup(field, values[field.key])).join('')
    : '<div class="extension-settings-empty">No configurable settings declared.</div>';
  const disabled = schema.length ? '' : ' disabled aria-disabled="true"';

  return `<div class="extension-settings-box">
    <div class="extension-settings-head">
      <div>
        <div class="extension-settings-title">Browser-local extension settings</div>
        <div class="extension-settings-note">Settings and extension-owned storage stay in this browser. Do not store secrets here.</div>
      </div>
    </div>
    <div class="extension-settings-fields">${fields}</div>
    <div class="extension-settings-actions">
      <button class="sm-btn" type="button" data-extension-settings-save="${esc(id)}"${disabled}>Save settings</button>
      <button class="sm-btn" type="button" data-extension-settings-reset="${esc(id)}"${disabled}>Reset settings</button>
      <button class="sm-btn" type="button" data-extension-storage-clear="${esc(id)}">Clear extension storage</button>
    </div>
  </div>`;
}

function readSettingsForm(row) {
  const values = {};
  row.querySelectorAll('[data-extension-setting-input]').forEach(input => {
    const key = input.dataset.extensionSettingInput || '';
    const type = input.dataset.extensionSettingType || '';
    if (!key) return;
    if (type === 'boolean') values[key] = !!input.checked;
    else if (type === 'integer') values[key] = Number.parseInt(input.value, 10);
    else if (type === 'number') values[key] = Number.parseFloat(input.value);
    else values[key] = input.value;
  });
  return values;
}

function fillSettingsForm(row, id) {
  if (!window.HermesExtensionSettings) return;
  const values = window.HermesExtensionSettings.settingsForExtension(id).values;
  row.querySelectorAll('[data-extension-setting-input]').forEach(input => {
    const key = input.dataset.extensionSettingInput || '';
    const type = input.dataset.extensionSettingType || '';
    const value = values[key];
    if (type === 'boolean') input.checked = !!value;
    else input.value = value ?? '';
  });
}

function saveExtensionSettings(button) {
  const id = button && button.dataset.extensionSettingsSave;
  const row = button && button.closest('[data-extension-id]');
  if (!id || !row || !window.HermesExtensionSettings) return;
  const result = window.HermesExtensionSettings.settingsForExtension(id).setAll(readSettingsForm(row));
  if (!result.ok) {
    showToast('Extension settings contain invalid values.');
    return;
  }
  fillSettingsForm(row, id);
  showToast('Extension settings saved in this browser.');
}

function resetExtensionSettings(button) {
  const id = button && button.dataset.extensionSettingsReset;
  const row = button && button.closest('[data-extension-id]');
  if (!id || !row || !window.HermesExtensionSettings) return;
  window.HermesExtensionSettings.settingsForExtension(id).reset();
  fillSettingsForm(row, id);
  showToast('Extension settings reset in this browser.');
}

function clearExtensionStorage(button) {
  const id = button && button.dataset.extensionStorageClear;
  if (!id || !window.HermesExtensionSettings) return;
  window.HermesExtensionSettings.storageForExtension(id).clear();
  showToast('Extension storage cleared in this browser.');
}

export function bindExtensionSettings(root) {
  if (!root) return;
  root.querySelectorAll('[data-extension-settings-save]').forEach(button => {
    button.addEventListener('click', () => saveExtensionSettings(button));
  });
  root.querySelectorAll('[data-extension-settings-reset]').forEach(button => {
    button.addEventListener('click', () => resetExtensionSettings(button));
  });
  root.querySelectorAll('[data-extension-storage-clear]').forEach(button => {
    button.addEventListener('click', () => clearExtensionStorage(button));
  });
}
