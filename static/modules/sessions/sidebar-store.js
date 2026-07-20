const SESSION_ARCHIVED_PAGE_SIZE = 100;
const SESSION_ARCHIVED_MAX_LOADED_LIMIT = 2000;
const SESSION_VIRTUAL_ROW_HEIGHT = 52;
const SESSION_VIRTUAL_BUFFER_ROWS = 12;
const SESSION_VIRTUAL_THRESHOLD_ROWS = 80;
const NO_PROJECT_FILTER = '__none__';
const SHOW_ALL_PROFILES_STORAGE_KEY = 'hermes-show-all-profiles';

const _selectedSessions = new Set();
const _expandedChildSessionKeys = new Set();
const _expandedLineageKeys = new Set();
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
const _optimisticallyRemovedSessionIds = new Set();
const _sessionSwipeReturnOffsets = new Map();
const _sessionAttentionSoundState = new Map();

const state = {
  _activeProject: null,
  _allProjects: [],
  _allSessions: [],
  _allSessionsScope: null,
  _archivedCliCount: 0,
  _archivedRowsLoadedLimit: SESSION_ARCHIVED_PAGE_SIZE,
  _archivedWebuiCount: 0,
  _lineageReportCacheGeneration: 0,
  _otherProfileCount: 0,
  _pendingSessionReflowPositions: null,
  _profileSwitchOpeningExistingSession: false,
  _renamingSid: null,
  _renderSessionListGen: 0,
  _renderSessionListInFlight: null,
  _renderSessionListQueuedRequest: null,
  _serverCliSessionCount: null,
  _serverWebuiSessionCount: null,
  _sessionActionAnchor: null,
  _sessionActionMenu: null,
  _sessionActionMenuId: 0,
  _sessionActionPreviousFocus: null,
  _sessionActionSessionId: null,
  _sessionAttentionSoundPrimed: false,
  _sessionListEnterAllAnimationPending: false,
  _sessionListFirstRenderAnimated: false,
  _sessionListRefreshAnimationPending: false,
  _sessionSelectMode: false,
  _sessionSourceFilter: 'webui',
  _sessionVirtualScrollList: null,
  _sessionVirtualScrollRaf: 0,
  _sessionVisibleSidebarIds: [],
  _showAllProfiles: false,
  _showArchived: false,
  _sidebarReferenceSessions: [],
};

function _restoreShowAllProfiles(){
  try{
    const raw=localStorage.getItem(SHOW_ALL_PROFILES_STORAGE_KEY);
    state._showAllProfiles = raw === '1' || raw === 'true';
  }catch(_e){ state._showAllProfiles = false; }
}

function _setShowAllProfiles(enabled){
  state._showAllProfiles=!!enabled;
  try{ localStorage.setItem(SHOW_ALL_PROFILES_STORAGE_KEY,state._showAllProfiles?'1':'0'); }catch(_e){}
}

_restoreShowAllProfiles();

// Preserve the existing property-shaped binding contract while keeping the
// backing state private to this module. Every mutable sidebar value has one
// owner, and consumers can neither replace the binding object nor add fields.
const sidebarStateBindings = {};
for(const key of Object.keys(state)){
  Object.defineProperty(sidebarStateBindings,key,{
    enumerable:true,
    get(){ return state[key]; },
    set(value){ state[key]=value; },
  });
}
Object.freeze(sidebarStateBindings);

export {
  NO_PROJECT_FILTER,
  SESSION_ARCHIVED_MAX_LOADED_LIMIT,
  SESSION_ARCHIVED_PAGE_SIZE,
  SESSION_VIRTUAL_BUFFER_ROWS,
  SESSION_VIRTUAL_ROW_HEIGHT,
  SESSION_VIRTUAL_THRESHOLD_ROWS,
  SHOW_ALL_PROFILES_STORAGE_KEY,
  _expandedChildSessionKeys,
  _expandedLineageKeys,
  _lineageReportCache,
  _lineageReportInflight,
  _optimisticallyRemovedSessionIds,
  _selectedSessions,
  _sessionAttentionSoundState,
  _sessionSwipeReturnOffsets,
  _setShowAllProfiles,
  sidebarStateBindings,
};
