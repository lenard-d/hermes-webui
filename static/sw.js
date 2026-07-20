/**
 * Hermes WebUI Service Worker
 * Minimal PWA service worker — enables "Add to Home Screen".
 * No offline caching of API responses (the UI requires a live backend).
 * Caches only static shell assets so the app shell loads fast on repeat visits.
 */

// Cache version is injected by the server at request time (routes.py /sw.js handler).
// Bumps automatically whenever the git commit changes — no manual edits needed.
const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';

// Static assets that form the app shell.
//
// Versioned assets (CSS + JS) include `?v=__WEBUI_VERSION__` to match the
// query string the page sends — see index.html. Without the version query
// here, every cache lookup against `?v=...` URLs would miss and fall through
// to network, defeating the pre-cache.
//
// Do not pre-cache './' or login assets here: under password auth they can be
// either the authenticated app shell or login code, and stale cached responses
// can make valid password submits fail until the user clears browser cache.
// Navigations populate './' only after a successful non-redirect network load.
const VQ = '?v=__WEBUI_VERSION__';
const SHELL_ASSETS = [
  './static/style.css' + VQ,
  './static/style_parts/001-skins-core.css' + VQ,
  './static/style_parts/002-skins-geist-neon-light.css' + VQ,
  './static/style_parts/003-shell-navigation.css' + VQ,
  './static/style_parts/004-chat-workspace-responsive.css' + VQ,
  './static/style_parts/005-menus-actions-activity.css' + VQ,
  './static/style_parts/006-streaming-compression.css' + VQ,
  './static/style_parts/007-settings.css' + VQ,
  './static/style_parts/008-sessions-transcript.css' + VQ,
  './static/style_parts/009-message-main-insights.css' + VQ,
  './static/style_parts/010-kanban.css' + VQ,
  './static/style_parts/011-logs-rtl-outline.css' + VQ,
  './static/pwa-startup.js' + VQ,
  './static/modules/boot/index.js' + VQ,
  // Native module dependencies are imported with static relative specifiers,
  // so browsers request these URLs without the page entrypoint's version query.
  './static/modules/compatibility.js',
  './static/modules/boot/appearance.js',
  './static/modules/boot/composer.js',
  './static/modules/boot/navigation.js',
  './static/modules/boot/public-interfaces.js',
  './static/modules/boot/run-control.js',
  './static/modules/boot/server-lifecycle.js',
  './static/modules/boot/speech-capture.js',
  './static/modules/boot/voice-mode.js',
  './static/modules/boot/legacy-interface.js',
  './static/modules/commands/desktop-companion.js',
  './static/modules/commands/index.js',
  './static/modules/commands/manual-compression.js',
  './static/modules/commands/registry.js',
  './static/modules/commands/run-controls.js',
  './static/modules/commands/session-history.js',
  './static/modules/assistant-turn-anchors/index.js' + VQ,
  './static/modules/assistant-turn-anchors/model.js',
  './static/modules/assistant-turn-anchors/activity-scene.js',
  // The UI module's static relative import requests this dependency without VQ.
  './static/session_render_cache.js',
  './static/modules/ui/index.js' + VQ,
  // Native-module dependencies use static relative imports and therefore load
  // without the entrypoint's version query. The shell remains network-first.
  './static/modules/ui/activity-and-scroll.js',
  './static/modules/ui/activity-presentation.js',
  './static/modules/ui/anchor-scenes.js',
  './static/modules/ui/assistant-turn-presentation.js',
  './static/modules/ui/clipboard.js',
  './static/modules/ui/composer.js',
  './static/modules/ui/composer-footer-fit.js',
  './static/modules/ui/composer-menu-registry.js',
  './static/modules/ui/activity-timing.js',
  './static/modules/ui/composer-controls.js',
  './static/modules/ui/message-scroll-follow.js',
  './static/modules/ui/mobile-composer-config.js',
  './static/modules/ui/artifact-postprocessing.js',
  './static/modules/ui/code-postprocessing.js',
  './static/modules/ui/content-postprocessing.js',
  './static/modules/ui/app-dialogs.js',
  './static/modules/ui/clipboard.js',
  './static/modules/ui/inflight-state.js',
  './static/modules/ui/live-turn-recovery.js',
  './static/modules/ui/message-copy-actions.js',
  './static/modules/ui/reconnect-banner.js',
  './static/modules/ui/text-to-speech.js',
  './static/modules/ui/todo-state.js',
  './static/modules/ui/handoff-ui.js',
  './static/modules/ui/cli-tool-presentation.js',
  './static/modules/ui/compression-ui.js',
  './static/modules/ui/agent-health-monitor.js',
  './static/modules/ui/session-recovery.js',
  './static/modules/ui/system-health-monitor.js',
  './static/modules/ui/update-banner.js',
  './static/modules/ui/update-lifecycle.js',
  './static/modules/ui/update-summary.js',
  './static/modules/ui/live-activity.js',
  './static/modules/ui/live-run-status.js',
  './static/modules/ui/message-render-cache.js',
  './static/modules/ui/message-scroll-snapshot.js',
  './static/modules/ui/live-turn-preservation.js',
  './static/modules/ui/markdown-postprocessing.js',
  './static/modules/ui/media-and-quota.js',
  './static/modules/ui/message-editing.js',
  './static/modules/ui/model-catalog.js',
  './static/modules/ui/model-picker-rendering.js',
  './static/modules/ui/model-selection.js',
  './static/modules/ui/model-state.js',
  './static/modules/ui/navigation.js',
  './static/modules/ui/presentation.js',
  './static/modules/ui/reasoning-effort.js',
  './static/modules/ui/renderer.js',
  './static/modules/ui/render-support.js',
  './static/modules/ui/settled-activity-renderer.js',
  './static/modules/ui/settled-turn-finalization.js',
  './static/modules/ui/state.js',
  './static/modules/ui/tool-worklog.js',
  './static/modules/ui/thinking-lifecycle.js',
  './static/modules/ui/toolsets-controls.js',
  './static/modules/ui/topbar-presentation.js',
  './static/modules/ui/transparent-worklog.js',
  './static/modules/ui/workspace-preferences.js',
  './static/modules/ui/workspace-drag-drop.js',
  './static/modules/ui/workspace-file-actions.js',
  './static/modules/ui/workspace-tree.js',
  './static/modules/ui/upload-tray.js',
  './static/modules/ui/upload-status.js',
  './static/modules/ui/upload-transport.js',
  './static/modules/ui/workspace-and-uploads.js',
  './static/modules/sessions/index.js' + VQ,
  './static/modules/sessions/session-state-store.js',
  './static/modules/sessions/composer-drafts.js',
  './static/modules/sessions/session-unread.js',
  './static/modules/sessions/session-runtime.js',
  './static/modules/sessions/state.js',
  './static/modules/sessions/lifecycle.js',
  './static/modules/sessions/message-loading.js',
  './static/modules/sessions/message-timeline.js',
  './static/modules/sessions/sidebar-state.js',
  './static/modules/sessions/session-list.js',
  './static/modules/sessions/session-discovery.js',
  './static/modules/sessions/sidebar-interactions.js',
  './static/modules/sessions/sidebar-renderer.js',
  './static/modules/sessions/management.js',
  './static/modules/messages/index.js' + VQ,
  // Native relative imports do not inherit the entrypoint's version query.
  './static/modules/messages/anchor-scene.js',
  './static/modules/messages/approvals.js',
  './static/modules/messages/clarify.js',
  './static/modules/messages/composer-context.js',
  './static/modules/messages/compression-events.js',
  './static/modules/messages/control-events.js',
  './static/modules/messages/core.js',
  './static/modules/messages/live-tools.js',
  './static/modules/messages/markdown-tables.js',
  './static/modules/messages/notifications.js',
  './static/modules/messages/rendering.js',
  './static/modules/messages/run-journal.js',
  './static/modules/messages/send.js',
  './static/modules/messages/session-events.js',
  './static/modules/messages/stream-lifecycle.js',
  './static/modules/messages/stream-progress.js',
  './static/modules/messages/stream.js',
  './static/modules/panels/index.js' + VQ,
  // Native module imports do not inherit the entrypoint query string.
  './static/modules/panels/legacy-interface.js',
  './static/modules/panels/core.js',
  './static/modules/panels/cron-editor.js',
  './static/modules/panels/cron-list.js',
  './static/modules/panels/diagnostics.js',
  './static/modules/panels/kanban-board.js',
  './static/modules/panels/kanban-boards.js',
  './static/modules/panels/kanban-tasks.js',
  './static/modules/panels/profiles.js',
  './static/modules/panels/runtime-alerts.js',
  './static/modules/panels/settings-extensions.js',
  './static/modules/panels/settings-models-auth.js',
  './static/modules/panels/settings-navigation.js',
  './static/modules/panels/settings-preferences.js',
  './static/modules/panels/settings-providers.js',
  './static/modules/panels/settings-save.js',
  './static/modules/panels/settings-state.js',
  './static/modules/panels/settings-system.js',
  './static/modules/panels/skills-memory.js',
  './static/modules/panels/state.js',
  './static/modules/panels/workspaces.js',
  './static/icons.js' + VQ,
  './static/i18n.js' + VQ,
  './static/i18n_parts/helpers.js' + VQ,
  './static/i18n_parts/locale-en.js' + VQ,
  './static/i18n_parts/locale-it.js' + VQ,
  './static/i18n_parts/locale-ja.js' + VQ,
  './static/i18n_parts/locale-ru.js' + VQ,
  './static/i18n_parts/locale-es.js' + VQ,
  './static/i18n_parts/locale-de.js' + VQ,
  './static/i18n_parts/locale-zh.js' + VQ,
  './static/i18n_parts/locale-zh_hant.js' + VQ,
  './static/i18n_parts/locale-pt.js' + VQ,
  './static/i18n_parts/locale-ko.js' + VQ,
  './static/i18n_parts/locale-fr.js' + VQ,
  './static/i18n_parts/locale-cs.js' + VQ,
  './static/i18n_parts/locale-tr.js' + VQ,
  './static/i18n_parts/locale-pl.js' + VQ,
  './static/i18n_parts/locale-vi.js' + VQ,
  './static/i18n_parts/runtime.js' + VQ,
  './static/workspace.js' + VQ,
  './static/workspace_parts/001-navigation.js' + VQ,
  './static/workspace_parts/002-preview-editor.js' + VQ,
  './static/workspace_parts/003-upload.js' + VQ,
  './static/terminal.js' + VQ,
  './static/onboarding.js' + VQ,
  './static/vendor/smd.min.js' + VQ,
  './static/vendor/katex/0.16.22/katex.min.css' + VQ,
  './static/vendor/katex/0.16.22/katex.min.js' + VQ,
  './static/favicon.svg',
  './static/favicon-32.png',
  './manifest.json',
];

function deleteOldShellCaches() {
  return caches.keys().then((keys) =>
    Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )
  );
}

// Install: prune old shell caches first, then pre-cache the app shell. Doing
// this before caches.open(CACHE_NAME) avoids a temporary double-cache window on
// quota-sensitive browsers during frequent version bumps.
self.addEventListener('install', (event) => {
  event.waitUntil(
    deleteOldShellCaches().then(() =>
      caches.open(CACHE_NAME).then((cache) => {
        return cache.addAll(SHELL_ASSETS).catch((err) => {
          // Non-fatal: if any asset fails, still activate
          console.warn('[sw] Shell pre-cache partial failure:', err);
        });
      })
    )
  );
  self.skipWaiting();
});

// Activate: keep the old-cache cleanup as a safety net in case install was
// interrupted or an older worker was already waiting.
self.addEventListener('activate', (event) => {
  event.waitUntil(deleteOldShellCaches());
  self.clients.claim();
});

// Fetch strategy:
// - API calls (/api/*, /stream) → always network (never cache)
// - Login assets → always network (never cache stale auth code)
// - Page navigations → network-first so auth redirects/cookies are honored
// - Shell assets → network-first with cache fallback
// - Everything else → network-only
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept cross-origin requests
  if (url.origin !== self.location.origin) return;

  // Never intercept the service worker script itself. Returning a cached sw.js
  // prevents the browser from seeing a new cache version after local patches.
  if (url.pathname.endsWith('/sw.js')) return;

  // Login assets must always hit the network. Older login.js builds have had
  // subpath-sensitive auth POST paths; if the service worker caches one, the
  // password can keep failing until the user manually clears browser cache.
  if (
    url.pathname.endsWith('/login') ||
    url.pathname.endsWith('/static/login.js')
  ) {
    return;
  }

  // API and streaming endpoints — always go to network.
  // The WebUI may be mounted under a subpath such as /hermes/, so API
  // requests can look like /hermes/api/sessions rather than /api/sessions.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.includes('/api/') ||
    url.pathname.includes('/stream') ||
    url.pathname.startsWith('/health') ||
    url.pathname.includes('/health')
  ) {
    return; // let browser handle normally
  }

  // Page navigations must be network-first. A stale cached './' response can
  // otherwise hide the server's 302-to-login after auth expiry, or ignore a
  // freshly set login cookie until the user manually refreshes.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(new Request(event.request, { cache: 'no-store' })).then((response) => {
        if (
          event.request.method === 'GET' &&
          response.status === 200 &&
          !response.redirected
        ) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put('./', clone));
        }
        return response;
      }).catch(() => {
        return caches.match('./').then((cached) => cached || new Response(
          '<html><body style="font-family:sans-serif;padding:2rem;background:#1a1a1a;color:#ccc">' +
          '<h2>You are offline</h2>' +
          '<p>Hermes requires a server connection. Please check your network and try again.</p>' +
          '</body></html>',
          { headers: { 'Content-Type': 'text/html' } }
        ));
      })
    );
    return;
  }

  // Only explicit shell assets are cached. Everything else should hit the
  // network so stale one-off files (especially auth/login scripts) do not get
  // trapped in CacheStorage until a manual cache clear.
  const scopePath = new URL(self.registration.scope).pathname;
  const relPath = url.pathname.startsWith(scopePath)
    ? url.pathname.slice(scopePath.length)
    : url.pathname.replace(/^\/+/, '');
  const shellPath = './' + relPath.replace(/^\/+/, '') + url.search;
  if (!SHELL_ASSETS.includes(shellPath)) return;

  // Shell assets: network-first with cache fallback. This keeps offline support
  // but avoids executing stale JS/CSS after a local hotfix when WEBUI_VERSION
  // has not changed yet (e.g. before a guarded restart updates the ?v token).
  event.respondWith(
    fetch(new Request(event.request, { cache: 'no-store' })).then((response) => {
      if (
        event.request.method === 'GET' &&
        response.status === 200
      ) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => caches.match(event.request).then((cached) => cached || new Response('Offline', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    })))
  );
});


self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawUrl = (event.notification.data && event.notification.data.url) || './';
  const targetUrl = new URL(rawUrl, self.registration.scope || './').href;
  const targetPath = new URL(targetUrl).pathname;
  const samePath = (clientUrl) => {
    try { return new URL(clientUrl).pathname === targetPath; } catch (_e) { return false; }
  };
  const sameOrigin = (clientUrl) => {
    try { return new URL(clientUrl).origin === self.location.origin; } catch (_e) { return false; }
  };
  event.waitUntil(
    self.clients.matchAll({type: 'window', includeUncontrolled: true}).then((clientList) => {
      // Match on pathname, not the full href: _sessionUrlForSid copies the
      // current page's query string + hash into the deep link, so an open tab
      // already on /session/<sid> would fail an exact-href match and spawn a
      // duplicate window.
      const targetClient = clientList.find((client) => samePath(client.url) && 'focus' in client);
      if (targetClient) return targetClient.focus();

      const openNotificationWindow = () => (
        self.clients.openWindow ? self.clients.openWindow(targetUrl) : undefined
      );
      const focusableClient = clientList.find((client) => sameOrigin(client.url) && 'focus' in client && 'navigate' in client);
      if (focusableClient && 'navigate' in focusableClient) {
        return focusableClient.navigate(targetUrl)
          .then((client) => (client && 'focus' in client ? client.focus() : focusableClient.focus()))
          .catch(() => focusableClient.focus());
      }
      return openNotificationWindow();
    })
  );
});
