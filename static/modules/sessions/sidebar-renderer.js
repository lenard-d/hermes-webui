import { renderSessionListFromCache } from './sidebar-list-orchestrator.js';
import { registerSidebarRenderer } from './sidebar-render-port.js';
import { _renderOneSession } from './sidebar-row-presentation.js';

registerSidebarRenderer(renderSessionListFromCache);

export const sidebarRendering=Object.freeze({
  renderRow:_renderOneSession,
  renderList:renderSessionListFromCache,
});

export { renderSessionListFromCache };
