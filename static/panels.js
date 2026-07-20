/**
 * Panels core and compatibility namespace.
 *
 * The files in panels_parts are ordered classic scripts. Existing top-level
 * function declarations remain compatibility globals for markup and sibling
 * scripts; each domain also publishes its owned functions on HermesPanels.
 */
window.HermesPanels = window.HermesPanels || {
  interface: 'classic-global',
};
let _currentPanel = 'chat';
let _renamingAppTitlebar = false;  // guard against re-entrant rename
let _kanbanBoard = null;
let _kanbanLatestEventId = 0;
let _kanbanPollTimer = null;
let _kanbanCurrentTaskId = null;
let _kanbanLanesByProfile = true;
// Multi-board state. _kanbanCurrentBoard is the slug of the active board
// the UI is currently viewing. null means "use whatever the server reports
// as active" (i.e. don't pin a specific board in API calls). The UI
// persists the last-viewed slug to localStorage so refresh stays put.
let _kanbanCurrentBoard = null;
let _kanbanBoardsList = null;
let _kanbanBoardMenuOpen = false;
let _kanbanIsDispatching = false;
let _kanbanSuppressCardClickUntil = 0;
// SSE event stream — replaces the 30s polling cadence with a long-lived
// /api/kanban/events/stream connection. Falls back to polling when the
// EventSource fails to connect (proxy that strips text/event-stream, etc).
let _kanbanEventSource = null;
let _kanbanEventSourceFailures = 0;
let _skillsData = null; // cached skills list
let _cronList = null; // cached cron jobs (array)
let _currentCronDetail = null; // full cron job object
let _currentCronDetailKey = '';
let _cronMode = 'empty'; // 'empty' | 'read' | 'create' | 'edit'
let _cronPreFormDetail = null; // snapshot of prior selection when entering a form
let _showAllCronProfiles = false;
let _cronOtherProfileCount = 0;
let _currentWorkspaceDetail = null; // { path, name, is_default }
let _workspaceMode = 'empty'; // 'empty' | 'read' | 'create' | 'edit'
let _workspacePreFormDetail = null;
let _currentProfileDetail = null; // full profile object
let _profileMode = 'empty'; // 'empty' | 'read' | 'create'
let _profilePreFormDetail = null;
let _pendingSettingsTargetPanel = null; // destination selected while settings had unsaved changes
let _logsAutoRefreshTimer = null;
let _lastLogsLines = [];
let _logsSeverityFilter = 'all';

// Map of panel names → i18n keys for the app titlebar label.
const APP_TITLEBAR_KEYS = {
  chat: 'tab_chat', tasks: 'tab_tasks', skills: 'tab_skills',
  memory: 'tab_memory', workspaces: 'tab_workspaces',
  profiles: 'tab_profiles', todos: 'tab_todos', insights: 'tab_insights', logs: 'tab_logs', settings: 'tab_settings',
};
const MAIN_VIEW_PANELS = ['settings','skills','memory','tasks','kanban','workspaces','profiles','insights','logs','plugin'];
const MAIN_VIEW_SIDEBAR_PANEL_FALLBACKS = { plugin: 'settings' };

/**
 * Update the top app titlebar to reflect the current page or selected conversation.
 * On the chat panel, a selected session's title takes precedence over the page name.
 */
function syncAppTitlebar() {
  const titleEl = document.getElementById('appTitlebarTitle');
  const subEl = document.getElementById('appTitlebarSub');
  if (!titleEl) return;
  const panel = (typeof _currentPanel === 'string' && _currentPanel) ? _currentPanel : 'chat';
  let mainText = '';
  let subText = '';
  let sourceLabel = '';
  if (panel === 'chat' && typeof S !== 'undefined' && S && S.session) {
    mainText = S.session.title || (typeof t === 'function' ? t('untitled') : 'Untitled');
    const vis = Array.isArray(S.messages) ? S.messages.filter(m => m && m.role && m.role !== 'tool') : [];
    subText = String(vis.length);
    sourceLabel = S.session.source_label || S.session.source_tag || S.session.raw_source || '';
    // Recovered sidecars stamp source_label 'WebUI' (api/session_recovery.py); don't badge a native session as its own source (#3338).
    if (/^webui$/i.test(sourceLabel)) sourceLabel = '';
  } else {
    const key = APP_TITLEBAR_KEYS[panel];
    mainText = key && typeof t === 'function' ? t(key) : (panel.charAt(0).toUpperCase() + panel.slice(1));
  }

  // Don't touch the element while an inline rename is in progress — replacing
  // the span with an input would fire a MutationObserver that calls
  // syncAppTitlebar again, destroying the input before the user finishes.
  if (_renamingAppTitlebar) return;

  titleEl.textContent = mainText;
  if (panel !== 'chat') {
    const bot = typeof assistantDisplayName === 'function' ? assistantDisplayName() : '';
    document.title = bot ? mainText + ' \u2014 ' + bot : mainText;
  }
  if (subEl) {
    if (subText) {
      subEl.textContent = subText;
      if (sourceLabel) {
        const badge = document.createElement('span');
        badge.className = 'topbar-source-badge';
        badge.textContent = sourceLabel + (S.session && S.session.read_only ? ' · read-only' : '');
        subEl.appendChild(document.createTextNode(' '));
        subEl.appendChild(badge);
      }
      subEl.hidden = false;
    }
    else { subEl.textContent = ''; subEl.hidden = true; }
  }

  // Double-click on the titlebar title → rename the active session (same behaviour
  // as double-clicking a session title in the sidebar).  Only active on the chat
  // panel when a session is open.
  titleEl.ondblclick = null;  // remove any previous handler before adding a fresh one
  if (panel === 'chat' && typeof S !== 'undefined' && S && S.session && !(S.session.read_only || S.session.is_read_only)) {
    titleEl.ondblclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (_renamingAppTitlebar) return;
      _renamingAppTitlebar = true;

      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'app-titlebar-rename-input';
      inp.value = S.session.title || (typeof t === 'function' ? t('untitled') : 'Untitled');

      // Prevent click/dblclick on the input from bubbling — we don't want
      // panel switches, session switches, or any other handler firing.
      ['click', 'mousedown', 'dblclick', 'pointerdown'].forEach(ev =>
        inp.addEventListener(ev, e2 => e2.stopPropagation())
      );

      const finish = async (save) => {
        _renamingAppTitlebar = false;
        if (save) {
          const newTitle = inp.value.trim() || (typeof t === 'function' ? t('untitled') : 'Untitled');
          S.session.title = newTitle;
          syncTopbar();   // update #topbarTitle in the chat header
          syncAppTitlebar();
          // Update the sidebar list so the renamed title appears immediately.
          // _renderOneSession reads from _allSessions cache, so patch it there too.
          try {
            const _cached = typeof _allSessions !== 'undefined' && _allSessions.find(s => s && s.session_id === S.session.session_id);
            if (_cached) _cached.title = newTitle;
          } catch (_) {}
          if (typeof renderSessionListFromCache === 'function') renderSessionListFromCache();
          try {
            await api('/api/session/rename', {
              method: 'POST',
              body: JSON.stringify({ session_id: S.session.session_id, title: newTitle })
            });
          } catch (err) {
            if (typeof setStatus === 'function') setStatus('Rename failed: ' + err.message);
          }
        }
        inp.replaceWith(titleEl);
        syncAppTitlebar();
      };

      inp.onkeydown = e2 => {
        if (e2.key === 'Enter') { e2.preventDefault(); e2.stopPropagation(); finish(true); }
        if (e2.key === 'Escape') { e2.preventDefault(); e2.stopPropagation(); finish(false); }
      };
      inp.onblur = () => finish(false);

      titleEl.replaceWith(inp);
      inp.focus();
      inp.select();
    };
  }

  // Dismiss stale popover on session/panel switch
  const _existingPop = document.querySelector('.app-titlebar-title-popover');
  if (_existingPop) {
    _existingPop.remove(); titleEl._titlePopover = null;
    if (titleEl._popoverOutsideHandler) {
      document.removeEventListener('click', titleEl._popoverOutsideHandler, true);
      titleEl._popoverOutsideHandler = null;
    }
  }

  // Mobile touch interactions
  if ('ontouchstart' in window) {
    // Tap-to-reveal full title popover — wired once per element lifetime
    if (!titleEl._mobileTouchWired) {
      titleEl._mobileTouchWired = true;
      titleEl._titlePopover = null;
      const _dismissTitlePopover = () => {
        if (titleEl._titlePopover) { titleEl._titlePopover.remove(); titleEl._titlePopover = null; }
        if (titleEl._popoverOutsideHandler) {
          document.removeEventListener('click', titleEl._popoverOutsideHandler, true);
          titleEl._popoverOutsideHandler = null;
        }
      };
      titleEl.addEventListener('click', function _onTitleClick(e) {
        if (_renamingAppTitlebar) return;
        if (titleEl._titlePopover) {
          _dismissTitlePopover();
          return;
        }
        e.stopPropagation();
        const pop = document.createElement('div');
        pop.className = 'app-titlebar-title-popover';
        pop.textContent = (S && S.session && S.session.title) ||
          (typeof t === 'function' ? t('untitled') : 'Untitled');
        document.body.appendChild(pop);
        const rect = titleEl.getBoundingClientRect();
        pop.style.top = (rect.bottom + 6) + 'px';
        pop.style.left = Math.max(8, rect.left) + 'px';
        pop.style.maxWidth = (window.innerWidth - 16) + 'px';
        titleEl._titlePopover = pop;
        const _outside = titleEl._popoverOutsideHandler = (ev) => {
          if (!pop.contains(ev.target) && ev.target !== titleEl) {
            _dismissTitlePopover();
            document.removeEventListener('click', _outside, true);
            titleEl._popoverOutsideHandler = null;
          }
        };
        setTimeout(() => document.addEventListener('click', _outside, true), 0);
      }, { passive: true });
    }

    // Long-press → session action menu (re-evaluated each sync so late-arriving sessions attach)
    if (!titleEl._mobileLpWired && panel === 'chat' && S && S.session &&
        !S.session.read_only && !S.session.is_read_only &&
        typeof _openSessionActionMenu === 'function') {
      titleEl._mobileLpWired = true;
      let _lpTimer = null;
      let _lpHandled = false;
      let _lpStartX = 0, _lpStartY = 0;
      const _lpDelay = typeof SESSION_LONG_PRESS_DELAY_MS !== 'undefined' ?
        SESSION_LONG_PRESS_DELAY_MS : 400;
      titleEl.addEventListener('touchstart', (e) => {
        const touch = e.changedTouches && e.changedTouches[0];
        if (!touch) return;
        if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
        _lpHandled = false; _lpStartX = touch.clientX; _lpStartY = touch.clientY;
        titleEl.classList.add('long-pressing');
        _lpTimer = setTimeout(() => {
          _lpTimer = null;
          if (_lpHandled) return;
          _lpHandled = true;
          titleEl.classList.remove('long-pressing');
          _openSessionActionMenu(S.session, titleEl);
        }, _lpDelay);
      }, { passive: true });
      titleEl.addEventListener('touchmove', (e) => {
        if (!_lpTimer) return;
        const touch = e.changedTouches && e.changedTouches[0];
        if (!touch) return;
        if (Math.abs(touch.clientX - _lpStartX) > 10 || Math.abs(touch.clientY - _lpStartY) > 10) {
          clearTimeout(_lpTimer); _lpTimer = null;
          titleEl.classList.remove('long-pressing');
        }
      }, { passive: true });
      titleEl.addEventListener('touchend', (e) => {
        clearTimeout(_lpTimer); _lpTimer = null;
        titleEl.classList.remove('long-pressing');
        if (_lpHandled) { e.preventDefault(); e.stopPropagation(); }
      }, { passive: false });
      titleEl.addEventListener('touchcancel', () => {
        clearTimeout(_lpTimer); _lpTimer = null; _lpHandled = false;
        titleEl.classList.remove('long-pressing');
      }, { passive: true });
    }
  }
}

function _beginSettingsPanelSession() {
  _settingsIndex = null;
  _settingsIndexPromise = null;
  // Invalidate any in-flight search render from a PRIOR Settings session and
  // reset the search UI, so a slow index build that resolves after the panel
  // was closed/reopened can't paint stale results into the dropdown. #4340
  // review fix (filterSettings() bails when its captured seq != current).
  ++_settingsSearchSeq;
  const _searchInput = $('settingsSearch');
  if (_searchInput) _searchInput.value = '';
  const _searchResults = $('settingsSearchResults');
  if (_searchResults) {
    _searchResults.style.display = 'none';
    _searchResults.innerHTML = '';
  }
  _settingsDirty = false;
  _settingsThemeOnOpen = localStorage.getItem('hermes-theme') || 'dark';
  _settingsSkinOnOpen = localStorage.getItem('hermes-skin') || 'default';
  _settingsFontSizeOnOpen = localStorage.getItem('hermes-font-size') || 'default';
  _pendingSettingsTargetPanel = null;
  if (_settingsAppearanceAutosaveTimer) {
    clearTimeout(_settingsAppearanceAutosaveTimer);
    _settingsAppearanceAutosaveTimer = null;
  }
  _settingsAppearanceAutosaveRetryPayload = null;
  if (!_settingsSearchDismissListenerRegistered) {
    _settingsSearchDismissListenerRegistered = true;
    document.addEventListener('click', e => {
      if (!e.target.closest('#settingsMenu')) {
        // Invalidate an in-flight first-build too, so it can't resurrect the
        // dropdown after an outside-click dismiss. #4340 review fix.
        ++_settingsSearchSeq;
        const r = $('settingsSearchResults');
        if (r) {
          r.style.display = 'none';
          r.innerHTML = '';
        }
      }
    });
  }
  _resetSettingsPanelState();
}

function _beforePanelSwitch(nextPanel) {
  if (_currentPanel !== 'settings' || nextPanel === 'settings') return true;
  if (_settingsDirty) {
    _pendingSettingsTargetPanel = nextPanel || 'chat';
    _showSettingsUnsavedBar();
    return false;
  }
  _revertSettingsPreview();
  _pendingSettingsTargetPanel = null;
  _resetSettingsPanelState();
  return true;
}

function _consumeSettingsTargetPanel(fallback = 'chat') {
  const target = (_pendingSettingsTargetPanel && _pendingSettingsTargetPanel !== 'settings')
    ? _pendingSettingsTargetPanel
    : fallback;
  _pendingSettingsTargetPanel = null;
  return target;
}

function _resyncChatSidebarAfterPanelSwitch() {
  if (_currentPanel !== 'chat') return;
  if (typeof renderSessionListFromCache !== 'function') return;
  const run = () => {
    if (_currentPanel !== 'chat') return;
    if (typeof _renamingSid !== 'undefined' && _renamingSid) return;
    // If the user opens the per-conversation action menu immediately after
    // returning to Chat, do not let the deferred sidebar resync tear it down.
    // renderSessionListFromCache() intentionally closes that menu before it
    // rebuilds rows, which is correct for normal list refreshes but hostile to
    // this one-shot panel-transition repair.
    if (typeof _sessionActionMenu !== 'undefined' && _sessionActionMenu) return;
    renderSessionListFromCache();
  };
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
  else run();
}

function _closeMobileSidebarAfterPanelSelection(){
  if(typeof closeMobileSidebar!=='function')return;
  if(typeof _isDesktopWidth==='function'&&_isDesktopWidth())return;
  closeMobileSidebar();
}

function _panelFromCurrentMainView(){
  const mainEl=document.querySelector('main.main');
  if(!mainEl)return _currentPanel||'chat';
  for(const panel of MAIN_VIEW_PANELS){
    if(mainEl.classList.contains('showing-'+panel))return MAIN_VIEW_SIDEBAR_PANEL_FALLBACKS[panel]||panel;
  }
  if(_currentPanel&&$('panel'+_currentPanel.charAt(0).toUpperCase()+_currentPanel.slice(1)))return _currentPanel;
  return 'chat';
}

function _syncMobileSidebarPanelFromMainView(){
  const panel=_panelFromCurrentMainView();
  if(!panel)return _currentPanel||'chat';
  const panelEl=$('panel'+panel.charAt(0).toUpperCase()+panel.slice(1));
  if(!panelEl)return _currentPanel||'chat';
  _currentPanel=panel;
  document.querySelectorAll('[data-panel]').forEach(t=>t.classList.toggle('active',t.dataset.panel===panel));
  document.querySelectorAll('.panel-view').forEach(p=>p.classList.remove('active'));
  panelEl.classList.add('active');
  return panel;
}

async function switchPanel(name, opts = {}) {
  const nextPanel = name || 'chat';
  const prevPanel = _currentPanel;
  // ── Desktop sidebar collapse toggle (rail-click only) ──
  // If the click came from a rail icon AND we're on desktop, the rail icon
  // does double duty: clicking the already-active panel collapses the sidebar;
  // clicking any panel while collapsed expands first. Programmatic switches
  // (no opts.fromRailClick) are unaffected so legacy callers preserve
  // behaviour exactly.
  if (opts.fromRailClick && typeof _isSidebarCollapsed === 'function'
      && typeof _isDesktopWidth === 'function' && _isDesktopWidth()) {
    if (_isSidebarCollapsed()) {
      // Expand first, then continue to the normal panel switch below so
      // the clicked panel becomes (or stays) active in the same gesture.
      expandSidebar();
    } else if (prevPanel === nextPanel) {
      // Same panel clicked while sidebar is open → collapse and short-circuit.
      // Skip the guard/cleanup work below; nothing about the active panel
      // is changing, only the visibility of the panel container.
      toggleSidebar(true);
      return false;
    }
  }
  if (!opts.bypassSettingsGuard && !_beforePanelSwitch(nextPanel)) return false;
  if (prevPanel !== 'settings' && nextPanel === 'settings') _beginSettingsPanelSession();
  // Close any long-lived Kanban SSE stream when leaving the kanban panel
  // so we don't keep a stale connection open in the background.
  if (prevPanel === 'kanban' && nextPanel !== 'kanban') {
    if (typeof _kanbanStopPolling === 'function') _kanbanStopPolling();
  }
  _currentPanel = nextPanel;
  // Update nav tabs (rail + mobile sidebar-nav share data-panel)
  document.querySelectorAll('[data-panel]').forEach(t => t.classList.toggle('active', t.dataset.panel === nextPanel));
  // Refresh aria-expanded on the newly-active rail button to mirror sidebar state.
  if (typeof _syncSidebarAria === 'function') _syncSidebarAria();
  // Update panel views
  document.querySelectorAll('.panel-view').forEach(p => p.classList.remove('active'));
  const panelEl = $('panel' + nextPanel.charAt(0).toUpperCase() + nextPanel.slice(1));
  if (panelEl) panelEl.classList.add('active');
  // Update main content view. Each entry in MAIN_VIEW_PANELS gets a matching
  // showing-<name> class on <main>; no class means chat (the default).
  const mainEl = document.querySelector('main.main');
  if (mainEl) {
    MAIN_VIEW_PANELS.forEach(p => {
      mainEl.classList.toggle('showing-' + p, nextPanel === p);
    });
  }
  // Lazy-load panel data
  if (nextPanel === 'tasks') await loadCrons();
  if (nextPanel === 'kanban') await loadKanban();
  if (nextPanel === 'skills') await loadSkills();
  if (nextPanel === 'memory') await loadMemory();
  if (nextPanel === 'workspaces') await loadWorkspacesPanel();
  if (nextPanel === 'profiles') await loadProfilesPanel();
  if (nextPanel === 'todos') loadTodos();
  if (nextPanel === 'insights') await loadInsights();
  if (nextPanel === 'logs') await loadLogs();
  _syncLogsAutoRefresh();
  if (typeof _syncSystemHealthMonitorVisibility === 'function') _syncSystemHealthMonitorVisibility();
  if (nextPanel === 'settings') {
    switchSettingsSection(_currentSettingsSection);
    loadSettingsPanel();
  }
  if (opts.fromRailClick && typeof _isDesktopWidth === 'function' && !_isDesktopWidth()) {
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) {
      sidebar.classList.remove('mobile-session-page');
      sidebar.classList.add('mobile-panel-drawer', 'mobile-open');
    }
  }
  _resyncChatSidebarAfterPanelSwitch();
  if (nextPanel === 'chat' && typeof syncTopbar === 'function') syncTopbar();
  else syncAppTitlebar();
  return true;
}

window.HermesPanels.core = {
  syncAppTitlebar,
  _beginSettingsPanelSession,
  _beforePanelSwitch,
  _consumeSettingsTargetPanel,
  _resyncChatSidebarAfterPanelSwitch,
  _closeMobileSidebarAfterPanelSelection,
  _panelFromCurrentMainView,
  _syncMobileSidebarPanelFromMainView,
  switchPanel,
};
