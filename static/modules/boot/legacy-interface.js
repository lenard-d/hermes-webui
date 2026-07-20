/**
 * Boot and command registrations for the temporary classic-script boundary.
 * The central compatibility module owns the actual global writes.
 */
import {publishCompatibilityDomain} from '../compatibility.js';
import {COMMANDS,commandInterface} from '../commands/index.js';
import {cancelSessionStream,cancelStream} from './run-control.js';
import {
  _isDesktopWidth,
  _isInteractiveSwipeTarget,
  _isSidebarCollapsed,
  _setButtonTooltip,
  _syncSidebarAria,
  clearPreview,
  closeMobileSidebar,
  expandSidebar,
  mobileSwitchPanel,
  openWorkspacePanel,
  syncWorkspacePanelUI,
  toggleMobileSidebar,
  toggleSidebar,
  toggleWorkspacePanel,
} from './navigation.js';
import {speechCapture} from './speech-capture.js';
import {
  _hermesNotifySessionOpen,
  _hermesTtsEngineOptions,
  _hermesTtsIsRegistered,
  _hermesTtsSynth,
  _persistDefaultMessageMode,
  _readPersistedDefaultMessageMode,
  eagerDefaultMessageMode,
  registerHermesSessionOpenHandler,
  registerHermesTtsEngine,
  renderTranscript,
} from './public-interfaces.js';
import {voiceMode} from './voice-mode.js';
import {
  _COMPOSER_CONTROL_TOGGLE_DEFS,
  _COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS,
  _applyComposerControlOrder,
  _applyComposerFooterVisibilitySettings,
  _applyFontSize,
  _applySkin,
  _applyTheme,
  _applyTitlebarProfileVisibility,
  _buildSkinPicker,
  _composerControlVisibilityFromSettings,
  _mirrorSpeechSettingsFromServer,
  _orderedComposerControlDefs,
  _pickFontSize,
  _pickTheme,
  _sanitizeComposerControlOrder,
  _syncFontSizePicker,
  _syncSkinPicker,
  _syncThemePicker,
  applyBotName,
  registerHermesSkin,
} from './appearance.js';
import {
  _isImeEnter,
  _shouldAttachLargePastedText,
  exportSessionHTML,
  initResizePanels,
} from './composer.js';
import {showServerStopped,shutdownServer} from './server-lifecycle.js';

const commandCompatibility=Object.freeze({
  COMMANDS,
  _invalidateSlashModelCache:commandInterface.invalidateSlashModelCache,
  _trySteer:commandInterface.trySteer,
  ensureSkillCommandsLoadedForAutocomplete:commandInterface.ensureSkillCommandsLoadedForAutocomplete,
  executeAgentCommand:commandInterface.executeAgentCommand,
  executeAgentPluginCommand:commandInterface.executeAgentPluginCommand,
  executeCommand:commandInterface.executeCommand,
  forkFromMessage:commandInterface.forkFromMessage,
  getAgentCommandMetadata:commandInterface.getAgentCommandMetadata,
  getBundleCommandMetadata:commandInterface.getBundleCommandMetadata,
  getComposerPathAutocompleteMatches:commandInterface.getComposerPathAutocompleteMatches,
  getMatchingCommands:commandInterface.getMatchingCommands,
  getSlashAutocompleteMatches:commandInterface.getSlashAutocompleteMatches,
  handlePetSlashCommand:commandInterface.handlePetSlashCommand,
  hideCmdDropdown:commandInterface.hideCmdDropdown,
  invalidateSlashSkillCaches:commandInterface.invalidateSlashSkillCaches,
  navigateCmdDropdown:commandInterface.navigateCmdDropdown,
  parseCommand:commandInterface.parseCommand,
  resolveBundleCommand:commandInterface.resolveBundleCommand,
  resumeManualCompressionForSession:commandInterface.resumeManualCompressionForSession,
  selectCmdDropdownItem:commandInterface.selectCmdDropdownItem,
  showCmdDropdown:commandInterface.showCmdDropdown,
  undoLastExchange:commandInterface.undoLastExchange,
});

const bootCompatibility=Object.freeze({
  _COMPOSER_CONTROL_TOGGLE_DEFS,
  _COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS,
  _applyComposerControlOrder,
  _applyComposerFooterVisibilitySettings,
  _applyFontSize,
  _applyRawAudioModePreference:speechCapture.applyRawAudioModePreference,
  _applySkin,
  _applyTheme,
  _applyTitlebarProfileVisibility,
  _applyVoiceModePref:voiceMode.applyPreference,
  _buildSkinPicker,
  _composerControlVisibilityFromSettings,
  _hermesNotifySessionOpen,
  _hermesTtsEngineOptions,
  _hermesTtsIsRegistered,
  _hermesTtsSynth,
  _initResizePanels:initResizePanels,
  _isDesktopWidth,
  _isImeEnter,
  _isInteractiveSwipeTarget,
  _isSidebarCollapsed,
  _mirrorSpeechSettingsFromServer,
  _orderedComposerControlDefs,
  _persistDefaultMessageMode,
  _pickFontSize,
  _pickTheme,
  _readPersistedDefaultMessageMode,
  _sanitizeComposerControlOrder,
  _setButtonTooltip,
  _shouldAttachLargePastedText,
  _stopMic:speechCapture.stopMic,
  _syncFontSizePicker,
  _syncSidebarAria,
  _syncSkinPicker,
  _syncThemePicker,
  _toggleMicCapture:speechCapture.toggleMicCapture,
  _voiceModeActive:voiceMode.isActive,
  _voiceModeDeactivate:voiceMode.deactivate,
  _voiceModeImmediateSend:voiceMode.sendImmediately,
  _voiceModeOnResponseComplete:voiceMode.onResponseComplete,
  applyBotName,
  autoReadLastAssistant:voiceMode.autoReadLastAssistant,
  cancelSessionStream,
  cancelStream,
  clearPreview,
  closeMobileSidebar,
  expandSidebar,
  exportSessionHTML,
  mobileSwitchPanel,
  openWorkspacePanel,
  registerHermesSessionOpenHandler,
  registerHermesSkin,
  registerHermesTtsEngine,
  renderTranscript,
  showServerStopped,
  shutdownServer,
  syncWorkspacePanelUI,
  toggleMobileSidebar,
  toggleSidebar,
  toggleWorkspacePanel,
});

const definedBootCompatibility=Object.fromEntries(
  Object.entries(bootCompatibility).filter(([,value])=>value!==undefined)
);
publishCompatibilityDomain('commands',{
  namespace:'HermesCommands',
  api:Object.freeze({api:commandInterface,registry:COMMANDS}),
  bindings:commandCompatibility,
});
publishCompatibilityDomain('boot',{
  namespace:'HermesBoot',
  api:Object.freeze({api:bootCompatibility}),
  bindings:{...definedBootCompatibility,_defaultMessageMode:eagerDefaultMessageMode},
});

export {bootCompatibility,commandCompatibility};
