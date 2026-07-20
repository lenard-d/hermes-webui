// Stable compatibility facade. New code should import the semantic owner that
// implements the behavior instead of depending on this aggregate surface.
export * from './workspace-preferences.js';
export * from './workspace-drag-drop.js';
export * from './workspace-file-actions.js';
export * from './workspace-tree.js';
export * from './upload-tray.js';
export * from './upload-status.js';
export * from './upload-transport.js';

const compatibilityFacade = true;
const compatibilityBindings = Object.freeze({});

export { compatibilityBindings, compatibilityFacade };
