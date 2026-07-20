/** Shared mutable panel-domain state. Each key has one explicit owner object. */
export const state = {
  _currentPanel: 'chat', // owner: core.js
  _renamingAppTitlebar: false, // owner: core.js // guard against re-entrant rename
  _kanbanBoard: null, // owner: core.js
  _kanbanLatestEventId: 0, // owner: core.js
  _kanbanPollTimer: null, // owner: core.js
  _kanbanCurrentTaskId: null, // owner: core.js
  _kanbanLanesByProfile: true, // owner: core.js
  _kanbanCurrentBoard: null, // owner: core.js
  _kanbanBoardsList: null, // owner: core.js
  _kanbanBoardMenuOpen: false, // owner: core.js
  _kanbanIsDispatching: false, // owner: core.js
  _kanbanSuppressCardClickUntil: 0, // owner: core.js
  _kanbanEventSource: null, // owner: core.js
  _kanbanEventSourceFailures: 0, // owner: core.js
  _skillsData: null, // owner: core.js // cached skills list
  _cronList: null, // owner: core.js // cached cron jobs (array)
  _currentCronDetail: null, // owner: core.js // full cron job object
  _currentCronDetailKey: '', // owner: core.js
  _cronMode: 'empty', // owner: core.js // 'empty' | 'read' | 'create' | 'edit'
  _cronPreFormDetail: null, // owner: core.js // snapshot of prior selection when entering a form
  _showAllCronProfiles: false, // owner: core.js
  _cronOtherProfileCount: 0, // owner: core.js
  _currentWorkspaceDetail: null, // owner: core.js // { path, name, is_default }
  _workspaceMode: 'empty', // owner: core.js // 'empty' | 'read' | 'create' | 'edit'
  _workspacePreFormDetail: null, // owner: core.js
  _currentProfileDetail: null, // owner: core.js // full profile object
  _profileMode: 'empty', // owner: core.js // 'empty' | 'read' | 'create'
  _profilePreFormDetail: null, // owner: core.js
  _pendingSettingsTargetPanel: null, // owner: core.js // destination selected while settings had unsaved changes
  _logsAutoRefreshTimer: null, // owner: core.js
  _lastLogsLines: [], // owner: core.js
  _logsSeverityFilter: 'all', // owner: core.js
  _cronSelectedSkills: [], // owner: cron-editor.js
  _cronIsDuplicate: false, // owner: cron-editor.js
  _cronSkillsCache: null, // owner: cron-editor.js
  _cronProfilesCache: null, // owner: cron-editor.js
  _cronDeliveryOptionsCache: null, // owner: cron-editor.js
  _cronWatchInterval: null, // owner: cron-editor.js
  _cronWatchStart: null, // owner: cron-editor.js
  _cronWatchTimerInterval: null, // owner: cron-editor.js
  _editingCronId: null, // owner: cron-editor.js
  _kanbanConfigApplied: false, // owner: kanban-board.js
  _kanbanRefreshScheduled: false, // owner: kanban-tasks.js
  _kanbanRefreshPendingTaskIds: new Set(), // owner: kanban-tasks.js
  _kanbanTaskModalMode: 'create', // owner: kanban-tasks.js // 'create' | 'edit'
  _kanbanTaskModalEditingId: null, // owner: kanban-tasks.js // task id when mode === 'edit'
  _kanbanProfileNamesCache: null, // owner: kanban-tasks.js // populated lazily on first modal open
  _kanbanProfileNamesCacheAt: 0, // owner: kanban-tasks.js
  _kanbanTaskModalFocusCleanup: null, // owner: kanban-tasks.js
  _kanbanTaskModalInitialDisplayedStatus: null, // owner: kanban-tasks.js
  _kanbanBoardModalFocusCleanup: null, // owner: kanban-tasks.js
  _profilesCache: null, // owner: profiles.js
  _profileDropdownFetchPromise: null, // owner: profiles.js
  _profileDropdownCacheLoadedFromStorage: false, // owner: profiles.js
  _profileSwitchGeneration: 0, // owner: profiles.js
  _profileDropdownTrigger: null, // owner: profiles.js // tracks which element triggered the dropdown
  _profileDropdownOpenGeneration: 0, // owner: profiles.js
  _cronPollSince: Date.now()/1000, // owner: runtime-alerts.js // track from page load
  _cronPollTimer: null, // owner: runtime-alerts.js
  _cronUnreadCount: 0, // owner: runtime-alerts.js
  _cronPollGeneration: 0, // owner: runtime-alerts.js
  _settingsPasswordEnvLocked: false, // owner: settings-models-auth.js
  _settingsPasswordAuthEnabled: false, // owner: settings-models-auth.js
  _auxProviders: [], // owner: settings-models-auth.js // cached provider list from /api/models
  _auxTasks: [], // owner: settings-models-auth.js // sanitized auxiliary task configs from /api/model/auxiliary
  _auxOriginalConfig: null, // owner: settings-models-auth.js // snapshot of initial config for dirty detection
  _mainAdvancedConfig: null, // owner: settings-models-auth.js // current advanced config for the default chat model
  _settingsSpeechPersistedKeys: new Set(), // owner: settings-navigation.js
  _settingsSpeechLocalStorageKeys: new Set(), // owner: settings-navigation.js
  _settingsSpeechChangedKeys: new Set(), // owner: settings-navigation.js
  _settingsDirty: false, // owner: settings-state.js
  _settingsThemeOnOpen: null, // owner: settings-state.js // track theme at open time for discard revert
  _settingsSkinOnOpen: null, // owner: settings-state.js // track skin at open time for discard revert
  _settingsFontSizeOnOpen: null, // owner: settings-state.js // track font size at open time for discard revert
  _settingsHermesDefaultModelOnOpen: '', // owner: settings-state.js
  _settingsHermesDefaultModelProviderOnOpen: null, // owner: settings-state.js
  _settingsSection: 'conversation', // owner: settings-state.js
  _currentSettingsSection: 'conversation', // owner: settings-state.js
  _settingsIndex: null, // owner: settings-state.js
  _settingsIndexPromise: null, // owner: settings-state.js
  _settingsSearchSeq: 0, // owner: settings-state.js
  _settingsSearchDismissListenerRegistered: false, // owner: settings-state.js
  _settingsAppearanceAutosaveTimer: null, // owner: settings-state.js
  _settingsAppearanceAutosaveRetryPayload: null, // owner: settings-state.js
  _settingsPreferencesAutosaveTimer: null, // owner: settings-state.js
  _settingsPreferencesAutosaveRetryPayload: null, // owner: settings-state.js
  _tabVisibilityDragSuppressUntil: 0, // owner: settings-state.js
  _composerControlDragSuppressUntil: 0, // owner: settings-state.js
  _composerControlDraggingKey: '', // owner: settings-state.js
  _mcpToolsCache: [], // owner: settings-system.js
  _mcpToolsMeta: {}, // owner: settings-system.js
  _mcpToolsPage: 1, // owner: settings-system.js
  _mcpToolsPageSize: 5, // owner: settings-system.js
  _gatewayActionInFlight: false, // owner: settings-system.js
  _collapsedCats: new Set(), // owner: skills-memory.js // persisted collapsed state across re-renders
  _currentSkillDetail: null, // owner: skills-memory.js // { name, category, content }
  _skillMode: 'empty', // owner: skills-memory.js // 'empty' | 'read' | 'create' | 'edit'
  _skillPreFormDetail: null, // owner: skills-memory.js // snapshot of previously-viewed skill when entering a form
  _editingSkillName: null, // owner: skills-memory.js
  _memoryData: null, // owner: skills-memory.js
  _notesSourcesData: null, // owner: skills-memory.js
  _notesSearchResults: [], // owner: skills-memory.js
  _notesSelectedSource: 'joplin', // owner: skills-memory.js
  _notesPreviewNote: null, // owner: skills-memory.js
  _notesSearchError: '', // owner: skills-memory.js
  _notesSearchLoading: false, // owner: skills-memory.js
  _currentMemorySection: null, // owner: skills-memory.js // 'memory' | 'user' | 'soul' | 'project_context' | 'external_notes'
  _memoryMode: 'empty', // owner: skills-memory.js // 'empty' | 'read' | 'edit'
  _workspaceList: [], // owner: workspaces.js // cached from /api/workspaces
  _wsSuggestTimer: null, // owner: workspaces.js
  _wsSuggestReq: 0, // owner: workspaces.js
  _wsSuggestIndex: -1, // owner: workspaces.js
};
