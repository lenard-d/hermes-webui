// ── Appearance helpers (theme = light/dark/system, skin = palette/accent) ────
const _THEMES=[
  {name:'Light', value:'light', colors:['#FEFCF7','#FAF7F0','#B8860B']},
  {name:'Dark', value:'dark', colors:['#0D0D1A','#141425','#FFD700']},
  {name:'System', value:'system', colors:['#FEFCF7','#0D0D1A','#B8860B']},
];
const _SKINS=[
  {name:'Default',  colors:['#FFD700','#FFBF00','#CD7F32']},
  {name:'Ares',     colors:['#FF4444','#CC3333','#992222']},
  {name:'Mono',     colors:['#CCCCCC','#999999','#666666']},
  {name:'Graphite', colors:['#FFFFFF','#D6D6D6','#242424']},
  {name:'GitHub', colors:['#0969DA','#1F883D','#242424']},
  {name:'Codex', colors:['#72B39A','#242624','#ECEBE4']},
  {name:'Terracotta', colors:['#D97757','#F0EEE6','#141413']},
  {name:'Slate',    colors:['#334155','#475569','#64748b']},
  {name:'Poseidon', colors:['#0EA5E9','#0284C7','#0369A1']},
  {name:'Sisyphus', colors:['#A78BFA','#8B5CF6','#7C3AED']},
  {name:'Charizard',colors:['#FB923C','#F97316','#EA580C']},
  {name:'Sienna',   colors:['#D97757','#C06A49','#9A523A']},
  {name:'Catppuccin',colors:['#CBA6F7','#B4BEFE','#8839EF']},
  {name:'Hepburn',   colors:['#c6246a','#ec5597','#f2abca']},
  {name:'Nous',     colors:['#4682B4','#3A6E9A','#2C5F88']},
  {name:'Neon',     colors:['#B347FF','#C76BFF','#00DDFF']},
  {name:'Neon Soft', value:'neon-soft', colors:['#B347FF','#C76BFF','#00DDFF']},
  {name:'Neon Paint', value:'neon-paint', colors:['#FF2D95','#00E5FF','#FFB800']},
  {name:'Geist Contrast', value:'geist-contrast', colors:['#000000','#ffffff','#FFF175']},
  {name:'Zeus',     colors:['#FFD700','#FFBF00','#1A1A00']},
  {name:'Verdigris', value:'verdigris', colors:['#C89A5A','#0F1714','#22342C']},
];
const _VALID_THEMES=new Set((_THEMES||[]).map(t=>t.value));
const _VALID_SKINS=new Set((_SKINS||[]).map(s=>(s.value||s.name).toLowerCase()));
const _LEGACY_THEME_MAP={
  slate:{theme:'dark',skin:'slate'},
  solarized:{theme:'dark',skin:'poseidon'},
  monokai:{theme:'dark',skin:'sisyphus'},
  nord:{theme:'dark',skin:'slate'},
  oled:{theme:'dark',skin:'default'},
};
let _systemThemeMq=null;
let _onSystemThemeChange=null;
let _resolvedThemeBaseDark=false;

function _normalizeAppearance(theme,skin){
  const rawTheme=typeof theme==='string'?theme.trim().toLowerCase():'';
  const rawSkin=typeof skin==='string'?skin.trim().toLowerCase():'';
  const legacy=_LEGACY_THEME_MAP[rawTheme];
  const nextTheme=legacy?legacy.theme:(_VALID_THEMES.has(rawTheme)?rawTheme:'dark');
  const nextSkin=_VALID_SKINS.has(rawSkin)?rawSkin:(legacy?legacy.skin:'default');
  return {theme:nextTheme,skin:nextSkin};
}

// Sync <meta name="theme-color"> with the active theme's app chrome color.
// This surfaces the WebUI's exact theme background to:
//   1. Mobile Safari status bar (the prefers-color-scheme media variants in index.html
//      cover the pre-load case; this updater handles user-toggled changes mid-session).
//   2. iOS PWA / Add to Home Screen status bar.
//   3. Native WKWebView wrappers (e.g. hermes-swift-mac) that read this attribute as
//      the source of truth for AppKit chrome (tab bar, title bar, traffic-light area)
//      instead of pixel-sampling — overlay-resistant and IPC-free.
// Reading getComputedStyle(html).getPropertyValue('--sidebar') picks up the active skin
// (Default, Sienna, Sisyphus, Charizard, etc.) so each skin's distinct paint reaches
// the meta tag.
function _syncThemeColorMeta(){
  try{
    const bg=getComputedStyle(document.documentElement).getPropertyValue('--sidebar').trim();
    if(!bg) return;
    const known=document.getElementById('hermes-theme-color');
    if(known){
      known.setAttribute('content',bg);
      known.removeAttribute('media');
    }
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta=>{
      meta.setAttribute('content',bg);
      meta.removeAttribute('media');
    });
  }catch(e){}
}

function _skinKey(skin){
  return (skin&&String(skin.value||skin.name||'').toLowerCase())||'';
}

function _findSkinEntry(key){
  const normalized=String(key||'default').toLowerCase();
  return (_SKINS||[]).find(s=>_skinKey(s)===normalized)||null;
}

function _activeSkinScheme(){
  const key=(document.documentElement.dataset.skin||'default').toLowerCase();
  const skin=_findSkinEntry(key);
  const scheme=skin&&skin._extScheme;
  return scheme==='light'||scheme==='dark'?scheme:'';
}

function _effectiveThemeDark(baseIsDark){
  const skinScheme=_activeSkinScheme();
  if(skinScheme==='dark') return true;
  if(skinScheme==='light') return false;
  return !!baseIsDark;
}

function _setResolvedTheme(isDark){
  _resolvedThemeBaseDark=!!isDark;
  const effectiveDark=_effectiveThemeDark(_resolvedThemeBaseDark);
  document.documentElement.classList.toggle('dark',effectiveDark);
  const link=document.getElementById('prism-theme');
  if(!link){ _syncThemeColorMeta(); return; }
  const want=effectiveDark
    ?'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css'
    :'https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism.min.css';
  // No SRI integrity on theme CSS — jsdelivr edge nodes serve different
  // digests for the same pinned version, causing intermittent blocking (#1100).
  if(link.href!==want){ link.integrity=''; link.href=want; }
  _syncThemeColorMeta();
}

function _applyTheme(name){
  const normalized=_normalizeAppearance(name,'default');
  delete document.documentElement.dataset.theme;
  if(_systemThemeMq&&_onSystemThemeChange){
    _systemThemeMq.removeEventListener('change',_onSystemThemeChange);
    _systemThemeMq=null;
    _onSystemThemeChange=null;
  }
  if(normalized.theme==='system'){
    _systemThemeMq=window.matchMedia('(prefers-color-scheme:dark)');
    _onSystemThemeChange=()=>_setResolvedTheme(_systemThemeMq.matches);
    _setResolvedTheme(_systemThemeMq.matches);
    _systemThemeMq.addEventListener('change',_onSystemThemeChange);
    return;
  }
  _setResolvedTheme(normalized.theme==='dark');
}

function _applySkin(name){
  const key=(name||'default').toLowerCase();
  if(key==='default') delete document.documentElement.dataset.skin;
  else document.documentElement.dataset.skin=key;
  _setResolvedTheme(_resolvedThemeBaseDark);
}

function _pickTheme(name){
  const currentSkin=localStorage.getItem('hermes-skin');
  const appearance=_normalizeAppearance(name,currentSkin);
  localStorage.setItem('hermes-theme',appearance.theme);
  localStorage.setItem('hermes-skin',appearance.skin);
  _applyTheme(appearance.theme);
  _applySkin(appearance.skin);
  _syncThemePicker(appearance.theme);
  _syncSkinPicker(appearance.skin);
  const hidden=$('settingsTheme');
  if(hidden) hidden.value=appearance.theme;
  const skinHidden=$('settingsSkin');
  if(skinHidden) skinHidden.value=appearance.skin;
  if(typeof _scheduleAppearanceAutosave==='function') _scheduleAppearanceAutosave();
}

function _pickSkin(name){
  const appearance=_normalizeAppearance(localStorage.getItem('hermes-theme'),name);
  localStorage.setItem('hermes-theme',appearance.theme);
  localStorage.setItem('hermes-skin',appearance.skin);
  _applyTheme(appearance.theme);
  _applySkin(appearance.skin);
  _syncThemePicker(appearance.theme);
  _syncSkinPicker(appearance.skin);
  const hidden=$('settingsSkin');
  if(hidden) hidden.value=appearance.skin;
  const themeHidden=$('settingsTheme');
  if(themeHidden) themeHidden.value=appearance.theme;
  if(typeof _scheduleAppearanceAutosave==='function') _scheduleAppearanceAutosave();
}

function _syncThemePicker(active){
  document.querySelectorAll('#themePickerGrid .theme-pick-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.themeVal===active);
    btn.style.borderColor='';
    btn.style.boxShadow='';
  });
}

function _syncSkinPicker(active){
  document.querySelectorAll('#skinPickerGrid .skin-pick-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.skinVal===active);
    btn.style.borderColor='';
    btn.style.boxShadow='';
  });
}

function _applyFontSize(size){
  if(size&&size!=='default'){
    document.documentElement.dataset.fontSize=size;
  } else {
    delete document.documentElement.dataset.fontSize;
  }
}

function _pickFontSize(size){
  localStorage.setItem('hermes-font-size',size);
  _applyFontSize(size);
  _syncFontSizePicker(size);
  const hidden=$('settingsFontSize');
  if(hidden) hidden.value=size;
  if(typeof _scheduleAppearanceAutosave==='function') _scheduleAppearanceAutosave();
}

function _syncFontSizePicker(active){
  document.querySelectorAll('#fontSizePickerGrid .font-size-pick-btn').forEach(btn=>{
    btn.classList.toggle('active',btn.dataset.fontSizeVal===(active||'default'));
    btn.style.borderColor='';
    btn.style.boxShadow='';
  });
}

function _buildSkinPicker(activeSkin){
  const grid=$('skinPickerGrid');
  if(!grid) return;
  grid.innerHTML='';
  for(const skin of _SKINS){
    const key=(skin.value||skin.name).toLowerCase();
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='skin-pick-btn';
    btn.dataset.skinVal=key;
    btn.style.cssText='border:1px solid var(--border2);border-radius:8px;padding:8px 4px;text-align:center;cursor:pointer;background:none;transition:all .15s';
    btn.onclick=()=>_pickSkin(key);
    // Build with DOM nodes + textContent so an extension-registered skin's
    // label/name (registerHermesSkin descriptor) can never inject markup into
    // the picker. Swatch colors are already value-sanitized upstream, but set
    // them via element.style.background (not interpolated HTML) as defense in depth.
    const dotRow=document.createElement('div');
    dotRow.style.cssText='display:flex;gap:3px;justify-content:center;margin-bottom:4px';
    for(const c of (skin.colors||[])){
      const dot=document.createElement('span');
      dot.style.cssText='display:inline-block;width:10px;height:10px;border-radius:50%';
      dot.style.background=c;
      dotRow.appendChild(dot);
    }
    const labelEl=document.createElement('span');
    labelEl.style.cssText='font-size:11px;color:var(--text)';
    labelEl.textContent=skin.label||skin.name||'';
    btn.appendChild(dotRow);
    btn.appendChild(labelEl);
    grid.appendChild(btn);
  }
  _syncSkinPicker((activeSkin||'default').toLowerCase());
}

// ── Extension-registered skins (theme-registration capability) ───────────────
// Lets a trusted local extension contribute a custom skin that appears in the
// NATIVE skin picker (rather than bolting on a parallel theme switcher). An
// extension calls window.registerHermesSkin(descriptor); core validates +
// sanitizes it, injects a managed <style> rule for its CSS-variable tokens,
// appends it to _SKINS so the picker renders it, and re-applies the persisted
// selection if it was waiting on this (late-registered) skin.
//
// Security: token values are written into CSS, so every value is sanitized
// against a strict allowlist HERE, once, so all theme extensions inherit the
// guard safe-by-construction. Reserved core skin keys cannot be overwritten.
const _EXT_SKIN_STYLE_ID='hermesExtensionSkinStyles';
const _EXT_SKIN_KEYS=new Set();                 // keys we registered (for idempotent re-register)
const _RESERVED_SKIN_KEYS=new Set((_SKINS||[]).map(s=>(s.value||s.name).toLowerCase()));
// CSS custom-property names a skin is allowed to set. Mirrors the documented
// design-token contract; anything outside this set is dropped.
const _ALLOWED_SKIN_TOKENS=new Set([
  '--bg','--surface','--surface2','--surface-subtle','--text','--text2','--muted',
  '--accent','--accent2','--accent3','--accent-contrast','--accent-hover',
  '--accent-text','--accent-bg','--accent-bg-strong','--accent-rgb',
  '--border','--border2','--hover-bg','--code-bg','--code-text',
  '--sidebar','--sidebar-text','--user-bubble','--assistant-bubble',
  '--success','--warning','--danger','--info','--link'
]);
// Accept only safe color / simple numeric-with-unit values, OR a bare RGB triple
// (e.g. "0, 0, 0" for --accent-rgb, consumed inside rgba(...)). Rejects anything
// with url(), expression(), semicolons, braces, or other CSS-injection vectors.
const _SAFE_SKIN_VALUE_RE=/^(#(?:[0-9a-fA-F]{3,8})|rg(?:b|ba)\(\s*[0-9.,%\s/]+\)|hsl(?:a)?\(\s*[0-9.,%\s/deg]+\)|[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}|[a-zA-Z]{3,20}|[0-9.]+(?:px|em|rem|%)?)$/;

function _sanitizeSkinScheme(scheme){
  const value=String(scheme||'').trim().toLowerCase();
  return value==='light'||value==='dark'?value:'';
}

function _sanitizeSkinTokens(tokens){
  const out={};
  if(!tokens||typeof tokens!=='object') return out;
  for(const rawKey of Object.keys(tokens)){
    const key=String(rawKey).trim();
    if(!_ALLOWED_SKIN_TOKENS.has(key)) continue;          // unknown token → drop
    const val=String(tokens[rawKey]).trim();
    if(val.length>64) continue;                            // absurd length → drop
    if(!_SAFE_SKIN_VALUE_RE.test(val)) continue;          // unsafe value → drop
    out[key]=val;
  }
  return out;
}

function _renderExtensionSkinStyles(){
  let styleEl=document.getElementById(_EXT_SKIN_STYLE_ID);
  if(!styleEl){
    styleEl=document.createElement('style');
    styleEl.id=_EXT_SKIN_STYLE_ID;
    document.head.appendChild(styleEl);
  }
  const blocks=[];
  for(const skin of _SKINS){
    if(!skin||!skin._extToken) continue;                  // only ext-registered skins
    const key=(skin.value||skin.name).toLowerCase();
    const decls=Object.keys(skin._extToken).map(k=>`${k}:${skin._extToken[k]}`).join(';');
    if(decls) blocks.push(`:root[data-skin="${key}"]{${decls}}`);
  }
  styleEl.textContent=blocks.join('\n');
}

// Public API for extensions. Returns true on success, false if rejected.
function registerHermesSkin(descriptor){
  try{
    if(!descriptor||typeof descriptor!=='object') return false;
    const name=String(descriptor.name||'').trim();
    if(!name) return false;
    const rawVal=String(descriptor.value||name).trim().toLowerCase();
    // key must be a simple slug (safe as a data-skin attr + CSS attr selector)
    const key=rawVal.replace(/[^a-z0-9_-]/g,'');
    if(!key) return false;
    if(_RESERVED_SKIN_KEYS.has(key)) return false;        // never shadow a core skin
    const tokens=_sanitizeSkinTokens(descriptor.tokens);
    if(Object.keys(tokens).length===0) return false;      // nothing valid to apply
    const scheme=_sanitizeSkinScheme(descriptor.scheme);
    // 3 swatch colors for the picker (sanitized); fall back to accent/bg/text.
    let colors=Array.isArray(descriptor.colors)?descriptor.colors.slice(0,3):[];
    colors=colors.map(c=>String(c).trim()).filter(c=>_SAFE_SKIN_VALUE_RE.test(c));
    while(colors.length<3) colors.push(tokens['--accent']||tokens['--bg']||tokens['--text']||'#888');
    const label=String(descriptor.label||name).slice(0,40);
    const entry={name:name.slice(0,40),value:key,label,colors,_extToken:tokens,_extScheme:scheme,_extension:true};

    const existingIdx=_SKINS.findIndex(s=>(s.value||s.name).toLowerCase()===key);
    if(existingIdx>=0&&_EXT_SKIN_KEYS.has(key)){
      _SKINS[existingIdx]=entry;                           // idempotent update
    }else if(existingIdx>=0){
      return false;                                        // collides w/ a non-ext skin
    }else{
      _SKINS.push(entry);
    }
    _EXT_SKIN_KEYS.add(key);
    _VALID_SKINS.add(key);
    _renderExtensionSkinStyles();
    // Refresh the picker if it's already built.
    if(document.getElementById('skinPickerGrid')){
      _buildSkinPicker((localStorage.getItem('hermes-skin')||'default').toLowerCase());
    }
    // If the user had previously selected this (now-available) skin, apply it.
    if((localStorage.getItem('hermes-skin')||'').toLowerCase()===key){
      _applySkin(key);
    }
    return true;
  }catch(_){ return false; }
}

function applyBotName(){
  // The saved assistant name applies to the default profile only.
  // Non-default profiles use their own profile names.
  const name=assistantDisplayName();
  if(!S.session) document.title=name;
  const sidebarH1=document.querySelector('.sidebar-header h1');
  if(sidebarH1) sidebarH1.textContent=name;
  const logo=document.querySelector('.sidebar-header .logo');
  if(logo) logo.textContent=name.charAt(0).toUpperCase();
  const topbarTitle=$('topbarTitle');
  if(topbarTitle && (!S.session)) topbarTitle.textContent=name;
  const msg=$('msg');
  if(msg) msg.placeholder='Message '+name+'\u2026';
  if(typeof _applyBusyComposerPlaceholder==='function') _applyBusyComposerPlaceholder();
}

const _COMPOSER_CONTROL_TOGGLE_DEFS=[
  {key:'hide_composer_attach',label:'Attach',labelKey:'composer_control_attach',selectors:['#btnAttach'],orderSelector:'#btnAttach',orderGroup:'left'},
  {key:'hide_composer_saved_prompts',label:'Saved prompts',labelKey:'composer_control_saved_prompts',selectors:['#btnSavedPrompts'],orderSelector:'#btnSavedPrompts',orderGroup:'left'},
  {key:'hide_composer_mic',label:'Mic',labelKey:'composer_control_mic',selectors:['#btnMic'],orderSelector:'#btnMic',orderGroup:'left'},
  {key:'hide_composer_profile',label:'Profile',labelKey:'composer_control_profile',selectors:['#profileChipWrap'],orderSelector:'#profileChipWrap',orderGroup:'left'},
  {key:'hide_composer_workspace',label:'Workspace',labelKey:'composer_control_workspace',selectors:['.composer-ws-wrap','#composerMobileWorkspaceAction'],orderSelector:'.composer-ws-wrap',orderGroup:'left'},
  {key:'hide_composer_model',label:'Model',labelKey:'composer_control_model',selectors:['.composer-model-wrap','#composerMobileModelAction'],orderSelector:'.composer-model-wrap',orderGroup:'left'},
  {key:'hide_composer_reasoning',label:'Reasoning',labelKey:'composer_control_reasoning',selectors:['#composerReasoningWrap','#composerMobileReasoningAction'],orderSelector:'#composerReasoningWrap',orderGroup:'left'},
  {key:'hide_composer_context',label:'Context',labelKey:'composer_control_context',selectors:['#ctxIndicatorWrap','#composerMobileContextAction'],orderSelector:'#ctxIndicatorWrap',orderGroup:'right'},
];

const _COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS=[
  {key:'hide_composer_voice_mode',label:'Voice mode',labelKey:'composer_control_voice_mode',selectors:['#btnVoiceMode'],orderSelector:'#btnVoiceMode',orderGroup:'left'},
  {key:'hide_composer_yolo',label:'YOLO',labelKey:'composer_control_yolo',selectors:['#yoloPill'],orderSelector:'#yoloPill',orderGroup:'left'},
  {key:'hide_composer_bg_badge',label:'Background badge',labelKey:'composer_control_bg_badge',selectors:['#bgBadge'],orderSelector:'#bgBadge',orderGroup:'right'},
  {key:'hide_composer_mobile_config',label:'Mobile config',labelKey:'composer_control_mobile_config',selectors:['#composerMobileConfigBtn'],orderSelector:'#composerMobileConfigBtn',orderGroup:'left'},
  {key:'hide_composer_quota_chip',label:'Quota chip',labelKey:'composer_control_quota_chip',selectors:['#providerQuotaChip','#composerMobileQuotaAction'],orderSelector:'#providerQuotaChip',orderGroup:'left'},
  {key:'hide_composer_toolsets',label:'Toolsets',labelKey:'composer_control_toolsets',selectors:['#composerToolsetsWrap'],orderSelector:'#composerToolsetsWrap',orderGroup:'left'},
  {key:'hide_composer_status',label:'Status',labelKey:'composer_control_status',selectors:['#composerStatus'],orderSelector:'#composerStatus',orderGroup:'right'},
];

function _allComposerControlToggleDefs(){
  return _COMPOSER_CONTROL_TOGGLE_DEFS.concat(_COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS);
}

function _sanitizeComposerControlOrder(order){
  if(!Array.isArray(order)) return [];
  const allowed=new Set(_allComposerControlToggleDefs().map(def=>def.key));
  const out=[];
  order.forEach(key=>{
    if(typeof key!=='string') return;
    key=key.trim();
    if(!key||!allowed.has(key)||out.includes(key)) return;
    out.push(key);
  });
  return out;
}

function _orderedComposerControlDefs(order){
  const defs=_allComposerControlToggleDefs();
  const byKey=new Map(defs.map(def=>[def.key,def]));
  const out=[];
  _sanitizeComposerControlOrder(Array.isArray(order)?order:window._composerControlOrder).forEach(key=>{
    if(byKey.has(key)) out.push(byKey.get(key));
  });
  defs.forEach(def=>{if(!out.includes(def)) out.push(def);});
  return out;
}

function _applyComposerControlOrder(order){
  window._composerControlOrder=_sanitizeComposerControlOrder(order);
  const grouped=new Map();
  _orderedComposerControlDefs(window._composerControlOrder).forEach(def=>{
    const node=document.querySelector(def.orderSelector||def.selectors&&def.selectors[0]);
    if(!node||!node.parentNode) return;
    const parent=node.parentNode;
    if(!grouped.has(parent)) grouped.set(parent,[]);
    grouped.get(parent).push(node);
  });
  grouped.forEach((nodes,parent)=>{
    if(!nodes.length) return;
    const marker=document.createComment('composer-control-order');
    parent.insertBefore(marker,nodes[0]);
    let ref=marker;
    nodes.forEach(node=>{
      parent.insertBefore(node,ref.nextSibling);
      ref=node;
    });
    marker.remove();
  });
  if(typeof _fitComposerFooter==='function') _fitComposerFooter();
}

function _composerControlVisibilityFromSettings(settings){
  const next={};
  for(const def of _allComposerControlToggleDefs()){
    next[def.key]=!!(settings&&settings[def.key]);
  }
  return next;
}

function _setComposerControlHidden(el, hidden){
  if(!el) return;
  el.classList.toggle('composer-control-hidden', !!hidden);
  if(hidden) el.setAttribute('aria-hidden','true');
  else el.removeAttribute('aria-hidden');
}

function _applyComposerFooterVisibilitySettings(){
  const hidden=window._composerControlVisibility||{};
  for(const def of _allComposerControlToggleDefs()){
    const isHidden=!!hidden[def.key];
    for(const selector of def.selectors){
      document.querySelectorAll(selector).forEach(el=>_setComposerControlHidden(el,isHidden));
    }
  }

  const hideMic=!!hidden.hide_composer_mic;
  if(hideMic&&window._micActive&&typeof window._stopMic==='function'){
    try{window._stopMic();}catch(_){ }
  }

  const hideSavedPrompts=!!hidden.hide_composer_saved_prompts;
  const savedBtn=$('btnSavedPrompts');
  const savedPopup=$('savedPromptsPopup');
  if(hideSavedPrompts&&savedPopup){
    savedPopup.style.display='none';
    if(savedBtn) savedBtn.setAttribute('aria-expanded','false');
  }

  if(hidden.hide_composer_workspace&&typeof closeWsDropdown==='function') closeWsDropdown();
  if(hidden.hide_composer_profile&&typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(hidden.hide_composer_model&&typeof closeModelDropdown==='function') closeModelDropdown();
  if(hidden.hide_composer_reasoning&&typeof closeReasoningDropdown==='function') closeReasoningDropdown();
  if(hidden.hide_composer_toolsets&&typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  if(hidden.hide_composer_mobile_config&&typeof closeMobileComposerConfig==='function') closeMobileComposerConfig();

  // Hide the divider when all left-group buttons before it are hidden
  // Stops a lone vertical separator from appearing when attach/saved-prompts/mic/voice are all hidden.
  const _divider=document.querySelector('.composer-divider');
  if(_divider){
    const _leftBtnSelectors=['#btnAttach','#btnSavedPrompts','#btnMic','#btnVoiceMode'];
    const _allLeftHidden=_leftBtnSelectors.every(sel=>{
      const el=document.querySelector(sel);
      return !el||el.classList.contains('composer-control-hidden')||el.style.display==='none';
    });
    // Use classList.toggle directly instead of _setComposerControlHidden
    // so we don't strip the intentional aria-hidden="true" on the decorative divider
    // when buttons are visible (Greptile feedback).
    _divider.classList.toggle('composer-control-hidden',_allLeftHidden);
  }
}

function _applyTitlebarProfileVisibility(){
  const btn=$('titlebarProfileBtn');
  if(!btn) return;
  btn.style.display=window._showTitlebarProfile?'':'none';
}

function _mirrorSpeechSettingsFromServer(s){
  if(!s||typeof s!=='object') return;
  const persistedSpeechKeys = new Set(
    Array.isArray(s.persisted_speech_keys) ? s.persisted_speech_keys : []
  );
  const hasServerValue=(settingKey)=>persistedSpeechKeys.has(settingKey);
  const defaults={
    tts_enabled:false,
    tts_auto_read:false,
    tts_engine:'browser',
    tts_voice:'',
    tts_rate:1,
    tts_pitch:1,
    voice_mode_button:false,
    voice_continuous:false,
    voice_silence_ms:1800,
    raw_audio_mode:false,
  };
  const cachedValue=(storageKey)=>{
    try{return localStorage.getItem(storageKey);}catch(_){return null;}
  };
  const boolValue=(value)=>value===true||value==='true';
  const resolveBool=(settingKey,storageKey)=>{
    const server=hasServerValue(settingKey)?s[settingKey]:defaults[settingKey];
    const cached=cachedValue(storageKey);
    if(!hasServerValue(settingKey)&&cached!==null){
      return boolValue(cached);
    }
    return boolValue(server);
  };
  const resolveScalar=(settingKey,storageKey)=>{
    const server=hasServerValue(settingKey)?s[settingKey]:defaults[settingKey];
    const cached=cachedValue(storageKey);
    if(!hasServerValue(settingKey)&&cached!==null){
      return cached;
    }
    return server;
  };
  const boolKeys=[
    ['tts_enabled','hermes-tts-enabled'],
    ['tts_auto_read','hermes-tts-auto-read'],
    ['voice_mode_button','hermes-voice-mode-button'],
    ['voice_continuous','hermes-voice-continuous'],
  ];
  boolKeys.forEach(([settingKey,storageKey])=>{
    if(hasServerValue(settingKey)){
      try{localStorage.setItem(storageKey,resolveBool(settingKey,storageKey)?'true':'false');}catch(_){}
    }
  });
  [
    ['tts_engine','hermes-tts-engine'],
    ['tts_voice','hermes-tts-voice'],
    ['tts_rate','hermes-tts-rate'],
    ['tts_pitch','hermes-tts-pitch'],
    ['voice_silence_ms','hermes-voice-silence-ms'],
  ].forEach(([settingKey,storageKey])=>{
    if(hasServerValue(settingKey)){
      try{localStorage.setItem(storageKey,String(resolveScalar(settingKey,storageKey)));}catch(_){}
    }
  });
  if(hasServerValue('raw_audio_mode')){
    const rawAudioMode=resolveBool('raw_audio_mode','hermes-raw-audio-mode');
    if(typeof window._applyRawAudioModePreference==='function'){
      window._applyRawAudioModePreference(rawAudioMode);
    }else{
      try{localStorage.setItem('hermes-raw-audio-mode',rawAudioMode?'true':'false');}catch(_){}
    }
  }
}

export {
  _COMPOSER_CONTROL_TOGGLE_DEFS,
  _COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS,
  _LEGACY_THEME_MAP,
  _VALID_SKINS,
  _applyComposerControlOrder,
  _applyComposerFooterVisibilitySettings,
  _applyFontSize,
  _applySkin,
  _applyTheme,
  _applyTitlebarProfileVisibility,
  _buildSkinPicker,
  _composerControlVisibilityFromSettings,
  _mirrorSpeechSettingsFromServer,
  _normalizeAppearance,
  _orderedComposerControlDefs,
  _pickFontSize,
  _pickTheme,
  _sanitizeComposerControlOrder,
  _syncFontSizePicker,
  _syncSkinPicker,
  _syncThemePicker,
  applyBotName,
  registerHermesSkin,
};
