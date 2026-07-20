import * as activityAndScroll from './activity-and-scroll.js';
import * as activityPresentation from './activity-presentation.js';
import * as anchorScenes from './anchor-scenes.js';
import * as assistantTurnPresentation from './assistant-turn-presentation.js';
import * as composer from './composer.js';
import * as activityTiming from './activity-timing.js';
import * as composerControls from './composer-controls.js';
import * as messageScrollFollow from './message-scroll-follow.js';
import * as mobileComposerConfig from './mobile-composer-config.js';
import * as contentPostprocessing from './content-postprocessing.js';
import * as appDialogs from './app-dialogs.js';
import * as clipboard from './clipboard.js';
import * as inflightState from './inflight-state.js';
import * as liveTurnRecovery from './live-turn-recovery.js';
import * as messageCopyActions from './message-copy-actions.js';
import * as reconnectBanner from './reconnect-banner.js';
import * as textToSpeech from './text-to-speech.js';
import * as todoState from './todo-state.js';
import * as healthAndUpdates from './health-and-updates.js';
import * as cliToolPresentation from './cli-tool-presentation.js';
import * as compressionUi from './compression-ui.js';
import * as handoffUi from './handoff-ui.js';
import * as liveActivity from './live-activity.js';
import * as liveRunStatus from './live-run-status.js';
import * as messageRenderCache from './message-render-cache.js';
import * as messageScrollSnapshot from './message-scroll-snapshot.js';
import * as mediaAndQuota from './media-and-quota.js';
import * as modelCatalog from './model-catalog.js';
import * as modelSelection from './model-selection.js';
import * as modelState from './model-state.js';
import * as navigation from './navigation.js';
import * as presentation from './presentation.js';
import * as renderer from './renderer.js';
import * as renderSupport from './render-support.js';
import * as state from './state.js';
import * as toolWorklog from './tool-worklog.js';
import * as transparentWorklog from './transparent-worklog.js';
import * as topbarPresentation from './topbar-presentation.js';
import * as toolsetsControls from './toolsets-controls.js';
import * as workspaceAndUploads from './workspace-and-uploads.js';
import { publishCompatibilityDomain } from '../compatibility.js';

const modules = Object.assign(Object.create(null), {
  state,
  navigation,
  mediaAndQuota,
  modelState,
  modelCatalog,
  modelSelection,
  toolsetsControls,
  mobileComposerConfig,
  messageScrollFollow,
  activityTiming,
  composerControls,
  activityAndScroll,
  assistantTurnPresentation,
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
  healthAndUpdates,
  cliToolPresentation,
  compressionUi,
  handoffUi,
  presentation,
  transparentWorklog,
  topbarPresentation,
  anchorScenes,
  liveActivity,
  liveRunStatus,
  messageRenderCache,
  messageScrollSnapshot,
  renderSupport,
  renderer,
  toolWorklog,
  contentPostprocessing,
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
