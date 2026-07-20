// Shared bootstrap for independently loadable Hermes translation scripts.
(function bootstrapHermesI18n(global) {
  'use strict';
  const api = global.HermesI18n = global.HermesI18n || {};
  api.locales = api.locales || {};
  api.helpers = api.helpers || {};
  api.registerLocale = api.registerLocale || function registerLocale(locale, translations) {
    const bundle = api.locales[locale] || (api.locales[locale] = {});
    Object.assign(bundle, translations);
  };
})(typeof window !== 'undefined' ? window : globalThis);

// Historical public binding used by commands.js and panels.js.
var LOCALES = (typeof window !== 'undefined' ? window : globalThis).HermesI18n.locales;
