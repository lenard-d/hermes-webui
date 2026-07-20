/**
 * Sessions compatibility facade.
 *
 * Load the classic scripts listed below in order, then load this file last.
 * The parts share the browser's classic-script global scope; this facade pins
 * the supported cross-file globals while the namespaced module interfaces are
 * available under window.HermesSessions.parts.
 */
(function installHermesSessionsFacade(root){
  const sessions=root.HermesSessions;
  if(!sessions||!sessions.parts) throw new Error('Hermes session parts must load before sessions.js');

  sessions.loadOrder=Object.freeze([
    '001-session-state.js',
    '002-session-lifecycle.js',
    '003-session-messages.js',
    '004-message-timeline.js',
    '005-sidebar-controls.js',
    '006-list-updates.js',
    '007-session-discovery.js',
    '008-sidebar-row-behavior.js',
    '009-sidebar-rendering.js',
    '010-session-management.js',
  ]);

  sessions.api=Object.freeze({
    newSession:sessions.parts.sessionLifecycle.create,
    loadSession:sessions.parts.sessionLifecycle.load,
    renderSessionList:sessions.parts.listUpdates.render,
    renderSessionListFromCache:sessions.parts.sidebarRendering.renderList,
    showSessionListSkeleton:sessions.parts.listUpdates.showSkeleton,
    refreshSessionList:sessions.parts.listUpdates.refresh,
    filterSessions:sessions.parts.sessionDiscovery.filter,
    clearSessionSearch:sessions.parts.sessionDiscovery.clear,
    deleteSession:sessions.parts.sessionManagement.deleteSession,
    removeWorktree:sessions.parts.sessionManagement.removeWorktree,
    navigateSession:sessions.parts.sessionManagement.navigate,
  });

  // Compatibility globals consumed by boot.js, messages.js, panels.js and
  // third-party extensions. They intentionally point at the namespaced owners.
  Object.assign(root,sessions.api);
})(window);
