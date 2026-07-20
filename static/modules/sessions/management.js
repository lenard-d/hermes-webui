// Compatibility facade for session management. Internal modules import the
// focused owner directly; the public sessions entrypoint keeps this stable seam.
import { navigateSession } from './session-navigation-events.js';
import { _showProjectPicker } from './session-projects.js';
import { deleteSession, removeWorktree } from './session-removal.js';

export const sessionManagement=Object.freeze({
  removeWorktree,
  deleteSession,
  showProjectPicker:_showProjectPicker,
  navigate:navigateSession,
});

export { navigateSession } from './session-navigation-events.js';
export { _showProjectContextMenu, _showProjectPicker, _startProjectCreate, _startProjectRename } from './session-projects.js';
export { deleteSession, removeWorktree } from './session-removal.js';
