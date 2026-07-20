import { installPanelCompatibility } from './compatibility.js';

export { state } from './state.js';
export { switchPanel, syncAppTitlebar } from './core.js';
export { loadCrons } from './cron-list.js';
export { loadKanban } from './kanban-board.js';
export { loadSkills, loadMemory } from './skills-memory.js';
export { loadWorkspacesPanel } from './workspaces.js';
export { loadProfilesPanel, switchToProfile } from './profiles.js';
export { switchSettingsSection } from './settings-navigation.js';
export { loadSettingsPanel } from './settings-preferences.js';

installPanelCompatibility();
