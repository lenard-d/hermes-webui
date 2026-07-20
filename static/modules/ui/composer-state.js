let _composerLockState=null;
let _compressionPlaceholderSaved=null;

export { _composerLockState, _compressionPlaceholderSaved };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _composerLockState: { enumerable: true, get: () => _composerLockState, set: (value) => { _composerLockState = value; } },
  _compressionPlaceholderSaved: { enumerable: true, get: () => _compressionPlaceholderSaved, set: (value) => { _compressionPlaceholderSaved = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
