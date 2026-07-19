// Temporary compatibility adapter for the classic-script frontend.
// New module consumers should import createSessionRenderCache directly.
import {
  createRenderSignature,
  createSessionRenderCache,
} from './session_render_cache.js';

window.HermesSessionRenderCache=Object.freeze({
  create:createSessionRenderCache,
  signature:createRenderSignature,
});
