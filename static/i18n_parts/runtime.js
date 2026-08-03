var SUPPORTED_LOCALES = Object.freeze([
  { code: 'en', label: 'English', asset: 'en' },
  { code: 'it', label: 'Italiano', asset: 'it' },
  { code: 'ja', label: '日本語', asset: 'ja' },
  { code: 'ru', label: 'Русский', asset: 'ru' },
  { code: 'es', label: 'Español', asset: 'es' },
  { code: 'de', label: 'Deutsch', asset: 'de' },
  { code: 'zh', label: '简体中文', asset: 'zh' },
  { code: 'zh-Hant', label: '繁體中文', asset: 'zh_hant' },
  { code: 'pt', label: 'Português', asset: 'pt' },
  { code: 'ko', label: '한국어', asset: 'ko' },
  { code: 'fr', label: 'Français', asset: 'fr' },
  { code: 'cs', label: 'Čeština', asset: 'cs' },
  { code: 'tr', label: 'Türkçe', asset: 'tr' },
  { code: 'pl', label: 'Polski', asset: 'pl' },
  { code: 'vi', label: 'Tiếng Việt', asset: 'vi' },
]);
const _SUPPORTED_LOCALE_SLUGS = Object.freeze(Object.fromEntries(
  SUPPORTED_LOCALES.map(({code, asset}) => [code, asset])
));
const _localeLoadPromises = new Map();
const _localeRuntimeAssetUrl = (() => {
  try {
    return document.currentScript?.src || '';
  } catch (_) {
    return '';
  }
})();

// Active locale — English is the only eager bundle; loadLocale() requests any
// saved non-English bundle without putting every translation on the cold path.
let _locale = LOCALES.en;
let _requestedLocale = 'en';

/**
 * Resolve an incoming locale tag to a known LOCALES key.
 * Supports exact keys, case-insensitive matches, and a few common aliases
 * (e.g. zh-CN -> zh, zh-TW -> zh-Hant). Returns null when unresolved.
 * @param {string} lang
 * @returns {string|null}
 */
function resolveLocale(lang) {
  if (typeof lang !== 'string') return null;
  const raw = lang.trim();
  if (!raw) return null;
  if (_SUPPORTED_LOCALE_SLUGS[raw]) return raw;

  const lower = raw.toLowerCase().replace(/_/g, '-');

  // Case-insensitive direct match first.
  const supportedLocales = Object.keys(_SUPPORTED_LOCALE_SLUGS);
  const direct = supportedLocales.find((k) => k.toLowerCase() === lower);
  if (direct) return direct;

  // Common Chinese variants.
  if (lower === 'zh' || lower.startsWith('zh-cn') || lower.startsWith('zh-sg') || lower.startsWith('zh-hans')) {
    return 'zh';
  }
  if (lower.startsWith('zh-tw') || lower.startsWith('zh-hk') || lower.startsWith('zh-mo') || lower.startsWith('zh-hant')) {
    return 'zh-Hant';
  }

  // Fallback to base language subtag (e.g. en-US -> en).
  const base = lower.split('-')[0];
  const baseMatch = supportedLocales.find((k) => k.toLowerCase() === base);
  return baseMatch || null;
}

function _localeAssetUrl(resolved) {
  const filename = `locale-${_SUPPORTED_LOCALE_SLUGS[resolved]}.js`;
  if (_localeRuntimeAssetUrl) {
    const runtimeUrl = new URL(_localeRuntimeAssetUrl);
    const localeUrl = new URL(filename, runtimeUrl);
    localeUrl.search = runtimeUrl.search;
    return localeUrl.href;
  }
  return new URL(`static/i18n_parts/${filename}`, document.baseURI).href;
}

function _loadLocaleBundle(resolved) {
  if (LOCALES[resolved]) return Promise.resolve(LOCALES[resolved]);
  const pending = _localeLoadPromises.get(resolved);
  if (pending) return pending;

  const promise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = _localeAssetUrl(resolved);
    script.async = true;
    script.onload = () => {
      if (LOCALES[resolved]) resolve(LOCALES[resolved]);
      else reject(new Error(`Locale bundle did not register: ${resolved}`));
    };
    script.onerror = () => reject(new Error(`Failed to load locale bundle: ${resolved}`));
    document.head.appendChild(script);
  }).catch((error) => {
    _localeLoadPromises.delete(resolved);
    throw error;
  });
  _localeLoadPromises.set(resolved, promise);
  return promise;
}

function _activateLocale(resolved) {
  _locale = LOCALES[resolved] || LOCALES.en;
  document.documentElement.lang = _locale._speech || resolved;
}

/**
 * Resolve locale with precedence:
 * 1) primary (typically server setting)
 * 2) fallback (typically localStorage)
 * 3) English
 * @param {string} primary
 * @param {string} fallback
 * @returns {string}
 */
function resolvePreferredLocale(primary, fallback) {
  return resolveLocale(primary) || resolveLocale(fallback) || 'en';
}

/**
 * Translate a key. Falls back to English if the key is missing in the active locale.
 * Supports function values (for interpolated strings): call t('key', arg).
 * @param {string} key
 * @param {...*} args - forwarded to function-valued translations
 * @returns {string}
 */
function t(key, ...args) {
  const val = _locale[key] ?? LOCALES.en[key];
  if (val === undefined) return key;  // final fallback: return key itself
  if (typeof val === 'function') return val(...args);
  if (args.length) {
    // Locale strings can use numbered placeholders like {0} and {1}.
    return String(val).replace(/\{(\d+)\}/g, (match, idx) => (
      Object.prototype.hasOwnProperty.call(args, idx) ? String(args[idx]) : match
    ));
  }
  return val;
}

/**
 * Switch locale by language code (e.g. 'en', 'zh').
 * Persists to localStorage and updates the <html lang> attribute.
 * @param {string} lang
 * @returns {Promise<string>} active canonical locale after loading
 */
function setLocale(lang) {
  const resolved = resolveLocale(lang) || 'en';
  _requestedLocale = resolved;
  try { localStorage.setItem('hermes-lang', resolved); } catch (_) {}
  if (LOCALES[resolved]) {
    _activateLocale(resolved);
    return Promise.resolve(resolved);
  }

  return _loadLocaleBundle(resolved).then(() => {
    if (_requestedLocale !== resolved) return _requestedLocale;
    _activateLocale(resolved);
    applyLocaleToDOM();
    return resolved;
  }).catch((error) => {
    console.warn('[hermes] Locale load failed; falling back to English:', error);
    if (_requestedLocale === resolved) {
      _activateLocale('en');
      applyLocaleToDOM();
    }
    return 'en';
  });
}

/**
 * Load locale from localStorage (called once at boot, before DOMContentLoaded).
 * Server-persisted preference is applied later in loadSettingsPanel().
 */
function loadLocale() {
  let stored = null;
  try { stored = localStorage.getItem('hermes-lang'); } catch (_) {}
  setLocale(resolvePreferredLocale(null, stored));
}

/**
 * Re-stamp all [data-i18n] elements in the DOM with the current locale.
 * Safe to call at any time — missing keys fall back to English.
 * Call after setLocale() to make static HTML text update without a reload.
 */
function applyLocaleToDOM() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const val = t(key);
    if (val && val !== key) el.textContent = val;
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    const val = t(key);
    if (!val || val === key) return;
    if (el.hasAttribute('data-tooltip')) {
      // Custom CSS tooltip is in use (#1775) — sync it and explicitly clear
      // the native `title` attribute so the slow ~1.5s browser tooltip never
      // co-fires alongside the fast custom tooltip.
      el.setAttribute('data-tooltip', val);
      if (el.hasAttribute('title')) el.removeAttribute('title');
    } else {
      // Element opted out of custom tooltips — fall back to the native title.
      el.title = val;
    }
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    const val = t(key);
    if (val && val !== key) el.placeholder = val;
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
    const key = el.getAttribute('data-i18n-aria-label');
    const val = t(key);
    if (val && val !== key) el.setAttribute('aria-label', val);
  });
  if (typeof syncWorkspacePanelUI === 'function') syncWorkspacePanelUI();
  if (typeof syncAppTitlebar === 'function') syncAppTitlebar();
}

// Apply saved locale immediately so there's no flash of English on reload.
loadLocale();

Object.assign(HermesI18n, {
  supportedLocales: SUPPORTED_LOCALES,
  resolveLocale,
  resolvePreferredLocale,
  t,
  setLocale,
  loadLocale,
  applyLocaleToDOM,
});
