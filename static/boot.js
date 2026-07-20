/*
 * Hermes WebUI boot family facade.
 *
 * Load this file first, followed by every file in loadOrder. Parts stay as
 * classic scripts so existing compatibility globals, event ownership, and
 * evaluation order remain unchanged while each owner is independently
 * parseable and directly loadable without a bundler.
 */
(function bootstrapHermesBoot(root){
  const api=root.HermesBoot||{};
  const parts=api.parts||Object.create(null);
  const loadOrder=Object.freeze([
    '001-run-control.js',
    '002-shell-navigation.js',
    '003-speech-capture.js',
    '004-public-interfaces.js',
    '005-conversation-voice.js',
    '006-composer-session-actions.js',
    '007-appearance-preferences.js',
    '008-bootstrap-coordinator.js',
  ]);
  const ownerOrder=Object.freeze([
    'runControl',
    'shellNavigation',
    'speechCapture',
    'publicInterfaces',
    'conversationVoice',
    'composerSessionActions',
    'appearancePreferences',
    'bootstrapCoordinator',
  ]);
  let nextOwner=0;
  let activeOwner='';

  api.parts=parts;
  api.loadOrder=loadOrder;
  api.begin=function begin(name){
    const expected=ownerOrder[nextOwner];
    if(activeOwner) throw new Error(`HermesBoot part ${activeOwner} did not publish its Interface`);
    if(name!==expected) throw new Error(`HermesBoot load order violation: expected ${expected}, received ${name}`);
    activeOwner=name;
  };
  api.publish=function publish(name,exports){
    if(name!==activeOwner) throw new Error(`HermesBoot publish order violation: expected ${activeOwner}, received ${name}`);
    if(!exports||typeof exports!=='object') throw new Error(`HermesBoot part ${name} must publish an Interface`);
    parts[name]=Object.freeze(Object.assign(Object.create(null),exports));
    activeOwner='';
    nextOwner+=1;
    return parts[name];
  };
  api.assertComplete=function assertComplete(){
    if(activeOwner||nextOwner!==ownerOrder.length) throw new Error('HermesBoot family did not finish loading');
    return true;
  };

  root.HermesBoot=api;
})(window);
