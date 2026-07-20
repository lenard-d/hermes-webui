import * as activityAndScroll from './activity-and-scroll.js';
import * as anchorScenes from './anchor-scenes.js';
import * as composer from './composer.js';
import * as composerControls from './composer-controls.js';
import * as contentPostprocessing from './content-postprocessing.js';
import * as dialogsAndReconnect from './dialogs-and-reconnect.js';
import * as healthAndUpdates from './health-and-updates.js';
import * as liveActivity from './live-activity.js';
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
import * as workspaceAndUploads from './workspace-and-uploads.js';
import { publishCompatibilityDomain } from '../compatibility.js';

const modules = Object.assign(Object.create(null), {
  state,
  navigation,
  mediaAndQuota,
  modelState,
  modelCatalog,
  modelSelection,
  composerControls,
  activityAndScroll,
  composer,
  dialogsAndReconnect,
  healthAndUpdates,
  presentation,
  transparentWorklog,
  anchorScenes,
  liveActivity,
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
