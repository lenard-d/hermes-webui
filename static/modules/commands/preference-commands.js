async function cmdUsage(){
  const next=!window._showTokenUsage;
  window._showTokenUsage=next;
  try{
    await api('/api/settings',{method:'POST',body:JSON.stringify({show_token_usage:next})});
  }catch(_){}
  const checkbox=$('settingsShowTokenUsage');
  if(checkbox)checkbox.checked=next;
  renderMessages();
  showToast(next?t('token_usage_on'):t('token_usage_off'));
}

async function cmdTheme(args){
  const themes=['system','dark','light'];
  const skins=(_SKINS||[]).map(s=>(s.value||s.name).toLowerCase());
  const legacyThemes=Object.keys(_LEGACY_THEME_MAP||{});
  const val=(args||'').toLowerCase().trim();
  if(themes.includes(val)||legacyThemes.includes(val)){
    const appearance=_normalizeAppearance(
      val,
      legacyThemes.includes(val)?null:localStorage.getItem('hermes-skin')
    );
    localStorage.setItem('hermes-theme',appearance.theme);
    localStorage.setItem('hermes-skin',appearance.skin);
    _applyTheme(appearance.theme);
    _applySkin(appearance.skin);
    try{
      await api('/api/settings',{method:'POST',body:JSON.stringify({theme:appearance.theme,skin:appearance.skin})});
    }catch(_){}
    const themeSelect=$('settingsTheme');
    if(themeSelect)themeSelect.value=appearance.theme;
    const skinSelect=$('settingsSkin');
    if(skinSelect)skinSelect.value=appearance.skin;
    if(typeof _syncThemePicker==='function')_syncThemePicker(appearance.theme);
    if(typeof _syncSkinPicker==='function')_syncSkinPicker(appearance.skin);
    showToast(t('theme_set')+appearance.theme+(legacyThemes.includes(val)?` + ${appearance.skin}`:''));
    return;
  }
  if(skins.includes(val)){
    const appearance=_normalizeAppearance(localStorage.getItem('hermes-theme'),val);
    localStorage.setItem('hermes-theme',appearance.theme);
    localStorage.setItem('hermes-skin',appearance.skin);
    _applyTheme(appearance.theme);
    _applySkin(appearance.skin);
    try{
      await api('/api/settings',{method:'POST',body:JSON.stringify({theme:appearance.theme,skin:appearance.skin})});
    }catch(_){}
    const skinSelect=$('settingsSkin');
    if(skinSelect)skinSelect.value=appearance.skin;
    const themeSelect=$('settingsTheme');
    if(themeSelect)themeSelect.value=appearance.theme;
    if(typeof _syncThemePicker==='function')_syncThemePicker(appearance.theme);
    if(typeof _syncSkinPicker==='function')_syncSkinPicker(appearance.skin);
    showToast(t('theme_set')+appearance.skin);
    return;
  }
  showToast(t('theme_usage')+themes.join('|')+' | '+skins.join('|')+' | legacy:'+legacyThemes.join('|'));
}

function cmdReasoning(args){
  const arg=(args||'').trim().toLowerCase();
  const brain='\uD83E\uDDE0';
  const efforts=['none','minimal','low','medium','high','xhigh','max'];
  const formatStatus=status=>{
    const visibility=(status&&status.show_reasoning===false)?'off':'on';
    const effort=(status&&status.reasoning_effort)||'default';
    return brain+' Reasoning effort: '+effort+' \u00B7 display: '+visibility
      +'  |  /reasoning show|hide|none|minimal|low|medium|high|xhigh|max';
  };
  if(!arg){
    const query=(typeof _reasoningEffortQuery==='function')?_reasoningEffortQuery():'';
    api('/api/reasoning'+query).then(status=>showToast(formatStatus(status)))
      .catch(()=>showToast(brain+' /reasoning — status unavailable'));
    return true;
  }
  if(arg==='show'||arg==='on'||arg==='hide'||arg==='off'){
    const on=(arg==='show'||arg==='on');
    window._showThinking=on;
    if(!on&&typeof removeThinking==='function')removeThinking();
    if(typeof renderMessages==='function')renderMessages();
    api('/api/reasoning',{method:'POST',body:JSON.stringify({display:arg})}).catch(()=>{});
    api('/api/settings',{method:'POST',body:JSON.stringify({show_thinking:on})}).catch(()=>{});
    showToast(brain+' Thinking blocks: '+(on?'on':'off')+' (saved)');
    return true;
  }
  if(efforts.includes(arg)){
    api('/api/reasoning',{method:'POST',body:JSON.stringify({effort:arg})})
      .then(st=>{
        const eff=(st&&st.reasoning_effort)||arg;
        showToast(brain+' Reasoning effort: '+eff+' (saved; applies to next turn)');
        if(typeof _applyReasoningChip==='function')_applyReasoningChip(eff, st||{});
      })
      .catch(error=>{
        showToast(brain+' Failed to set effort: '+(error&&error.message?error.message:arg));
      });
    return true;
  }
  showToast('Unknown argument: '+arg+' — use show|hide|'+efforts.join('|'));
  return true;
}

function cmdVoice(){
  const mic=document.getElementById('btnMic');
  const visible=!!(
    mic
    &&mic.style.display!=='none'
    &&!mic.disabled
    &&!mic.classList.contains('composer-control-hidden')
    &&mic.getAttribute('aria-hidden')!=='true'
    &&(!window.getComputedStyle||window.getComputedStyle(mic).display!=='none')
  );
  if(visible){try{mic.click();return;}catch(_){}}
  showToast(t('cmd_voice_use_mic'));
}

export {cmdReasoning,cmdTheme,cmdUsage,cmdVoice};
