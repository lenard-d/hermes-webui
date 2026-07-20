function renderModelDropdown(){
  const opts=arguments[0]||{};
  const dd=$(opts.dropdownId||'composerModelDropdown');
  const sel=$(opts.selectId||'modelSelect');
  if(!dd||!sel) return;
  if(typeof _deduplicateModelPickerOptions==='function') _deduplicateModelPickerOptions(sel,sel.value);
  // Whether the search input should auto-grab focus on (re-)render. Default true
  // preserves the composer picker's behavior exactly; the settings picker passes
  // false on coarse-pointer devices so opening it doesn't pop the mobile keyboard.
  const _autoFocusSearch=opts.autoFocusSearch!==false;
  const selectFromDropdown=typeof opts.selectModel==='function'
    ? opts.selectModel
    : (value,provider)=>selectModelFromDropdown(value,provider);
  const closeDropdown=typeof opts.closeDropdown==='function'
    ? opts.closeDropdown
    : closeModelDropdown;
  // Group(s) that must render OPEN even though they aren't the selected group —
  // set when the user expands a group's overflow via "Show more" so a later full
  // re-render doesn't re-collapse it (_groupOpenState is rebuilt per render, so
  // this cross-render intent persists on a global). Resolved as a function-local
  // so renderModelDropdown stays self-contained when eval'd in isolation (the
  // #3691 node test driver evals the function body without module scope).
  const _forceOpenGroups=(()=>{
    const _g=(typeof window!=='undefined')?window:(typeof globalThis!=='undefined'?globalThis:{});
    const key=opts.forceOpenKey||'composer';
    if(!_g.__modelGroupForceOpenByPicker) _g.__modelGroupForceOpenByPicker={};
    if(!_g.__modelGroupForceOpenByPicker[key]) _g.__modelGroupForceOpenByPicker[key]=new Set();
    return _g.__modelGroupForceOpenByPicker[key];
  })();
  const _modelData=[];
  const _groupMeta=new Map();
  const _groupOrder=[];
  const _badgeMap=window._configuredModelBadges||{};
  const _ensureGroupMeta=(groupKey,groupLabel,providerId,optgroup)=>{
    if(!_groupMeta.has(groupKey)){
      _groupMeta.set(groupKey,{
        key:groupKey,
        label:groupLabel||'',
        providerId:providerId||'',
        optgroup:optgroup||null,
        modelsEndpointError:null,
        modelCount:0,
        hiddenCount:0,
        endpointErrorOnly:false,
      });
      _groupOrder.push(groupKey);
    }
    return _groupMeta.get(groupKey);
  };
  const _vendorPrefix=(rawId)=>{
    const stripped=String(rawId||'').replace(/^@([^:]+:)+/,'');
    const slash=stripped.indexOf('/');
    return slash>0?stripped.slice(0,slash):'';
  };
  const SUB_GROUP_PROVIDERS=new Set(['openrouter','nous']);
  const SUB_GROUP_MIN_MODELS=8;
  for(const child of Array.from(sel.children)){
    if(child.tagName==='OPTGROUP'){
      const providerId=child.dataset&&child.dataset.provider?child.dataset.provider:'';
      const groupKey=providerId||child.label||`group-${_groupOrder.length}`;
      const groupMeta=_ensureGroupMeta(groupKey,child.label||'',providerId,child);
      let modelsEndpointError=null;
      if(child.dataset&&child.dataset.modelsEndpointError){
        try{ modelsEndpointError=JSON.parse(child.dataset.modelsEndpointError); }catch(_e){ modelsEndpointError=null; }
      }
      groupMeta.modelsEndpointError=modelsEndpointError;
      for(const opt of Array.from(child.children)){
        const rawValue=String(opt.value||'');
        const displayName=rawValue.startsWith('@custom:')
          ? getModelLabel(rawValue)
          : (opt.textContent||getModelLabel(rawValue));
        const entry={value:opt.value,name:esc(displayName),id:esc(opt.value),group:child.label||'',groupKey,providerId,modelsEndpointError,badge:_getConfiguredModelBadge(opt.value,_badgeMap,providerId),hiddenByDefault:false};
        _modelData.push(entry);
        groupMeta.modelCount++;
      }
      for(const overflowModel of _readModelOverflowData(child)){
        const displayName=overflowModel.id.startsWith('@custom:')
          ? getModelLabel(overflowModel.id)
          : (overflowModel.label||getModelLabel(overflowModel.id));
        _modelData.push({
          value:overflowModel.id,
          name:esc(displayName),
          id:esc(overflowModel.id),
          group:child.label||'',
          groupKey,
          providerId,
          modelsEndpointError,
          badge:_getConfiguredModelBadge(overflowModel.id,_badgeMap,providerId),
          hiddenByDefault:true,
        });
        groupMeta.modelCount++;
        groupMeta.hiddenCount++;
      }
      if(modelsEndpointError && !child.children.length && !groupMeta.hiddenCount){
        groupMeta.endpointErrorOnly=true;
        _modelData.push({value:`__models_endpoint_error__:${providerId||child.label||''}`,name:'',id:'',group:child.label||'',groupKey,providerId,modelsEndpointError,endpointErrorOnly:true});
      }
    }
    if(child.tagName==='OPTION'){
      const groupKey='__ungrouped__';
      _ensureGroupMeta(groupKey,'','',null);
      const rawValue=String(child.value||'');
      const displayName=rawValue.startsWith('@custom:')
        ? getModelLabel(rawValue)
        : (child.textContent||getModelLabel(rawValue));
      _modelData.push({value:child.value,name:esc(displayName),id:esc(child.value),group:'',groupKey,providerId:'',badge:_getConfiguredModelBadge(child.value,_badgeMap),hiddenByDefault:false});
      _groupMeta.get(groupKey).modelCount++;
    }
  }
  for(const [modelId,badge] of Object.entries(_badgeMap)){
    if(_isEquivalentConfiguredModelEntry(modelId,badge,_modelData)) continue;
    _modelData.push({
      value:modelId,
      name:esc(getModelLabel(modelId)),
      id:esc(modelId),
      group:'',
      badge,
    });
  }
  // Create search input FIRST before filterModels definition
  const _scopeNote=document.createElement('div');
  _scopeNote.className='model-scope-note';
  _scopeNote.textContent=opts.scopeNoteText||(t('model_scope_advisory')||'Applies to this conversation from your next message.');
  const _searchRow=document.createElement('div');
  _searchRow.className='model-search-row';
  _searchRow.innerHTML=`<input class="model-search-input" type="text" placeholder="${esc(t('model_search_placeholder')||'Search models…')}" spellcheck="false" autocomplete="off"><button class="model-search-clear" title="Clear search">${li('x',10)}</button>`;
  const _si=_searchRow.querySelector('.model-search-input');
  const _sc=_searchRow.querySelector('.model-search-clear');
  // Create custom model section elements
  const _custSep=document.createElement('div');
  _custSep.className='model-group model-custom-sep';
  _custSep.textContent=t('model_custom_label')||'Custom model ID';
  const _custRow=document.createElement('div');
  _custRow.className='model-custom-row';
  _custRow.innerHTML=`<input class="model-custom-input" type="text" placeholder="${esc(t('model_custom_placeholder')||'e.g. openai/gpt-5.4')}" spellcheck="false" autocomplete="off"><button class="model-custom-btn" title="Use this model">${li('plus',12)}</button>`;
  const _ci=_custRow.querySelector('.model-custom-input');
  const _cb=_custRow.querySelector('.model-custom-btn');
  const _configuredRank=(badge)=>{
    if(!badge) return Number.POSITIVE_INFINITY;
    if(badge.role==='primary') return 0;
    if(badge.role==='fallback'){
      const m=String(badge.label||'').match(/fallback\s+(\d+)/i);
      return m?Number(m[1]):999;
    }
    return 500;
  };
  const _selectedModelState=(typeof _modelStateForSelect==='function')?_modelStateForSelect(sel,sel.value):{model:sel&&sel.value||'',model_provider:null};
  const _modelProviderForSelectedBadge=(m)=>{
    const _provider=String((m&&m.providerId)||(m&&m.badge&&m.badge.provider)||((typeof _providerFromModelValue==='function')?_providerFromModelValue(m&&m.value):'')||'').trim();
    return (_provider&&_provider!=='default')?_provider:null;
  };
  const _isSelectedModelRow=(m)=>String((m&&m.value)||'')===String((_selectedModelState&&_selectedModelState.model)||(sel&&sel.value)||'')&&String(_modelProviderForSelectedBadge(m)||'')===String((_selectedModelState&&_selectedModelState.model_provider)||'');
  const _selectedModelBadge=(m)=>_isSelectedModelRow(m)
    ?`<span class="model-opt-badge model-opt-badge--selected">${esc(t('model_badge_selected')||'Selected')}</span>`
    :'';
  const _renderProviderEndpointHint=(entry,parent)=>{
    if(!entry||!entry.label||!entry.modelsEndpointError) return;
    const hint=document.createElement('div');
    hint.className='model-provider-hint';
    hint.textContent=entry.modelsEndpointError.message||'Models endpoint could not be reached for this provider.';
    (parent||dd).appendChild(hint);
  };
  // Build a single model-option row (mirrors the main render loop's row markup),
  // used both by the main render and by the in-place overflow reveal below.
  const _buildModelRow=(m,withProviderChip)=>{
    const row=document.createElement('div');
    row.className='model-opt'+(_isSelectedModelRow(m)?' active':'');
    const badgeHtml=m.badge?`<span class="model-opt-badge model-opt-badge--${esc(m.badge.role||'configured')}">${esc(m.badge.label||'Configured')}</span>`:'';
    const _plainGroup=m.group?String(m.group).replace(/\s*\(\d+\s+of\s+\d+\)\s*$/,''):'';
    const providerChip=(_plainGroup&&withProviderChip)?`<span class="model-opt-provider">${esc(_plainGroup)}</span>`:'';
    row.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">${esc(m.name)}</span>${badgeHtml}${_selectedModelBadge(m)}${providerChip}</div><span class="model-opt-id">${esc(m.id)}</span>`;
    row.onclick=()=>selectFromDropdown(m.value,m.providerId||(m.badge&&m.badge.provider)||null);
    return row;
  };
  const _expandOverflowGroup=(groupMetaEntry)=>{
    if(!groupMetaEntry||!groupMetaEntry.optgroup) return;
    const og=groupMetaEntry.optgroup;
    const groupKey=groupMetaEntry.key;
    const extraModels=_readModelOverflowData(og);
    // Nothing to reveal — no overflow tail advertised.
    if(!extraModels.length) return;
    // Append the overflow models to the source <select> so the dropdown's state
    // stays the source of truth (search, re-render, selection all see them).
    // NOTE: guard on extraModels.length (above), NOT on the append return value —
    // _appendOverflowOptionsToGroup returns the count of NEWLY-created <option>s
    // and returns 0 (while still clearing dataset.extraModels) when every overflow
    // model already existed as an option. Bailing on a 0 return would leave those
    // already-present-but-hidden rows unrevealed and the expander dead (#bug3).
    _appendOverflowOptionsToGroup(og,extraModels);
    // Full re-render fallback — the proven path. Used when the in-place reveal
    // can't run (minimal/headless DOM without CSS.escape/rAF/insertBefore, or any
    // unexpected failure). Produces the same end state: overflow appended,
    // expander gone, search term reapplied.
    const _fullReRender=()=>{
      const _term=(_si&&_si.value)||'';
      renderModelDropdown(opts);
      const ns=dd.querySelector('.model-search-input');
      if(ns){ ns.value=_term; (ns._listeners&&ns._listeners.input)?ns._listeners.input():ns.dispatchEvent(new Event('input')); }
    };
    // IN-PLACE reveal: build the newly-revealed rows and insert them directly into
    // the existing group wrapper (before the "Show more" expander), then remove
    // the expander. No full re-render — so the group stays open, every other
    // group keeps its collapsed/open state, and the scroll position is preserved.
    // The user lands on the first new row. Falls back to a full re-render if the
    // runtime lacks the DOM APIs this needs.
    const _canInPlace = typeof CSS!=='undefined' && CSS && typeof CSS.escape==='function'
      && typeof dd.querySelector==='function';
    if(!_canInPlace){ _fullReRender(); return; }
    let wrap, moreEl;
    try{
      wrap=dd.querySelector(`.model-group-body[data-group="${CSS.escape(groupKey)}"]`);
      moreEl=wrap?wrap.querySelector('.model-opt-more'):null;
    }catch(_){ _fullReRender(); return; }
    if(!wrap||!moreEl||typeof wrap.insertBefore!=='function'){
      _fullReRender();
      return;
    }
    try{
      const _plainLabel=String(groupMetaEntry.label||'').replace(/\s*\(\d+\s+of\s+\d+\)\s*$/,'');
      const _alreadyShown=new Set(Array.from(wrap.querySelectorAll('.model-opt .model-opt-id')).map(el=>el.textContent));
      let firstNewRow=null;
      for(const m of extraModels){
        if(!m||!m.id) continue;
        if(_alreadyShown.has(esc(m.id))) continue;
        const row=_buildModelRow({value:m.id,name:m.label||m.id,id:m.id,group:_plainLabel,groupKey,providerId:(og.dataset&&og.dataset.provider)||''},false);
        wrap.insertBefore(row,moreEl);
        if(!firstNewRow) firstNewRow=row;
      }
      // Sync the in-memory model data so a later _filterModels() re-render (e.g.
      // after a search is typed and cleared) keeps the group fully expanded
      // instead of snapping back to the capped view + a fresh "Show more". The
      // overflow rows were just appended to the live <select>, so flip their
      // _modelData entries to no-longer-hidden and zero the group's hidden count.
      for(const _md of _modelData){
        if(_md && _md.groupKey===groupKey && _md.hiddenByDefault){
          _md.hiddenByDefault=false;
        }
      }
      if(groupMetaEntry && typeof groupMetaEntry.hiddenCount==='number'){
        groupMetaEntry.hiddenCount=0;
      }
      // The group is now fully expanded — drop the "Show more" expander, and bump
      // the heading count to the full total. Also force the group OPEN (the user
      // just asked to see more of it) regardless of any prior collapsed state.
      moreEl.remove();
      wrap.style.display='';
      _forceOpenGroups.add(groupKey);
      const heading=wrap.previousElementSibling;
      if(heading&&heading.classList&&heading.classList.contains('model-group')){
        const _total=wrap.querySelectorAll('.model-opt').length;
        heading.textContent=_total>1?`${_plainLabel} (${_total})`:_plainLabel;
        heading.classList.add('collapsible','open');
      }
      // Scroll so the first newly-revealed row sits near the top of the dropdown
      // viewport — the user asked to "land on the new models" after Show more,
      // not be reset to the top of the list and not have it jump unpredictably.
      if(firstNewRow && typeof firstNewRow.offsetTop==='number' && typeof requestAnimationFrame==='function'){
        const _targetTop=Math.max(0,firstNewRow.offsetTop-48);
        const _doScroll=()=>{ try{ dd.scrollTop=_targetTop; }catch(_){} };
        _doScroll();                                   // immediate
        requestAnimationFrame(()=>{ _doScroll(); requestAnimationFrame(_doScroll); });
        if(typeof setTimeout==='function') setTimeout(_doScroll,80); // after any refocus settles
      }
    }catch(_err){
      // Any unexpected DOM failure — fall back to the proven full re-render so
      // the overflow still gets revealed.
      _fullReRender();
    }
  };
  // Collapsible group state — persists across _filterModels calls
  const _groupOpenState={};
  let _prevHasSearch=false;  // tracks search->empty transition to reset open-state
  let _groupWrappers={};
  // The group that owns the currently-selected model. Groups start COLLAPSED by
  // default (#4279); the selected provider's group is the one exception so the
  // user always sees their active model without expanding anything. (#4279 + UX)
  const _selectedGroupKey=(()=>{
    const _selVal=String((sel&&sel.value)||'');
    if(!_selVal) return null;
    const _hit=_modelData.find(m=>m&&!m.endpointErrorOnly&&_isSelectedModelRow(m)) || _modelData.find(m=>m&&!m.endpointErrorOnly&&String(m.value||'')===_selVal);
    return _hit?_hit.groupKey:null;
  })();
  const _makeModelRow=(m,shouldRenderHeading)=>{
    const row=document.createElement('div');
    row.className='model-opt'+(_isSelectedModelRow(m)?' active':'');
    const badgeHtml=m.badge?`<span class="model-opt-badge model-opt-badge--${esc(m.badge.role||'configured')}">${esc(m.badge.label||'Configured')}</span>`:'';
    const _plainGroup=m.group?String(m.group).replace(/\s*\(\d+\s+of\s+\d+\)\s*$/,''):'';
    const _underOwnHeading=shouldRenderHeading&&!!(m.groupKey&&_groupWrappers[m.groupKey]);
    const providerChip=(_plainGroup&&!_underOwnHeading)?`<span class="model-opt-provider">${esc(_plainGroup)}</span>`:'';
    row.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">${esc(m.name)}</span>${badgeHtml}${_selectedModelBadge(m)}${providerChip}</div><span class="model-opt-id">${esc(m.id)}</span>`;
    row.onclick=()=>selectFromDropdown(m.value,m.providerId||(m.badge&&m.badge.provider)||null);
    return row;
  };
  const _filterModels=(term)=>{
    // Preserve focus across the re-render if the search input already had it — so a
    // touch user typing a query (where autoFocusSearch is suppressed to avoid the
    // initial keyboard pop) doesn't lose focus mid-word on each keystroke re-render.
    const _hadFocus=(typeof document!=='undefined')&&document.activeElement===_si;
    term=term.trim().toLowerCase();
    const hasSearch=!!term;
    // On a fresh search, expand all groups so every match is visible (#collapse).
    if(hasSearch) for(const k in _groupOpenState) _groupOpenState[k]=true;
    // When a search is CLEARED (search -> empty), reset the per-group open state
    // so the collapsed-except-selected default re-applies — otherwise every group
    // the search auto-expanded would stay open, defeating the collapse UX. Groups
    // the user explicitly expanded via "Show more" (_forceOpenGroups) and the
    // selected group remain open through the defaulting logic below.
    else if(_prevHasSearch){ for(const k in _groupOpenState) delete _groupOpenState[k]; }
    _prevHasSearch=hasSearch;
    const found=new Set();
    for(const m of _modelData){
      const name=m.name.toLowerCase();
      const id=m.id.toLowerCase();
      if(name.includes(term)||id.includes(term)){
        found.add(m.value);
      }
    }
    const matches=(m)=>!term||found.has(m.value);
    const configuredCandidates=_modelData
      .filter(m=>m.badge&&matches(m));
    const configuredBySemanticKey=new Map();
    const _configuredProviderKey=(m)=>String((m&&m.badge&&m.badge.provider)||_providerFromModelValue(m&&m.value)||'').toLowerCase();
    const _configuredModelKey=(m)=>_normalizeConfiguredModelKey(m&&m.value||'');
    const _configuredDisplayPriority=(m)=>{
      // Prefer plain IDs over provider-qualified aliases for readability.
      const v=String((m&&m.value)||'');
      if(v.startsWith('@')) return 0;
      if(v.includes('/')) return 1;
      return 2;
    };
    for(const candidate of configuredCandidates){
      const semanticKey=`${_configuredProviderKey(candidate)}::${_configuredModelKey(candidate)}`;
      const existing=configuredBySemanticKey.get(semanticKey);
      if(!existing){
        configuredBySemanticKey.set(semanticKey,candidate);
        continue;
      }
      const candidatePriority=_configuredDisplayPriority(candidate);
      const existingPriority=_configuredDisplayPriority(existing);
      if(candidatePriority>existingPriority){
        configuredBySemanticKey.set(semanticKey,candidate);
      }
    }
    const configuredModels=[...configuredBySemanticKey.values()]
      .sort((a,b)=>{
        const configuredRankA=_configuredRank(a.badge);
        const configuredRankB=_configuredRank(b.badge);
        if(configuredRankA!==configuredRankB) return configuredRankA-configuredRankB;
        return a.name.localeCompare(b.name);
      });
    const configuredIds=new Set(configuredModels.map(m=>m.value));
    const configuredSemanticKeys=new Set(configuredModels.map(m=>`${_configuredProviderKey(m)}::${_configuredModelKey(m)}`));
    const _effectiveHiddenCount=(groupKey)=>_modelData.filter(m=>
      m.groupKey===groupKey
      && m.hiddenByDefault
      && !configuredSemanticKeys.has(`${_configuredProviderKey(m)}::${_configuredModelKey(m)}`)
    ).length;
    dd.innerHTML='';
    dd.appendChild(_scopeNote);
    dd.appendChild(_searchRow);
    dd.appendChild(_custSep);
    dd.appendChild(_custRow);
    if(configuredModels.length){
      const configuredHeading=document.createElement('div');
      configuredHeading.className='model-group';
      configuredHeading.textContent=t('model_group_configured')||'Configured';
      dd.appendChild(configuredHeading);
      // 为了显示原始ID，建立 badgeKeyMap: badge对象->原始key
      const badgeKeyMap = new Map();
      for(const [k, v] of Object.entries(_badgeMap)){
        badgeKeyMap.set(v, k);
      }
      for(const m of configuredModels){
        const row=document.createElement('div');
        row.className='model-opt'+(_isSelectedModelRow(m)?' active':'');
        let badgeLabel = '';
        let modelName = m.name;
        if (m.badge) {
          // 直接用badge的原始key（即config.yaml里的ID）
          const rawId = badgeKeyMap.get(m.badge) || m.value || m.badge.label || 'Configured';
          badgeLabel = rawId;
          modelName = rawId; // model-opt-name直接用原始ID
          if(m.badge.provider){
            const providerName=m.badge.provider.replace(/^custom:/,'').split('/')[0];
            badgeLabel += ` (${providerName})`;
          }
        }
        const badgeHtml=m.badge?`<span class="model-opt-badge model-opt-badge--${esc(m.badge.role||'configured')}">${esc(badgeLabel)}</span>`:'';
        row.innerHTML=`<div class="model-opt-top"><span class="model-opt-name">${esc(modelName)}</span>${badgeHtml}${_selectedModelBadge(m)}</div><span class="model-opt-id">${esc(m.id)}</span>`;
        row.onclick=()=>selectFromDropdown(m.value,(m.badge&&m.badge.provider)||m.providerId||null);
        dd.appendChild(row);
      }
    }
    for(const groupKey of _groupOrder){
      const meta=_groupMeta.get(groupKey);
      if(!meta) continue;
      const hiddenCount=_effectiveHiddenCount(groupKey);
      const groupRows=_modelData.filter(m=>
        m.groupKey===groupKey
        && !configuredIds.has(m.value)
        && !m.endpointErrorOnly
        && matches(m)
        && (!m.hiddenByDefault || !!term)
      );
      const shouldRenderHeading=!!meta.label&&(groupRows.length||meta.endpointErrorOnly||(!term&&hiddenCount));
      if(shouldRenderHeading){
        const heading=document.createElement('div');
        heading.className='model-group';
        // When COLLAPSED (hiddenCount>0) keep the backend-decorated label verbatim
        // ("Nous (2 of 4)") so the overflow count shows. When EXPANDED, strip that
        // decoration and append the rendered-row count, otherwise the heading reads
        // "Nous (2 of 4) (4)" (double count). Count rendered rows, not modelCount,
        // so hoisted-configured models aren't double-counted. (#3691)
        const count=hiddenCount?0:groupRows.length;
        const _plainLabel=String(meta.label||'').replace(/\s*\(\d+\s+of\s+\d+\)\s*$/,'');
        heading.textContent=count>1?`${_plainLabel} (${count})`:meta.label;
        dd.appendChild(heading);
        const wrapper=document.createElement('div');
        wrapper.className='model-group-body';
        wrapper.dataset.group=groupKey;
        // A group carrying a provider endpoint-error hint must stay visible by
        // default — otherwise the "models endpoint unreachable" warning is hidden
        // inside a collapsed body and the user never sees it. (#2540 surface)
        const _hasEndpointError=!!(meta&&(meta.modelsEndpointError||meta.endpointErrorOnly));
        if(hasSearch) _groupOpenState[groupKey]=true;
        else if(_forceOpenGroups.has(groupKey)) _groupOpenState[groupKey]=true;
        else if(_hasEndpointError) _groupOpenState[groupKey]=true;
        else if(!(groupKey in _groupOpenState)) _groupOpenState[groupKey]=(groupKey===_selectedGroupKey);
        if(!_groupOpenState[groupKey]) wrapper.style.display='none';
        else heading.classList.add('open');
        heading.classList.add('collapsible');
        dd.appendChild(wrapper);
        _groupWrappers[groupKey]=wrapper;
        // Render the provider endpoint-error hint inside the collapsible group
        // so it collapses/expands with it (the group is force-opened above when
        // an error is present, so the hint stays visible by default).
        _renderProviderEndpointHint(meta,wrapper);
        heading.addEventListener('click',(e)=>{
          e.stopPropagation();
          const w=dd.querySelector(`.model-group-body[data-group="${CSS.escape(groupKey)}"]`);
          if(!w) return;
          const closed=w.style.display==='none';
          w.style.display=closed?'':'none';
          _groupOpenState[groupKey]=closed;
          // Keep the cross-render force-open intent in sync with manual toggles:
          // collapsing a previously overflow-expanded group should let it
          // re-collapse on the next render too.
          if(closed) _forceOpenGroups.add(groupKey); else _forceOpenGroups.delete(groupKey);
          heading.classList.toggle('open',closed);
        });
        const useSubGroups=(
          SUB_GROUP_PROVIDERS.has(meta.providerId) &&
          groupRows.length>=SUB_GROUP_MIN_MODELS
        );
        if(useSubGroups){
          const byPrefix=new Map();
          for(const m of groupRows){
            const pfx=_vendorPrefix(m.value)||'other';
            if(!byPrefix.has(pfx)) byPrefix.set(pfx,[]);
            byPrefix.get(pfx).push(m);
          }
          const sorted=[...byPrefix.entries()].sort((a,b)=>{
            if(a[0]==='other') return 1;
            if(b[0]==='other') return -1;
            return b[1].length-a[1].length;
          });
          for(const [pfx,pfxRows] of sorted){
            if(pfxRows.length>=2){
              const subKey=`${groupKey}::${pfx}`;
              if(!(subKey in _groupOpenState)) _groupOpenState[subKey]=true;
              if(hasSearch) _groupOpenState[subKey]=true;
              const subHeading=document.createElement('div');
              subHeading.className='model-group sub collapsible';
              subHeading.dataset.group=subKey;
              if(_groupOpenState[subKey]) subHeading.classList.add('open');
              subHeading.textContent=pfx;
              const subWrapper=document.createElement('div');
              subWrapper.className='model-group-body sub';
              subWrapper.dataset.group=subKey;
              if(!_groupOpenState[subKey]) subWrapper.style.display='none';
              subHeading.addEventListener('click',(e)=>{
                e.stopPropagation();
                const closed=subWrapper.style.display==='none';
                subWrapper.style.display=closed?'':'none';
                _groupOpenState[subKey]=closed;
                subHeading.classList.toggle('open',closed);
              });
              wrapper.appendChild(subHeading);
              wrapper.appendChild(subWrapper);
              for(const m of pfxRows) subWrapper.appendChild(_makeModelRow(m,shouldRenderHeading));
            } else {
              for(const m of pfxRows) wrapper.appendChild(_makeModelRow(m,shouldRenderHeading));
            }
          }
        } else {
          for(const m of groupRows) wrapper.appendChild(_makeModelRow(m,shouldRenderHeading));
        }
      } else {
        for(const m of groupRows) dd.appendChild(_makeModelRow(m,shouldRenderHeading));
      }
      if(!term&&hiddenCount){
        const showAll=document.createElement('div');
        showAll.className='model-opt-more';
        showAll.tabIndex=0;
        showAll.setAttribute('role','button');
        const _moreLabel=esc(t('model_show_all_models',hiddenCount)||`Show ${hiddenCount} more`);
        showAll.innerHTML=`<span class="model-opt-more-chevron" aria-hidden="true"></span><span class="model-opt-more-label">${_moreLabel}</span>`;
        const _doExpand=()=>{
          // The reveal itself (in-place row insert + open + scroll-to-new) is
          // handled by _expandOverflowGroup; just trigger it.
          _expandOverflowGroup(meta);
        };
        showAll.onclick=(e)=>{
          if(e&&typeof e.stopPropagation==='function') e.stopPropagation();
          _doExpand();
        };
        showAll.addEventListener('keydown',e=>{
          if(e.key==='Enter'||e.key===' '){
            e.preventDefault();
            _doExpand();
          }
        });
        // Keep the expander inside the collapsible group so it hides/shows with it.
        if(_groupWrappers[groupKey]) _groupWrappers[groupKey].appendChild(showAll);
        else dd.appendChild(showAll);
      }
    }
    if(term&&found.size===0){
      const noResult=document.createElement('div');
      noResult.className='model-search-no-results';
      noResult.textContent=t('model_search_no_results')||'No models found';
      noResult.style.padding='12px 14px';
      noResult.style.color='var(--muted)';
      noResult.style.textAlign='center';
      dd.appendChild(noResult);
    }
    if(_autoFocusSearch||_hadFocus) _si.focus();
  };
  _si.addEventListener('input',()=>_filterModels(_si.value));
  // Keyboard navigation through filtered model rows (#2791).
  const _visibleModelRows=()=>Array.from(dd.querySelectorAll('.model-opt,.model-opt-more')).filter(el=>{
    let node=el.parentElement;
    while(node&&node!==dd){
      if(node.classList.contains('model-group-body')&&node.style.display==='none') return false;
      node=node.parentElement;
    }
    return true;
  });
  const _activeRowIndex=(rows)=>rows.findIndex(r=>r.classList.contains('is-highlighted'));
  const _highlightRow=(rows,idx)=>{
    for(const r of rows) r.classList.remove('is-highlighted');
    if(idx<0||idx>=rows.length) return;
    const row=rows[idx];
    row.classList.add('is-highlighted');
    if(typeof row.scrollIntoView==='function') row.scrollIntoView({block:'nearest'});
  };
  _si.addEventListener('keydown',e=>{
    if(e.key==='Escape'){closeDropdown();return;}
    if(e.key==='ArrowDown'||e.key==='ArrowUp'||e.key==='Enter'){
      const rows=_visibleModelRows();
      if(!rows.length){if(e.key==='Enter') e.preventDefault();return;}
      const cur=_activeRowIndex(rows);
      if(e.key==='ArrowDown'){e.preventDefault();_highlightRow(rows,cur<0?0:Math.min(rows.length-1,cur+1));return;}
      if(e.key==='ArrowUp'){e.preventDefault();_highlightRow(rows,cur<=0?rows.length-1:cur-1);return;}
      if(e.key==='Enter'){
        e.preventDefault();
        const pick=cur>=0?rows[cur]:rows[0];
        if(pick) pick.click();
      }
    }
  });
  _si.addEventListener('click',e=>e.stopPropagation());
  _sc.onclick=()=>{ _si.value=''; _filterModels(''); _si.focus(); };
  _sc.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){ _si.value=''; _filterModels(''); _si.focus(); e.preventDefault(); }});
  const _applyCustom=()=>{const v=_ci.value.trim();if(!v)return;selectFromDropdown(v,null);_ci.value='';};
  _cb.onclick=_applyCustom;
  _ci.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();_applyCustom();}if(e.key==='Escape'){closeDropdown();}});
  _ci.addEventListener('click',e=>e.stopPropagation());
  dd.appendChild(_scopeNote);
  dd.appendChild(_searchRow);
  dd.appendChild(_custSep);
  dd.appendChild(_custRow);
  _filterModels('');
}

async function selectModelFromDropdown(value){
  const preferredProviderId=arguments[1];
  const sel=$('modelSelect');
  if(!sel) { closeModelDropdown(); return; }
  const provider=String(preferredProviderId||'').trim()||null;
  const currentState=(typeof _modelStateForSelect==='function')
    ? _modelStateForSelect(sel, sel.value)
    : {model:sel.value,model_provider:null};
  const sameModel=String(currentState.model||'')===String(value||'');
  const sameProvider=String(currentState.model_provider||'')===String(provider||'');
  if(sameModel&&sameProvider){ closeModelDropdown(); return; }
  // Resolve the provider-specific option so duplicate bare IDs (e.g. gpt-5.5
  // under OpenAI Codex vs OpenRouter) update session model_provider correctly.
  if(typeof _ensureModelOptionInDropdown==='function'){
    _ensureModelOptionInDropdown(value, sel, provider);
  }else{
    sel.value=value;
  }
  syncModelChip();
  closeModelDropdown();
  if(typeof sel.onchange==='function') await sel.onchange();
}

async function toggleModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const sel=$('modelSelect');
  if(!dd||!chip||!sel) return;
  const open=dd.classList.contains('open');
  if(open){closeModelDropdown(); return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  if(typeof closeReasoningDropdown==='function') closeReasoningDropdown();
  if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  if(typeof window._ensureModelDropdownReady==='function'){
    const ready=window._ensureModelDropdownReady({freshness:'session_visit'});
    if(ready&&typeof ready.catch==='function') ready.catch(()=>{});
  }
  if(dd.classList.contains('open')) return;
  renderModelDropdown();
  dd.classList.add('open');
  _positionModelDropdown();
  const activeRow=dd.querySelector('.model-opt.active');
  if(activeRow&&typeof activeRow.scrollIntoView==='function') activeRow.scrollIntoView({block:'nearest'});
  chip.classList.add('active');
  const mobileAction=$('composerMobileModelAction');
  if(mobileAction) mobileAction.classList.add('active');
}

function closeModelDropdown(){
  const dd=$('composerModelDropdown');
  const chip=$('composerModelChip');
  const mobileAction=$('composerMobileModelAction');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
  if(mobileAction) mobileAction.classList.remove('active');
  // If the phone path reparented the menu onto <body>, put it back in the
  // footer and clear the fixed-position inline styles so the DOM returns to its
  // baseline shape and the next desktop open anchors correctly (#6080).
  if(typeof _restoreModelDropdownHome==='function') _restoreModelDropdownHome();
}

function closeSettingsModelDropdown(){
  const dd=$('settingsModelDropdown');
  const chip=$('settingsModelChip');
  if(dd) dd.classList.remove('open');
  if(chip){
    chip.classList.remove('active');
    chip.setAttribute('aria-expanded','false');
  }
}

function syncSettingsModelChip(){
  const sel=$('settingsModel');
  const chip=$('settingsModelChip');
  if(!sel||!chip) return;
  const opt=sel.selectedOptions&&sel.selectedOptions[0];
  const text=(opt&&opt.textContent)||getModelLabel(sel.value||'')||t('settings_label_model')||'Default Model';
  chip.textContent=text;
  chip.title=sel.value||text;
}

function selectSettingsModelFromDropdown(value,preferredProviderId){
  const sel=$('settingsModel');
  if(!sel){closeSettingsModelDropdown();return;}
  const provider=String(preferredProviderId||'').trim()||null;
  if(typeof _ensureModelOptionInDropdown==='function'){
    _ensureModelOptionInDropdown(value,sel,provider);
  }else{
    sel.value=value;
    if(typeof syncSettingsModelChip==='function') syncSettingsModelChip();
  }
  closeSettingsModelDropdown();
  try{
    if(typeof Event==='function') sel.dispatchEvent(new Event('change',{bubbles:true}));
    else if(typeof sel.onchange==='function') sel.onchange();
  }catch(_){}
}

function openSettingsModelDropdown(){
  const dd=$('settingsModelDropdown');
  const sel=$('settingsModel');
  const chip=$('settingsModelChip');
  if(!dd||!sel) return;
  // Auto-focus the search on desktop only. On touch (coarse pointer) grabbing focus
  // pops the on-screen keyboard the instant the chip is tapped — the composer picker
  // doesn't do it either, so match that behavior on touch. Computed before render so
  // renderModelDropdown's own initial focus is suppressed too (not just the outer one).
  const _coarsePointer=(typeof window.matchMedia==='function')&&window.matchMedia('(pointer: coarse)').matches;
  renderModelDropdown({
    dropdownId:'settingsModelDropdown',
    selectId:'settingsModel',
    forceOpenKey:'settingsModel',
    closeDropdown:closeSettingsModelDropdown,
    selectModel:selectSettingsModelFromDropdown,
    scopeNoteText:t('settings_desc_model')||'Used for new conversations. Existing conversations keep their selected model.',
    autoFocusSearch:!_coarsePointer,
  });
  dd.classList.add('open');
  if(chip){
    chip.classList.add('active');
    chip.setAttribute('aria-expanded','true');
  }
  if(!_coarsePointer){
    setTimeout(()=>{
      const input=dd.querySelector('.model-search-input');
      if(input) input.focus();
    },0);
  }
}

function toggleSettingsModelDropdown(){
  const dd=$('settingsModelDropdown');
  if(dd&&dd.classList.contains('open')){closeSettingsModelDropdown();return;}
  openSettingsModelDropdown();
}

function mountSettingsModelPicker(){
  const chip=$('settingsModelChip');
  const sel=$('settingsModel');
  if(!chip||!sel) return;
  syncSettingsModelChip();
  if(!chip._settingsModelPickerBound){
    chip._settingsModelPickerBound=true;
    chip.addEventListener('click',e=>{
      e.preventDefault();
      e.stopPropagation();
      toggleSettingsModelDropdown();
    });
    chip.addEventListener('keydown',e=>{
      if(e.key==='Enter'||e.key===' '||e.key==='ArrowDown'){
        e.preventDefault();
        toggleSettingsModelDropdown();
      }
    });
  }
}

document.addEventListener('click',e=>{
  if(
    !e.target.closest('#composerModelChip') &&
    !e.target.closest('#composerMobileModelAction') &&
    !e.target.closest('#composerModelDropdown')
  ) closeModelDropdown();
  if(
    !e.target.closest('#settingsModelChip') &&
    !e.target.closest('#settingsModel') &&
    !e.target.closest('#settingsModelDropdown')
  ) closeSettingsModelDropdown();
});
window.addEventListener('resize',()=>{
  const dd=$('composerModelDropdown');
  if(dd&&dd.classList.contains('open')) _positionModelDropdown();
  // Keep the reasoning dropdown aligned under its chip when the window
  // resizes while open — same pattern as the model dropdown above.
  const rdd=$('composerReasoningDropdown');
  if(rdd&&rdd.classList.contains('open')&&typeof _positionReasoningDropdown==='function'){
    _positionReasoningDropdown();
  }
});

// visualViewport resize/scroll fire on mobile when the on-screen keyboard opens
// or the URL bar collapses/expands — the phone dropdown is fixed to the visual
// viewport, so it must be re-measured against the new offsets. Coalesce with rAF
// so a burst of scroll/resize events triggers at most one reposition per frame.
let _modelDropdownRepositionScheduled=false;
function _repositionOpenModelDropdown(){
  const dd=$('composerModelDropdown');
  if(!(dd&&dd.classList.contains('open'))||_modelDropdownRepositionScheduled) return;
  _modelDropdownRepositionScheduled=true;
  requestAnimationFrame(()=>{
    _modelDropdownRepositionScheduled=false;
    const openDd=$('composerModelDropdown');
    if(openDd&&openDd.classList.contains('open')) _positionModelDropdown();
  });
}
if(window.visualViewport){
  window.visualViewport.addEventListener('resize',_repositionOpenModelDropdown);
  window.visualViewport.addEventListener('scroll',_repositionOpenModelDropdown);
}

// ── Fit-based composer footer collapse ──────────────────────────────────────
// Stage classes on .composer-footer:
//   (none) full labels · .cf-icons icon chips · .cf-icons.cf-burger hamburger.
let _composerFitScheduled=false;
let _composerFitResizeObserver=null;
let _composerFitMutationObserver=null;
let _composerFitObservedFooter=null;
let _composerFitResizeListenerBound=false;

function _fitComposerFooter(){
  const footer=document.querySelector('.composer-footer');
  if(!footer) return;
  const left=footer.querySelector('.composer-left');
  if(!left) return;
  if(!left.clientWidth) return;
  const overflows=function(){return left.scrollWidth>left.clientWidth+1;};
  footer.classList.remove('cf-icons','cf-burger');
  if(!overflows()) return;
  footer.classList.add('cf-icons');
  if(!overflows()) return;
  footer.classList.add('cf-burger');
}
window._fitComposerFooter=_fitComposerFooter;

function _scheduleComposerFit(){
  if(_composerFitScheduled) return;
  _composerFitScheduled=true;
  requestAnimationFrame(function(){
    _composerFitScheduled=false;
    try{_fitComposerFooter();}catch(_){ }
  });
}
window._scheduleComposerFit=_scheduleComposerFit;

function _initComposerFooterFit(){
  const footer=document.querySelector('.composer-footer');
  const left=footer&&footer.querySelector('.composer-left');
  if(!footer||!left) return;
  _scheduleComposerFit();
  if(_composerFitObservedFooter===footer) return;
  if(_composerFitResizeObserver){try{_composerFitResizeObserver.disconnect();}catch(_){ }}
  if(_composerFitMutationObserver){try{_composerFitMutationObserver.disconnect();}catch(_){ }}
  _composerFitResizeObserver=null;
  _composerFitMutationObserver=null;
  _composerFitObservedFooter=footer;
  if(window.ResizeObserver){
    try{
      _composerFitResizeObserver=new ResizeObserver(_scheduleComposerFit);
      _composerFitResizeObserver.observe(footer);
      // Also observe the left control group directly: the footer's outer width
      // may not change when right-side controls (status/context chips) appear or
      // resize, but that shrinks .composer-left's available room and must
      // retrigger a refit. (Codex gate #4657.)
      if(left && left!==footer){try{_composerFitResizeObserver.observe(left);}catch(_){ }}
    }catch(_){ }
  }
  if(window.MutationObserver){
    try{
      _composerFitMutationObserver=new MutationObserver(_scheduleComposerFit);
      _composerFitMutationObserver.observe(left,{
        childList:true,subtree:true,characterData:true,
        attributes:true,attributeFilter:['class','style','hidden']
      });
    }catch(_){ }
  }
  if(!_composerFitResizeListenerBound){
    window.addEventListener('resize',_scheduleComposerFit);
    _composerFitResizeListenerBound=true;
  }
}
window._initComposerFooterFit=_initComposerFooterFit;

if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',_initComposerFooterFit);
}else{
  _initComposerFooterFit();
}

// ── Reasoning effort chip ────────────────────────────────────────────────────
let _currentReasoningEffort=null;
let _currentReasoningEffortsSupported=null;
// Whether the model accepts the thinking on/off toggle when supported_efforts
// is empty (GLM-4.5–5.1 on native zai). Undefined = unknown, treated as true
// so the chip stays visible by default (prior behavior).
let _currentReasoningToggleSupported=undefined;
let _profileTransitionReasoningContext=null;

function _normalizeReasoningEffort(eff){
  return String(eff||'').trim().toLowerCase();
}

function _formatReasoningEffortLabel(effort){
  if(effort==='none') return 'None';
  if(!effort) return 'Default';
  if(effort==='minimal') return 'Minimal';
  if(effort==='low') return 'Low';
  if(effort==='medium') return 'Medium';
  if(effort==='high') return 'High';
  if(effort==='xhigh') return 'XHigh';
  if(effort==='max') return 'Max';
  return effort.charAt(0).toUpperCase()+effort.slice(1);
}

function _reasoningEffortContext(){
  const transition=_profileTransitionReasoningContext;
  const session=S&&S.session;
  if(transition&&(!session||session.profile!==transition.profile)){
    const ctx={};
    if(transition.model) ctx.model=transition.model;
    if(transition.provider) ctx.provider=transition.provider;
    return ctx;
  }
  const sel=$('modelSelect');
  const model=(S&&S.session&&S.session.model)||(sel&&sel.value)||'';
  let provider=(S&&S.session&&S.session.model_provider)||'';
  if(!provider&&sel&&model&&typeof _modelStateForSelect==='function'){
    provider=_modelStateForSelect(sel, model).model_provider||'';
  }
  const ctx={};
  if(model) ctx.model=model;
  if(provider) ctx.provider=provider;
  return ctx;
}

function _reasoningEffortQuery(){
  const params=new URLSearchParams(_reasoningEffortContext());
  const qs=params.toString();
  return qs?('?'+qs):'';
}

function _applyReasoningOptions(supportedEfforts){
  const dd=$('composerReasoningDropdown');
  if(!dd) return;
  const supported=new Set(Array.isArray(supportedEfforts)?supportedEfforts:[]);
  dd.querySelectorAll('.reasoning-option').forEach(function(opt){
    const effort=opt.dataset.effort;
    // 'none' (turn thinking off) and '' (Default = clear override, provider
    // default = thinking on) are meta-options outside the effort ladder. They
    // are always shown so a thinking-toggle-only model (GLM-4.5–5.1 on native
    // zai, where the ladder is empty) still has an operable two-state control:
    // Default (on) + None (off). Without the Default option the toggle is
    // one-way off-only — the user can disable thinking but cannot re-enable it.
    // (#6219 round-3)
    if(effort==='none'||effort===''){
      opt.style.display='';
      return;
    }
    if(!supported.size){
      opt.style.display='none';
      return;
    }
    opt.style.display=supported.has(effort)?'':'none';
  });
}

function _applyReasoningChip(eff){
  const meta=arguments[1]||null;
  const effort=_normalizeReasoningEffort(eff);
  _currentReasoningEffort=effort;
  if(meta&&Array.isArray(meta.supported_efforts)){
    _currentReasoningEffortsSupported=meta.supported_efforts;
  }
  // supports_thinking_toggle: the model accepts the thinking on/off toggle even
  // when the effort ladder is empty (GLM-4.5–5.1 on native zai accept
  // `thinking: {"type": ...}` but NOT `reasoning_effort`). Without honoring this
  // flag, returning an empty supported_efforts hides the entire chip and
  // silently regresses the working thinking on/off control for those models.
  // Default true preserves prior behavior when the field is absent.
  if(meta&&typeof meta.supports_thinking_toggle==='boolean'){
    _currentReasoningToggleSupported=meta.supports_thinking_toggle;
  }
  const wrap=$('composerReasoningWrap');
  const label=$('composerReasoningLabel');
  const chip=$('composerReasoningChip');
  const mobileLabel=$('composerMobileReasoningLabel');
  const mobileAction=$('composerMobileReasoningAction');
  if(!wrap||!label) return;
  const supportedEfforts=(typeof _currentReasoningEffortsSupported==='undefined')
    ?null
    :_currentReasoningEffortsSupported;
  const toggleSupported=(typeof _currentReasoningToggleSupported==='undefined')
    ?true
    :_currentReasoningToggleSupported;
  const hasEffortLadder=Array.isArray(supportedEfforts)
    ?supportedEfforts.length>0
    :true;
  // Show the chip if there is an effort ladder OR a thinking toggle is still
  // available. Only hide when the model supports neither.
  const supports=hasEffortLadder||toggleSupported;
  if(!supports){
    wrap.style.display='none';
    if(mobileAction) mobileAction.style.display='none';
    return;
  }
  wrap.style.display='';
  if(mobileAction) mobileAction.style.display='';
  if(typeof _applyReasoningOptions==='function') _applyReasoningOptions(supportedEfforts);
  const text=_formatReasoningEffortLabel(effort);
  label.textContent=text;
  if(mobileLabel) mobileLabel.textContent=text;
  if(chip){
    const inactive=!effort||effort==='none';
    chip.classList.toggle('inactive',inactive);
    const labelText='Reasoning effort: '+text;
    chip.title=labelText;
    chip.setAttribute('aria-label',labelText);
  }
  if(mobileAction) mobileAction.classList.toggle('inactive',!effort||effort==='none');
  _highlightReasoningOption(effort);
}

// Tracks the model/provider identity of the last reasoning fetch so routine
// topbar syncs can serve the cached chip state instead of re-hitting the
// network. null = never fetched.
let _lastReasoningFetchKey=null;
// Monotonic dispatch counter. Each fetchReasoningChip() increments it and the
// async handlers capture their own value; a response (success OR failure) only
// applies if it is still the most recent dispatch. This defeats out-of-order
// resolution even when two fetches share the same model/provider key (e.g. a
// profile switch that resets the cache and refetches the same default model but
// a different agent.reasoning_effort) — #4650 review.
let _reasoningFetchSeq=0;

function fetchReasoningChip(keyOverride){
  // Set the cache key OPTIMISTICALLY before the request so rapid routine syncs
  // while this GET is in flight short-circuit instead of re-dispatching (that
  // in-flight window is exactly where the #4650 storm lived).
  const key=keyOverride===undefined?_reasoningEffortQuery():keyOverride;
  const seq=++_reasoningFetchSeq;
  _lastReasoningFetchKey=key;
  api('/api/reasoning'+key).then(function(st){
    // Ignore a stale/superseded response: only the most recent dispatch may
    // apply, so an older in-flight GET can't poison the current chip (#4650).
    if(seq!==_reasoningFetchSeq) return;
    _applyReasoningChip((st&&st.reasoning_effort)||'', st||{});
  }).catch(function(){
    // Same staleness guard on failure: a stale error must neither hide the chip
    // nor clear a newer fetch's key. Only the latest dispatch clears the key so
    // routine syncs retry after a genuine transient failure.
    if(seq!==_reasoningFetchSeq) return;
    _lastReasoningFetchKey=null;
    _applyReasoningChip('', {supported_efforts:[], supports_thinking_toggle:false});
  });
}

function refreshProfileTransitionReasoningChip(model, provider){
  _profileTransitionReasoningContext={profile:(S&&S.activeProfile)||'default',model,provider};
  _currentReasoningEffort=null;
  _currentReasoningEffortsSupported=null;
  _currentReasoningToggleSupported=undefined;
  _lastReasoningFetchKey=null;
  ++_reasoningFetchSeq;
  _applyReasoningChip('', {supported_efforts:[], supports_thinking_toggle:false});
  const params=new URLSearchParams();
  if(model) params.set('model',model);
  if(provider) params.set('provider',provider);
  fetchReasoningChip(params.size?'?'+params.toString():undefined);
}

function clearProfileTransitionReasoningContext(){
  _profileTransitionReasoningContext=null;
}

function syncReasoningChip(){
  // #4650: syncTopbar() calls this on every routine UI refresh, and during
  // streaming those fire at high frequency. Before a9ce2889 this served the
  // cached _currentReasoningEffort after the first load; that commit made it
  // refetch unconditionally to refresh supported-efforts after a model switch,
  // which turned ordinary syncs into a GET /api/reasoning storm (one per token).
  // Restore the cache short-circuit but keep a9ce2889's intent: only hit the
  // network when nothing is cached yet OR the model/provider identity changed
  // since the last fetch (the only inputs that change /api/reasoning's answer).
  // The user-pick and model-switch paths still update the cache directly.
  const key=_reasoningEffortQuery();
  // Short-circuit on the KEY alone: if a fetch for this exact model/provider has
  // already been dispatched (in-flight) or completed, do not dispatch another —
  // this is what stops the #4650 storm, including the COLD-cache window where
  // _currentReasoningEffort is still null between the first dispatch and its
  // response (10 syncs before the first GET resolves must produce ONE request,
  // not ten). Apply the cached chip only once we actually have an effort value.
  if(_lastReasoningFetchKey===key){
    if(_currentReasoningEffort!==null) _applyReasoningChip(_currentReasoningEffort);
    return;
  }
  fetchReasoningChip();
}

function _highlightReasoningOption(effort){
  const dd=$('composerReasoningDropdown');
  if(!dd) return;
  dd.querySelectorAll('.reasoning-option').forEach(function(opt){
    opt.classList.toggle('selected',opt.dataset.effort===effort);
  });
}

function toggleReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  if(!dd||!chip) return;
  const open=dd.classList.contains('open');
  if(open){closeReasoningDropdown();return;}
  if(typeof closeProfileDropdown==='function') closeProfileDropdown();
  if(typeof closeWsDropdown==='function') closeWsDropdown();
  closeModelDropdown();
  if(typeof closeToolsetsDropdown==='function') closeToolsetsDropdown();
  _highlightReasoningOption(_currentReasoningEffort);
  dd.classList.add('open');
  _positionReasoningDropdown();
  chip.classList.add('active');
  const mobileAction=$('composerMobileReasoningAction');
  if(mobileAction) mobileAction.classList.add('active');
}

function _positionReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const mobileAction=$('composerMobileReasoningAction');
  const footer=document.querySelector('.composer-footer');
  if(!dd||!chip||!footer) return;
  const panel=$('composerMobileConfigPanel');
  const anchor=(panel&&panel.classList.contains('open')&&mobileAction)?mobileAction:chip;
  const chipRect=anchor.getBoundingClientRect();
  const footerRect=footer.getBoundingClientRect();
  let left=chipRect.left-footerRect.left;
  const maxLeft=Math.max(0,footer.clientWidth-dd.offsetWidth);
  left=Math.max(0,Math.min(left,maxLeft));
  dd.style.left=`${left}px`;
}

function closeReasoningDropdown(){
  const dd=$('composerReasoningDropdown');
  const chip=$('composerReasoningChip');
  const mobileAction=$('composerMobileReasoningAction');
  if(dd) dd.classList.remove('open');
  if(chip) chip.classList.remove('active');
  if(mobileAction) mobileAction.classList.remove('active');
}

document.addEventListener('click',function(e){
  if(
    !e.target.closest('#composerReasoningChip') &&
    !e.target.closest('#composerMobileReasoningAction') &&
    !e.target.closest('#composerReasoningDropdown')
  ) closeReasoningDropdown();
  if(e.target.closest('.reasoning-option')){
    const opt=e.target.closest('.reasoning-option');
    const effort=opt&&opt.dataset.effort;
    // NOTE: effort may be the empty string for the "Default" option (clears
    // the override). Check option presence, not truthiness — `if(effort)` would
    // silently ignore the Default click and leave the toggle one-way off-only.
    // (#6219 round-3)
    if(opt){
      const payload=Object.assign({effort:effort},_reasoningEffortContext());
      api('/api/reasoning',{method:'POST',body:JSON.stringify(payload)})
        .then(function(st){
          // For Default (effort=''), the returned reasoning_effort is '' (clear)
          // — display 'Default' rather than an empty toast.
          const display=(st&&st.reasoning_effort)||effort||'Default';
          _applyReasoningChip((st&&st.reasoning_effort)||effort, st||{});
          showToast('🧠 Reasoning effort set to '+display);
        })
        .catch(function(){showToast('🧠 Failed to set effort');});
      closeReasoningDropdown();
    }
  }
});


window.HermesUI.register('modelPicker', {
  renderModelDropdown,
  selectModelFromDropdown,
  toggleModelDropdown,
  syncReasoningChip,
});
