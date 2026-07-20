// Public command entrypoint. Registry composes the owner modules while this
// file preserves the stable import surface used by boot and compatibility code.
export {
  COMMANDS,
  HANDLERS,
  _activeSlashCommandOffset,
  _findComposerPathToken,
  commandInterface,
  cliOnlyCommandResponse,
  ensureSkillCommandsLoadedForAutocomplete,
  executeAgentCommand,
  executeAgentPluginCommand,
  executeCommand,
  getAgentCommandMetadata,
  getBundleCommandMetadata,
  getComposerPathAutocompleteMatches,
  getMatchingCommands,
  getSlashAutocompleteMatches,
  hideCmdDropdown,
  loadAgentCommandMetadata,
  loadBundleCommands,
  loadSkillCommands,
  navigateCmdDropdown,
  parseCommand,
  resolveBundleCommand,
  selectCmdDropdownItem,
  showCmdDropdown,
} from './registry.js';

export {handlePetSlashCommand} from './desktop-companion.js';
export {resumeManualCompressionForSession} from './manual-compression.js';
export {_trySteer} from './run-controls.js';
export {forkFromMessage,undoLastExchange} from './session-history.js';
