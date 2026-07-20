/*
 * Hermes WebUI classic-script bootstrap.
 *
 * Load this file first, followed by the numbered scripts in static/ui_parts/.
 * The numbered files deliberately remain classic scripts: their existing
 * top-level bindings are compatibility globals shared with messages.js and the
 * current test harnesses. New integrations should prefer the explicit APIs in
 * window.HermesUI.modules or window.HermesUI.compat.
 */
(function bootstrapHermesUI(root){
  const api=root.HermesUI||{};
  const modules=api.modules||Object.create(null);
  const compat=api.compat||Object.create(null);

  api.modules=modules;
  api.compat=compat;
  api.register=function registerHermesUIModule(name,exports){
    if(!name||!exports) throw new Error('HermesUI module registration requires a name and exports');
    if(Object.prototype.hasOwnProperty.call(modules,name)){
      throw new Error(`HermesUI module already registered: ${name}`);
    }
    const frozen=Object.freeze(Object.assign(Object.create(null),exports));
    modules[name]=frozen;
    Object.assign(compat,exports);
    return frozen;
  };

  root.HermesUI=api;
})(window);
