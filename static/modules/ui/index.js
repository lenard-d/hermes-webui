import * as activityAndScroll from './activity-and-scroll.js';
import * as activityPresentation from './activity-presentation.js';
import * as anchorScenes from './anchor-scenes.js';
import * as assistantTurnPresentation from './assistant-turn-presentation.js';
import * as clipboard from './clipboard.js';
import * as composer from './composer.js';
import * as composerFooterFit from './composer-footer-fit.js';
import * as composerMenuRegistry from './composer-menu-registry.js';
import * as activityTiming from './activity-timing.js';
import * as composerControls from './composer-controls.js';
import * as messageScrollFollow from './message-scroll-follow.js';
import * as mobileComposerConfig from './mobile-composer-config.js';
import * as artifactPostprocessing from './artifact-postprocessing.js';
import * as codePostprocessing from './code-postprocessing.js';
import * as contentPostprocessing from './content-postprocessing.js';
import * as appDialogs from './app-dialogs.js';
import * as clipboard from './clipboard.js';
import * as inflightState from './inflight-state.js';
import * as liveTurnRecovery from './live-turn-recovery.js';
import * as messageCopyActions from './message-copy-actions.js';
import * as reconnectBanner from './reconnect-banner.js';
import * as textToSpeech from './text-to-speech.js';
import * as todoState from './todo-state.js';
import * as cliToolPresentation from './cli-tool-presentation.js';
import * as compressionUi from './compression-ui.js';
import * as handoffUi from './handoff-ui.js';
import * as agentHealthMonitor from './agent-health-monitor.js';
import * as sessionRecovery from './session-recovery.js';
import * as systemHealthMonitor from './system-health-monitor.js';
import * as updateBanner from './update-banner.js';
import * as updateLifecycle from './update-lifecycle.js';
import * as updateSummary from './update-summary.js';
import * as liveActivity from './live-activity.js';
import * as liveRunStatus from './live-run-status.js';
import * as messageRenderCache from './message-render-cache.js';
import * as messageScrollSnapshot from './message-scroll-snapshot.js';
import * as liveTurnPreservation from './live-turn-preservation.js';
import * as markdownPostprocessing from './markdown-postprocessing.js';
import * as messageEditing from './message-editing.js';
import * as mediaAndQuota from './media-and-quota.js';
import * as modelCatalog from './model-catalog.js';
import * as modelPickerRendering from './model-picker-rendering.js';
import * as modelSelection from './model-selection.js';
import * as modelState from './model-state.js';
import * as navigation from './navigation.js';
import * as presentation from './presentation.js';
import * as reasoningEffort from './reasoning-effort.js';
import * as renderer from './renderer.js';
import * as renderSupport from './render-support.js';
import * as settledActivityRenderer from './settled-activity-renderer.js';
import * as settledTurnFinalization from './settled-turn-finalization.js';
import * as state from './state.js';
import * as toolWorklog from './tool-worklog.js';
import * as thinkingLifecycle from './thinking-lifecycle.js';
import * as transparentWorklog from './transparent-worklog.js';
import * as topbarPresentation from './topbar-presentation.js';
import * as toolsetsControls from './toolsets-controls.js';
import * as workspaceAndUploads from './workspace-and-uploads.js';
import * as workspacePreferences from './workspace-preferences.js';
import * as workspaceDragDrop from './workspace-drag-drop.js';
import * as workspaceFileActions from './workspace-file-actions.js';
import * as workspaceTree from './workspace-tree.js';
import * as uploadTray from './upload-tray.js';
import * as uploadStatus from './upload-status.js';
import * as uploadTransport from './upload-transport.js';
import { publishCompatibilityDomain } from '../compatibility.js';

const modules = Object.assign(Object.create(null), {
  state,
  navigation,
  mediaAndQuota,
  modelState,
  modelCatalog,
  modelPickerRendering,
  modelSelection,
  reasoningEffort,
  composerFooterFit,
  composerMenuRegistry,
  toolsetsControls,
  mobileComposerConfig,
  messageScrollFollow,
  activityTiming,
  codePostprocessing,
  artifactPostprocessing,
  markdownPostprocessing,
  composerControls,
  activityAndScroll,
  assistantTurnPresentation,
  clipboard,
  activityPresentation,
  composer,
  appDialogs,
  clipboard,
  inflightState,
  liveTurnRecovery,
  messageCopyActions,
  reconnectBanner,
  textToSpeech,
  todoState,
  cliToolPresentation,
  compressionUi,
  handoffUi,
  agentHealthMonitor,
  sessionRecovery,
  systemHealthMonitor,
  updateBanner,
  updateLifecycle,
  updateSummary,
  presentation,
  transparentWorklog,
  topbarPresentation,
  anchorScenes,
  liveActivity,
  liveRunStatus,
  messageRenderCache,
  messageScrollSnapshot,
  liveTurnPreservation,
  renderSupport,
  renderer,
  settledActivityRenderer,
  settledTurnFinalization,
  toolWorklog,
  contentPostprocessing,
  workspacePreferences,
  workspaceDragDrop,
  workspaceFileActions,
  workspaceTree,
  uploadTray,
  uploadStatus,
  uploadTransport,
  messageEditing,
  thinkingLifecycle,
  workspaceAndUploads,
});

const api = {};
const compatibility = Object.create(null);

function exposeCompatibilityBinding(name, ownerBindings) {
  if (Object.prototype.hasOwnProperty.call(compatibility, name)) {
    throw new Error(`HermesUI compatibility binding already registered: ${name}`);
  }
  const ownerDescriptor = Object.getOwnPropertyDescriptor(ownerBindings, name);
  const descriptor = {
    configurable: true,
    enumerable: true,
    get: () => ownerBindings[name],
  };
  if (ownerDescriptor && typeof ownerDescriptor.set === 'function') {
    descriptor.set = (value) => { ownerBindings[name] = value; };
  }
  Object.defineProperty(compatibility, name, descriptor);
}

for (const moduleApi of Object.values(modules)) {
  if (moduleApi.compatibilityFacade === true) continue;
  for (const name of Object.keys(moduleApi.compatibilityBindings)) {
    exposeCompatibilityBinding(name, moduleApi.compatibilityBindings);
  }
}

api.modules = modules;
api.compat = compatibility;
api.ready = true;
api.register = function registerHermesUIModule(name, exports) {
  if (!name || !exports) {
    throw new Error('HermesUI module registration requires a name and exports');
  }
  if (Object.prototype.hasOwnProperty.call(api.modules, name)) {
    throw new Error(`HermesUI module already registered: ${name}`);
  }
  Object.defineProperty(api.modules, name, {
    configurable: false,
    enumerable: true,
    value: Object.freeze(Object.assign(Object.create(null), exports)),
  });
};
publishCompatibilityDomain('ui', {
  namespace: 'HermesUI',
  api,
  bindings: Object.getOwnPropertyDescriptors(compatibility),
});
window.dispatchEvent(new CustomEvent('hermes-ui-ready'));

export { compatibility, modules };
