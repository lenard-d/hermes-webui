/**
 * Temporary outer seam for classic-script and inline-handler callers.
 *
 * Internal panel modules use imports. Only names referenced by index.html,
 * generated inline handlers, or the remaining classic frontend families are
 * installed on window here.
 */
import { state } from './state.js';
import * as core from './core.js';
import * as cronList from './cron-list.js';
import * as cronEditor from './cron-editor.js';
import * as kanbanBoard from './kanban-board.js';
import * as kanbanTasks from './kanban-tasks.js';
import * as kanbanBoards from './kanban-boards.js';
import * as diagnostics from './diagnostics.js';
import * as knowledge from './skills-memory.js';
import * as workspaces from './workspaces.js';
import * as profiles from './profiles.js';
import * as settingsState from './settings-state.js';
import * as settingsNavigation from './settings-navigation.js';
import * as settingsPreferences from './settings-preferences.js';
import * as extensions from './settings-extensions.js';
import * as providers from './settings-providers.js';
import * as modelsAndAuth from './settings-models-auth.js';
import * as settingsSave from './settings-save.js';
import * as runtimeAlerts from './runtime-alerts.js';
import * as gateway from './settings-system.js';

const modules = {
  core,
  cronList,
  cronEditor,
  kanbanBoard,
  kanbanTasks,
  kanbanBoards,
  diagnostics,
  knowledge,
  workspaces,
  profiles,
  settingsState,
  settingsNavigation,
  settingsPreferences,
  extensions,
  providers,
  modelsAndAuth,
  settingsSave,
  runtimeAlerts,
  gateway,
};

const compatibilityGlobalNames = [
  '_applyLogsSeverityFilter', '_applyTabOrder', '_applyTabVisibility', '_applyTtsEnabled', '_applyWorkspaceTodosTabVisibility', '_closeSettingsPanel',
  '_discardSettings', '_gatewayAction', '_getHiddenTabs', '_getTabOrder', '_kanbanJsArg', '_kanbanOnWorkspaceKindChange',
  '_kanbanPopulateAssigneeSelect', '_legacyTodosFromMessages', '_loadRunContent', '_openProfileSwitchSessionBrowser', '_openWikiBrowser', '_pickChatActivityDisplayMode',
  '_pickTransparentEventTimestamps', '_renderTabVisibilityChips', '_resetCronUnreadForProfileSwitch', '_restoreCheckpoint', '_retryAppearanceAutosave', '_scheduleAppearanceAutosave',
  '_setAuthDisabledAck', '_syncHermesPanelSessionActions', '_syncLogsAutoRefresh', '_syncLogsWrap', '_syncMobileSidebarPanelFromMainView', '_viewCheckpointDiff',
  'activateCurrentProfile', 'activateCurrentWorkspace', 'addKanbanComment', 'addKanbanDependency', 'allowKanbanDrop', 'archiveKanbanBoard',
  'blockKanbanTask', 'bulkUpdateKanban', 'cancelCronForm', 'cancelMemoryEdit', 'cancelProfileForm', 'cancelSkillForm',
  'cancelWorkspaceForm', 'checkUpdatesNow', 'checkWebUIVersionSkew', 'clearConversation', 'clearKanbanDrop', 'clearKanbanFilters',
  'closeKanbanBoardModal', 'closeKanbanTaskDetail', 'closeKanbanTaskModal', 'closeProfileDropdown', 'closeWsDropdown', 'copyCurrentCronDiagnostics',
  'copyExtensionsDiagnostics', 'copyLogsAll', 'createKanbanTask', 'deleteCurrentCron', 'deleteCurrentProfile', 'deleteCurrentSkill',
  'deleteCurrentWorkspace', 'deletePasskey', 'disableAuth', 'dismissErrorBanner', 'dragKanbanTask', 'dropKanbanTask',
  'duplicateCurrentCron', 'editCurrentCron', 'editCurrentMemory', 'editCurrentSkill', 'editCurrentWorkspace', 'filterKanban',
  'filterMcpTools', 'filterSettings', 'filterSkills', 'finishKanbanDrag', 'getWorkspaceFriendlyName', 'goPasswordless',
  'hardRefreshWebUIClient', 'loadCrons', 'loadInsights', 'loadKanban', 'loadKanbanTask', 'loadLogs',
  'loadPluginsPanel', 'loadProvidersPanel', 'loadTodos', 'loadWorkspaceList', 'navigateToErrorSession', 'nudgeKanbanDispatcher',
  'openCronCreate', 'openKanbanCard', 'openKanbanCreate', 'openKanbanCreateBoard', 'openKanbanEdit', 'openKanbanRenameBoard',
  'openProfileCreate', 'openSkillCreate', 'openWorkspaceCreate', 'pauseCurrentCron', 'previewExternalNote', 'quickKanbanCardAction',
  'registerPasskey', 'removeKanbanDependency', 'renderWorkspacesPanel', 'resumeCurrentCron', 'runCurrentCron', 'runKanbanDispatcher',
  'saveCronForm', 'saveProfileForm', 'saveSettings', 'saveSkillForm', 'saveWorkspaceForm', 'searchExternalNotes',
  'selectExternalNotesSource', 'setMcpToolsPage', 'setMcpToolsPageSize', 'signOut', 'submitKanbanBoardModal', 'submitKanbanTaskModal',
  'submitMemorySave', 'switchExtensionsTab', 'switchKanbanBoard', 'switchPanel', 'switchSettingsSection', 'switchToProfile',
  'switchToWorkspace', 'syncAppTitlebar', 'syncWorkspaceDisplays', 'toggleComposerWsDropdown', 'toggleCronPromptExpanded', 'toggleCronRunExpanded',
  'toggleKanbanBoardMenu', 'toggleKanbanViewMode', 'toggleMcpServer', 'toggleProfileDropdown', 'toggleSettings', 'trackBackgroundError',
  'unblockKanbanTask', 'updateKanbanTask', 'updateNotificationPermissionStatus',
];

const exportedByName = Object.assign({}, ...Object.values(modules));

export function installPanelCompatibility(target = window) {
  for (const name of compatibilityGlobalNames) {
    const value = exportedByName[name];
    if (typeof value !== 'undefined') target[name] = value;
  }

  // These four state bindings still have evidenced classic-script readers;
  // _workspaceList also has one legacy writer. Accessors preserve live state.
  for (const name of ['_currentPanel', '_profilesCache', '_workspaceList', '_cronPollGeneration']) {
    Object.defineProperty(target, name, {
      configurable: true,
      enumerable: false,
      get: () => state[name],
      set: (value) => { state[name] = value; },
    });
  }

  return target;
}
