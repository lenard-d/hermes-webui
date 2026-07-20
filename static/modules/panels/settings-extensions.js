import { state } from "./state.js";
import { MAIN_VIEW_PANELS } from "./core.js";

// Panels domain: extensions and plugins

// ── Extensions panel (browser-origin diagnostics + local enable controls) ──

export function _extensionStatusLabel(value){
  return value ? 'Enabled' : 'Disabled';
}

export function _extensionBooleanBadge(value){
  const cls=value?'extension-status-badge-on':'extension-status-badge-off';
  return `<span class="extension-status-badge ${cls}">${value?'true':'false'}</span>`;
}

export function _extensionAssetList(urls){
  if(!Array.isArray(urls)||urls.length===0){
    return '<div class="extension-url-empty">None</div>';
  }
  return '<ul class="extension-url-list">'+urls.map(url=>`<li><code>${esc(url)}</code></li>`).join('')+'</ul>';
}

export function _extensionWarningList(warnings){
  if(!Array.isArray(warnings)||warnings.length===0){
    return '<div class="extension-url-empty">No warnings.</div>';
  }
  return '<ul class="extension-warning-list">'+warnings.map(item=>{
    const rawCode=(item&&item.code)||'unknown_warning';
    const code=esc(rawCode);
    const source=esc((item&&item.source)||'unknown');
    const hint=rawCode==='extension_state_unknown_ids'
      ? '<span>Some saved disabled-extension overrides no longer match the current manifest; re-added extensions with the same id may stay disabled.</span>'
      : '';
    return `<li><code>${code}</code><span>${source}</span>${hint}</li>`;
  }).join('')+'</ul>';
}

export function _extensionCountValue(counts,key,urls){
  if(counts&&Number.isFinite(Number(counts[key]))) return Number(counts[key]);
  return Array.isArray(urls)?urls.length:0;
}

export function _extensionEntryStatusLabel(entry){
  const status=(entry&&entry.status)||'';
  if(status==='manifest_disabled') return 'Disabled in manifest';
  if(status==='user_disabled') return 'Disabled';
  if(status==='enabled') return 'Enabled';
  return 'Unknown';
}

export function _extensionEntryBadge(entry){
  const enabled=!!(entry&&entry.effective_enabled);
  const cls=enabled?'extension-status-badge-on':'extension-status-badge-off';
  return `<span class="extension-status-badge ${cls}">${esc(_extensionEntryStatusLabel(entry))}</span>`;
}

export function _configureExtensionSettingsFromStatus(data){
  if(!window.HermesExtensionSettings||!data||!Array.isArray(data.extensions)) return;
  window.HermesExtensionSettings.primeFromStatus({extensions:data.extensions});
}

export function _extensionSettingsFieldHtml(field,value){
  const key=String(field&&field.key||'');
  const type=String(field&&field.type||'');
  const label=String(field&&field.label||key);
  const desc=String(field&&field.description||'');
  const dataAttrs=`data-extension-setting-input="${esc(key)}" data-extension-setting-type="${esc(type)}"`;
  let control='';
  if(type==='boolean'){
    control=`<label class="extension-setting-check"><input type="checkbox" ${dataAttrs}${value?' checked':''}> <span>${esc(label)}</span></label>`;
  }else if(type==='number'||type==='integer'){
    const step=type==='integer'?'1':'any';
    control=`<label><span>${esc(label)}</span><input type="number" step="${step}" ${dataAttrs} value="${esc(String(value??''))}"></label>`;
  }else if(type==='enum'){
    const options=Array.isArray(field.options)?field.options:[];
    control=`<label><span>${esc(label)}</span><select ${dataAttrs}>${options.map(option=>{
      const optionValue=String(option&&option.value||'');
      const optionLabel=String(option&&option.label||optionValue);
      return `<option value="${esc(optionValue)}"${optionValue===value?' selected':''}>${esc(optionLabel)}</option>`;
    }).join('')}</select></label>`;
  }else{
    control=`<label><span>${esc(label)}</span><input type="text" ${dataAttrs} value="${esc(String(value??''))}"></label>`;
  }
  return `<div class="extension-setting-field">${control}${desc?`<div class="extension-setting-desc">${esc(desc)}</div>`:''}</div>`;
}

export function _extensionSettingsControls(entry){
  const id=(entry&&entry.id)||'';
  const storageOwned=!!(entry&&entry.storage_owned);
  if(!storageOwned){
    return '<div class="extension-settings-empty">No extension-owned browser storage permission.</div>';
  }
  const settingsApi=window.HermesExtensionSettings&&id?window.HermesExtensionSettings.settingsForExtension(id):null;
  if(!settingsApi||!settingsApi.trusted){
    return '<div class="extension-settings-empty">Reload WebUI after enabling or installing this extension to edit browser-local settings.</div>';
  }
  const schema=Array.isArray(settingsApi&&settingsApi.schema)?settingsApi.schema:[];
  const values=settingsApi?settingsApi.values:{};
  const fields=schema.length
    ? schema.map(field=>_extensionSettingsFieldHtml(field,values[field.key])).join('')
    : '<div class="extension-settings-empty">No configurable settings declared.</div>';
  return `<div class="extension-settings-box">
    <div class="extension-settings-head">
      <div>
        <div class="extension-settings-title">Browser-local extension settings</div>
        <div class="extension-settings-note">Settings and extension-owned storage stay in this browser. Do not store secrets here.</div>
      </div>
    </div>
    <div class="extension-settings-fields">${fields}</div>
    <div class="extension-settings-actions">
      <button class="sm-btn" type="button" data-extension-settings-save="${esc(id)}"${schema.length?'':' disabled aria-disabled="true"'}>Save settings</button>
      <button class="sm-btn" type="button" data-extension-settings-reset="${esc(id)}"${schema.length?'':' disabled aria-disabled="true"'}>Reset settings</button>
      <button class="sm-btn" type="button" data-extension-storage-clear="${esc(id)}">Clear extension storage</button>
    </div>
  </div>`;
}

export function _extensionInstalledList(extensions,extensionDirConfigured){
  const list=Array.isArray(extensions)?extensions:[];
  if(!list.length){
    if(!extensionDirConfigured) return '<div class="extension-url-empty">No extension directory is configured.</div>';
    return '<div class="extension-url-empty">No manifest extensions are installed in the configured bundle.</div>';
  }
  return `<div class="extension-installed-list">${list.map(entry=>{
    const id=(entry&&entry.id)||'';
    const name=(entry&&entry.name)||id||'Unnamed extension';
    const canToggle=!!(entry&&entry.can_toggle);
    const userEnabled=!!(entry&&entry.user_enabled);
    const disabledAttr=canToggle?'':' disabled aria-disabled="true"';
    const buttonText=userEnabled?'Disable':'Enable';
    const nextEnabled=userEnabled?'false':'true';
    const note=canToggle
      ? 'Toggles the WebUI-managed override for the next app load.'
      : 'Manifest-disabled entries cannot be enabled from WebUI.';
    return `<div class="extension-installed-row" data-extension-id="${esc(id)}">
      <div class="extension-installed-main">
        <div class="extension-installed-title-row">
          <div class="extension-installed-title">${esc(name)}</div>
          ${_extensionEntryBadge(entry)}
        </div>
        <div class="extension-installed-meta"><code>${esc(id)}</code><span>${esc(note)}</span></div>
        ${_extensionSettingsControls(entry)}
      </div>
      <button class="sm-btn extension-toggle-btn" type="button" data-extension-toggle-id="${esc(id)}" data-extension-next-enabled="${nextEnabled}"${disabledAttr}>${esc(buttonText)}</button>
    </div>`;
  }).join('')}</div>`;
}

export function _extensionSidecarHealthBadge(status,label){
  const safeStatus=['checking','healthy','unhealthy','blocked'].includes(status)?status:'checking';
  return `<span class="extension-sidecar-status-badge extension-sidecar-status-${safeStatus}">${esc(label||safeStatus)}</span>`;
}

export function _extensionRuntimeStatusValue(value){
  const normalized=String(value||'').trim().toLowerCase();
  return ['running','connected','waiting','stale','unloaded','stopped','not_registered','unknown'].includes(normalized)
    ? normalized
    : 'unknown';
}

export function _extensionRuntimeStatusLabel(value){
  const normalized=_extensionRuntimeStatusValue(value);
  if(normalized==='not_registered') return 'not registered';
  return normalized.replace(/_/g,' ');
}

export function _extensionRuntimeLastSeen(value){
  const text=String(value??'').trim();
  if(!/^\d+(?:\.\d+)?$/.test(text)) return '';
  const raw=Number(text);
  if(!Number.isFinite(raw)||raw<=0) return '';
  const seconds=raw>1000000000000?raw/1000:raw;
  const now=Math.floor(Date.now()/1000);
  if(seconds>now+300) return '';
  const age=Math.max(0,Math.floor(now-seconds));
  if(age<5) return 'just now';
  if(age<60) return `${age}s ago`;
  const minutes=Math.floor(age/60);
  if(minutes<60) return `${minutes}m ago`;
  const hours=Math.floor(minutes/60);
  if(hours<24) return `${hours}h ago`;
  return `${Math.floor(hours/24)}d ago`;
}

export function _extensionRuntimeOrigin(value){
  const text=String(value||'').trim();
  if(!text) return '';
  try{
    const parsed=new URL(text);
    if(parsed.protocol==='http:'&&(parsed.hostname==='127.0.0.1'||parsed.hostname==='localhost')){
      return parsed.origin;
    }
  }catch(_e){}
  return '';
}

export function _extensionRuntimeRows(runtime){
  if(!runtime||typeof runtime!=='object') return [];
  const rows=[];
  if(Object.prototype.hasOwnProperty.call(runtime,'sidecar')){
    rows.push(['Sidecar',_extensionRuntimeStatusLabel(runtime.sidecar)]);
  }
  if(Object.prototype.hasOwnProperty.call(runtime,'native_host')){
    rows.push(['Native host',_extensionRuntimeStatusLabel(runtime.native_host)]);
  }
  if(Object.prototype.hasOwnProperty.call(runtime,'bridge')){
    rows.push(['Bridge',_extensionRuntimeStatusLabel(runtime.bridge)]);
  }
  const lastSeen=_extensionRuntimeLastSeen(runtime.last_seen_at);
  if(lastSeen) rows.push(['Last update',lastSeen]);
  const origin=_extensionRuntimeOrigin(runtime.webui_origin);
  if(origin) rows.push(['WebUI origin',origin]);
  return rows;
}

export function _extensionRuntimeDetails(runtime){
  const rows=_extensionRuntimeRows(runtime);
  if(!rows.length) return '';
  return rows.map(([label,value])=>`<div><span>${esc(label)}</span><code>${esc(value)}</code></div>`).join('');
}

export function _extensionSidecarCard(sidecars){
  const list=Array.isArray(sidecars)?sidecars:[];
  const body=list.length?`<div class="extension-sidecar-list">${list.map((sidecar,index)=>{
    const id=(sidecar&&sidecar.id)||'';
    const name=(sidecar&&sidecar.name)||'';
    const title=name||id||'Unnamed extension';
    const meta=(name&&id)?id:(sidecar&&sidecar.type)||'loopback';
    const origin=(sidecar&&sidecar.origin)||'';
    const healthPath=(sidecar&&sidecar.health_path)||'';
    const healthUrl=(sidecar&&sidecar.health_url)||'';
    const proxy=(sidecar&&sidecar.proxy&&typeof sidecar.proxy==='object')?sidecar.proxy:{};
    const proxyAvailable=proxy.available===true;
    const proxyConsented=proxy.consented===true;
    const proxyConsentRequired=proxy.consent_required===true;
    const proxyOriginChanged=proxy.origin_changed===true;
    const proxyPath=(proxy&&proxy.path)||'';
    const proxyStatus=proxyConsented
      ?'consented'
      :(proxyOriginChanged
        ?'reconfirm required'
        :(proxyConsentRequired
          ?'approval required'
          :'unavailable'));
    const proxyButton=(proxyAvailable&&id)
      ?`<button class="sm-btn extension-toggle-btn" type="button" data-extension-sidecar-proxy-id="${esc(id)}" data-extension-sidecar-proxy-approved="${proxyConsented?'false':'true'}">${esc(proxyConsented?'Revoke proxy consent':'Approve proxy consent')}</button>`
      :'';
    return `<div class="extension-sidecar-row" data-sidecar-index="${index}">
      <div class="extension-sidecar-row-head">
        <div class="extension-sidecar-title">${esc(title)}</div>
        <span id="extensionSidecarHealth${index}" data-sidecar-health-index="${index}">${_extensionSidecarHealthBadge('checking','checking')}</span>
      </div>
      <div class="extension-sidecar-meta">${esc(meta)}</div>
      <div class="extension-sidecar-fields">
        <div><span>Origin</span><code>${esc(origin)}</code></div>
        <div><span>Health path</span><code>${esc(healthPath)}</code></div>
        <div><span>Health URL</span><code>${esc(healthUrl)}</code></div>
        <div><span>Proxy</span><code>${esc(proxyStatus)}</code></div>
        <div><span>Proxy path</span><code>${esc(proxyPath)}</code></div>
      </div>
      <div class="extension-sidecar-actions">${proxyButton}</div>
      <div class="extension-sidecar-runtime" data-sidecar-runtime-index="${index}" hidden></div>
    </div>`;
  }).join('')}</div>`:'<div class="extension-url-empty">No loopback sidecars declared.</div>';
  return `
    <div class="provider-card extension-sidecars-card">
      <div class="provider-card-header plugin-card-header">
        <div class="provider-card-info">
          <div class="provider-card-name">Loopback sidecars</div>
          <div class="provider-card-meta">Declared local companions; health is checked directly from this browser with WebUI credentials omitted.</div>
        </div>
      </div>
      <div class="provider-card-body extension-card-body">
        ${body}
      </div>
    </div>`;
}

export function _setExtensionSidecarHealth(index,status,label){
  const el=document.querySelector(`[data-sidecar-health-index="${index}"]`);
  if(el) el.innerHTML=_extensionSidecarHealthBadge(status,label);
}

export function _setExtensionSidecarRuntime(index,runtime){
  const el=document.querySelector(`[data-sidecar-runtime-index="${index}"]`);
  if(!el) return;
  const details=_extensionRuntimeDetails(runtime);
  if(!details){
    el.hidden=true;
    el.innerHTML='';
    return;
  }
  el.hidden=false;
  el.innerHTML=details;
}

export async function _checkExtensionSidecarHealth(sidecar,index,seq){
  const healthUrl=sidecar&&sidecar.health_url;
  if(!healthUrl){
    _setExtensionSidecarHealth(index,'blocked','unreachable / blocked');
    _setExtensionSidecarRuntime(index,null);
    return;
  }
  let controller=null;
  let timeoutId=null;
  try{
    if(typeof AbortController!=='undefined'){
      controller=new AbortController();
      timeoutId=setTimeout(()=>controller.abort(),2500);
    }
    const res=await fetch(healthUrl,{credentials:'omit',cache:'no-store',signal:controller?controller.signal:undefined});
    if(seq!==state._extensionsSidecarMonitorSeq) return;
    if(res.ok){
      _setExtensionSidecarHealth(index,'healthy','healthy');
      let body=null;
      try{
        body=await res.json();
      }catch(_e){}
      if(seq!==state._extensionsSidecarMonitorSeq) return;
      _setExtensionSidecarRuntime(index,body&&typeof body==='object'?body.runtime:null);
    }else{
      _setExtensionSidecarHealth(index,'unhealthy','unhealthy');
      _setExtensionSidecarRuntime(index,null);
    }
  }catch(_e){
    if(seq!==state._extensionsSidecarMonitorSeq) return;
    _setExtensionSidecarHealth(index,'blocked','unreachable / blocked');
    _setExtensionSidecarRuntime(index,null);
  }finally{
    if(timeoutId) clearTimeout(timeoutId);
  }
}

export function _monitorExtensionSidecars(sidecars,seq){
  if(!Array.isArray(sidecars)||sidecars.length===0) return;
  sidecars.forEach((sidecar,index)=>_checkExtensionSidecarHealth(sidecar,index,seq));
}

export function _renderExtensionsPanel(data,seq){
  const target=$('extensionsDiagnostics');
  const copyBtn=$('extensionsCopyDiagnosticsBtn');
  if(!target) return;
  state._extensionsStatusData=data||null;
  _configureExtensionSettingsFromStatus(data);
  if(copyBtn) copyBtn.disabled=!data;
  const manifest=(data&&data.manifest)||{};
  const counts=(data&&data.counts)||{};
  const scripts=Array.isArray(data&&data.script_urls)?data.script_urls:[];
  const styles=Array.isArray(data&&data.stylesheet_urls)?data.stylesheet_urls:[];
  const sidecars=Array.isArray(data&&data.sidecars)?data.sidecars:[];
  const extensions=Array.isArray(data&&data.extensions)?data.extensions:[];
  const statusClass=(data&&data.enabled)?'extension-card-enabled':'extension-card-disabled';
  const scriptCount=_extensionCountValue(counts,'script_urls',scripts);
  const styleCount=_extensionCountValue(counts,'stylesheet_urls',styles);
  const sidecarCount=_extensionCountValue(counts,'sidecars',sidecars);
  const manifestExtensionCount=_extensionCountValue(counts,'manifest_extensions',extensions);
  const userDisabledCount=_extensionCountValue(counts,'user_disabled',[]);
  target.innerHTML=`
    <div class="provider-card extension-status-card ${statusClass}">
      <div class="provider-card-header plugin-card-header">
        <div class="provider-card-info">
          <div class="provider-card-name">Extension runtime</div>
          <div class="provider-card-meta">Status from /api/extensions/status; toggles persist a local override for installed manifest entries.</div>
        </div>
        <span class="provider-card-badge ${data&&data.enabled?'':'plugin-card-badge-disabled'}">${_extensionStatusLabel(!!(data&&data.enabled))}</span>
      </div>
      <div class="provider-card-body extension-card-body">
        <div class="extension-summary-grid">
          <div><span>Extension dir configured</span>${_extensionBooleanBadge(!!(data&&data.extension_dir_configured))}</div>
          <div><span>Extension dir valid</span>${_extensionBooleanBadge(!!(data&&data.extension_dir_valid))}</div>
          <div><span>Manifest configured</span>${_extensionBooleanBadge(!!manifest.configured)}</div>
          <div><span>Manifest loaded</span>${_extensionBooleanBadge(!!manifest.loaded)}</div>
          <div><span>Manifest status</span><code>${esc(manifest.status||'unknown')}</code></div>
          <div><span>Manifest entries inspected</span><code>${Number(manifest.entry_count)||0}</code></div>
          <div><span>Manifest script count</span><code>${Number(manifest.script_count)||0}</code></div>
          <div><span>Manifest stylesheet count</span><code>${Number(manifest.stylesheet_count)||0}</code></div>
          <div><span>Manifest sidecar count</span><code>${Number(manifest.sidecar_count)||0}</code></div>
          <div><span>Final script count</span><code>${scriptCount}</code></div>
          <div><span>Final stylesheet count</span><code>${styleCount}</code></div>
          <div><span>Loopback sidecar count</span><code>${sidecarCount}</code></div>
          <div><span>Installed manifest extensions</span><code>${manifestExtensionCount}</code></div>
          <div><span>User-disabled extensions</span><code>${userDisabledCount}</code></div>
        </div>
      </div>
    </div>
    <div class="provider-card extension-installed-card">
      <div class="provider-card-header plugin-card-header">
        <div class="provider-card-info">
          <div class="provider-card-name">Installed manifest extensions</div>
          <div class="provider-card-meta">Enable or disable already-present local extensions. Reload WebUI to apply injected asset changes to this browser tab.</div>
        </div>
      </div>
      <div class="provider-card-body extension-card-body">
        ${_extensionInstalledList(extensions,!!(data&&data.extension_dir_configured))}
      </div>
    </div>
    <div class="provider-card extension-assets-card">
      <div class="provider-card-header plugin-card-header">
        <div class="provider-card-info">
          <div class="provider-card-name">Final public asset URLs</div>
          <div class="provider-card-meta">Same-origin URLs that may be injected into the app shell.</div>
        </div>
      </div>
      <div class="provider-card-body extension-card-body">
        <div class="provider-card-label">Scripts</div>
        ${_extensionAssetList(scripts)}
        <div class="provider-card-label extension-section-label">Stylesheets</div>
        ${_extensionAssetList(styles)}
      </div>
    </div>
    ${_extensionSidecarCard(sidecars)}
    <div class="provider-card extension-warnings-card">
      <div class="provider-card-header plugin-card-header">
        <div class="provider-card-info">
          <div class="provider-card-name">Sanitized warnings</div>
          <div class="provider-card-meta">Codes and coarse sources only; paths and rejected values are not shown.</div>
        </div>
      </div>
      <div class="provider-card-body extension-card-body">
        ${_extensionWarningList(data&&data.warnings)}
      </div>
    </div>
  `;
  _bindExtensionToggleButtons(target);
  _bindExtensionSidecarProxyButtons(target);
  _bindExtensionSettingsButtons(target);
  _monitorExtensionSidecars(sidecars,seq);
}

export function _bindExtensionToggleButtons(root){
  if(!root) return;
  root.querySelectorAll('[data-extension-toggle-id]').forEach(btn=>{
    btn.addEventListener('click',()=>handleExtensionToggle(btn));
  });
}

export function _bindExtensionSidecarProxyButtons(root){
  if(!root) return;
  root.querySelectorAll('[data-extension-sidecar-proxy-id]').forEach(btn=>{
    btn.addEventListener('click',()=>handleExtensionSidecarProxyConsent(btn));
  });
}

export async function handleExtensionToggle(btn){
  if(!btn||btn.disabled) return;
  const id=btn.dataset.extensionToggleId||'';
  const enabled=btn.dataset.extensionNextEnabled==='true';
  if(!id) return;
  const previousText=btn.textContent;
  btn.disabled=true;
  btn.textContent=enabled?'Enabling…':'Disabling…';
  try{
    const data=await api('/api/extensions/toggle',{method:'POST',body:JSON.stringify({id,enabled})});
    showToast(enabled?'Extension enabled. Reload WebUI to apply changes.':'Extension disabled. Reload WebUI to apply changes.');
    _renderExtensionsPanel(data,++state._extensionsSidecarMonitorSeq);
  }catch(e){
    btn.disabled=false;
    btn.textContent=previousText;
    showToast('Failed to update extension: '+(e&&e.message?e.message:String(e)));
  }
}

export async function handleExtensionSidecarProxyConsent(btn){
  if(!btn||btn.disabled) return;
  const id=btn.dataset.extensionSidecarProxyId||'';
  const approved=btn.dataset.extensionSidecarProxyApproved==='true';
  if(!id) return;
  const previousText=btn.textContent;
  btn.disabled=true;
  btn.textContent=approved?'Approving…':'Revoking…';
  try{
    const data=await api('/api/extensions/sidecar-proxy-consent',{method:'POST',body:JSON.stringify({id,approved})});
    showToast(approved?'Extension sidecar proxy approved.':'Extension sidecar proxy consent revoked.');
    _renderExtensionsPanel(data,++state._extensionsSidecarMonitorSeq);
  }catch(e){
    btn.disabled=false;
    btn.textContent=previousText;
    showToast('Failed to update extension sidecar proxy consent: '+(e&&e.message?e.message:String(e)));
  }
}
export function _readExtensionSettingsForm(row){
  const values={};
  row.querySelectorAll('[data-extension-setting-input]').forEach(input=>{
    const key=input.dataset.extensionSettingInput||'';
    const type=input.dataset.extensionSettingType||'';
    if(!key) return;
    if(type==='boolean') values[key]=!!input.checked;
    else if(type==='integer') values[key]=Number.parseInt(input.value,10);
    else if(type==='number') values[key]=Number.parseFloat(input.value);
    else values[key]=input.value;
  });
  return values;
}

export function _fillExtensionSettingsForm(row,id){
  if(!window.HermesExtensionSettings) return;
  const values=window.HermesExtensionSettings.settingsForExtension(id).values;
  row.querySelectorAll('[data-extension-setting-input]').forEach(input=>{
    const key=input.dataset.extensionSettingInput||'';
    const type=input.dataset.extensionSettingType||'';
    const value=values[key];
    if(type==='boolean') input.checked=!!value;
    else input.value=value??'';
  });
}

export function _bindExtensionSettingsButtons(root){
  if(!root) return;
  root.querySelectorAll('[data-extension-settings-save]').forEach(btn=>{
    btn.addEventListener('click',()=>handleExtensionSettingsSave(btn));
  });
  root.querySelectorAll('[data-extension-settings-reset]').forEach(btn=>{
    btn.addEventListener('click',()=>handleExtensionSettingsReset(btn));
  });
  root.querySelectorAll('[data-extension-storage-clear]').forEach(btn=>{
    btn.addEventListener('click',()=>handleExtensionStorageClear(btn));
  });
}

export function handleExtensionSettingsSave(btn){
  const id=btn&&btn.dataset.extensionSettingsSave;
  const row=btn&&btn.closest('[data-extension-id]');
  if(!id||!row||!window.HermesExtensionSettings) return;
  const api=window.HermesExtensionSettings.settingsForExtension(id);
  const result=api.setAll(_readExtensionSettingsForm(row));
  if(!result.ok){
    showToast('Extension settings contain invalid values.');
    return;
  }
  _fillExtensionSettingsForm(row,id);
  showToast('Extension settings saved in this browser.');
}

export function handleExtensionSettingsReset(btn){
  const id=btn&&btn.dataset.extensionSettingsReset;
  const row=btn&&btn.closest('[data-extension-id]');
  if(!id||!row||!window.HermesExtensionSettings) return;
  window.HermesExtensionSettings.settingsForExtension(id).reset();
  _fillExtensionSettingsForm(row,id);
  showToast('Extension settings reset in this browser.');
}

export function handleExtensionStorageClear(btn){
  const id=btn&&btn.dataset.extensionStorageClear;
  if(!id||!window.HermesExtensionSettings) return;
  window.HermesExtensionSettings.storageForExtension(id).clear();
  showToast('Extension storage cleared in this browser.');
}

export async function loadExtensionsPanel(opts){
  const target=$('extensionsDiagnostics');
  const copyBtn=$('extensionsCopyDiagnosticsBtn');
  if(!target) return;
  // Only preserve REAL rendered diagnostics across a refresh — never the
  // "Loading…" / error placeholders, or a failed refresh would leave the panel
  // stuck on "Loading extension diagnostics…" instead of rendering the error.
  const preserveExisting=!!(
    opts&&opts.preserveExisting&&target.innerHTML.trim()
    &&!target.querySelector('.extensions-loading,.extensions-error')
  );
  if(copyBtn&&!preserveExisting) copyBtn.disabled=true;
  const seq=++state._extensionsSidecarMonitorSeq;
  if(!preserveExisting) target.innerHTML='<div class="extensions-loading">Loading extension diagnostics…</div>';
  try{
    const data=await api('/api/extensions/status');
    if(seq!==state._extensionsSidecarMonitorSeq) return;
    _renderExtensionsPanel(data,seq);
  }catch(e){
    if(seq!==state._extensionsSidecarMonitorSeq) return;
    if(preserveExisting&&target.innerHTML.trim()) return;
    state._extensionsStatusData=null;
    if(copyBtn) copyBtn.disabled=true;
    target.innerHTML='<div class="extensions-error">Failed to load extension diagnostics: '+esc(e.message||String(e))+'</div>';
  }
  if(state._extensionsActiveTab==='gallery'&&!state._extensionsGalleryLoaded) loadExtensionsGallery();
}

export function switchExtensionsTab(tab){
  state._extensionsActiveTab=tab;
  document.querySelectorAll('[data-extensions-tab]').forEach(btn=>{
    btn.classList.toggle('extensions-tab-active',btn.dataset.extensionsTab===tab);
  });
  document.querySelectorAll('[data-extensions-pane]').forEach(pane=>{
    pane.hidden=pane.dataset.extensionsPane!==tab;
  });
  if(tab==='diagnostics') loadExtensionsPanel({preserveExisting:true});
  if(tab==='gallery'&&!state._extensionsGalleryLoaded) loadExtensionsGallery();
}

export function _extensionSafeHttpUrl(value){
  if(!value) return '';
  const raw=String(value).trim();
  if(!/^https?:\/\//i.test(raw)) return '';
  try{
    const url=new URL(raw);
    if(url.username||url.password) return '';
    return (url.protocol==='http:'||url.protocol==='https:')?url.href:'';
  }catch(_){
    return '';
  }
}

export function _extensionRegistrySourceUrl(entryPath){
  const raw=String(entryPath||'').trim();
  if(!raw||raw.startsWith('/')||raw.includes('\\')||raw.includes('\0')) return '';
  const parts=raw.split('/').filter(Boolean);
  if(parts.length===0||parts.some(part=>part==='.'||part==='..')) return '';
  const folder=parts.length>1?parts.slice(0,-1):parts;
  return 'https://github.com/hermes-webui/hermes-webui-extensions/tree/main/'+folder.map(encodeURIComponent).join('/');
}

export function _extensionSourceUrl(entry){
  if(!entry||typeof entry!=='object') return '';
  const candidates=[
    entry.homepage,
    entry.repository_url,
    entry.repo_url,
    entry.source_url,
    entry.source,
  ];
  const repository=entry.repository;
  if(typeof repository==='string'){
    candidates.push(repository);
  }else if(repository&&typeof repository==='object'){
    candidates.push(repository.url,repository.html_url);
  }
  for(const candidate of candidates){
    const safe=_extensionSafeHttpUrl(candidate);
    if(safe) return safe;
  }
  return _extensionSafeHttpUrl(_extensionRegistrySourceUrl(entry.entry_path||entry.runtime_manifest_path));
}

export function _extensionSourceLink(entry){
  const url=_extensionSourceUrl(entry);
  if(!url) return '';
  return `<a class="extension-gallery-source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Source</a>`;
}

export function _extensionPermissionList(value){
  if(!Array.isArray(value)) return '';
  const items=value
    .map(item=>String(item||'').trim())
    .filter(Boolean);
  return items.length?items.join(', '):'';
}

export function _extensionPermissionRows(perms){
  if(!perms||typeof perms!=='object') return [];
  const rows=[];
  const api=(perms.webui_api&&typeof perms.webui_api==='object')?perms.webui_api:{};
  const apiRead=_extensionPermissionList(api.read);
  const apiWrite=_extensionPermissionList(api.write);
  if(apiRead) rows.push(['WebUI API reads',apiRead]);
  if(apiWrite) rows.push(['WebUI API writes',apiWrite]);
  if(perms.webui_navigation===true) rows.push(['Navigation','Can open or switch WebUI views']);

  const sidecarCommands=(perms.sidecar_commands&&typeof perms.sidecar_commands==='object')?perms.sidecar_commands:{};
  const commandLabels=[
    ['from_loopback','accepts loopback commands'],
    ['can_switch_sessions','switch sessions'],
    ['can_write_drafts','write drafts'],
    ['can_autosend','auto-send drafts'],
    ['can_respond_approval','respond to approvals'],
    ['can_respond_clarify','respond to clarifications'],
  ];
  const commands=commandLabels
    .filter(([key])=>sidecarCommands[key]===true)
    .map(([,label])=>label);
  if(commands.length) rows.push(['Sidecar commands',commands.join(', ')]);

  const dom=(perms.dom&&typeof perms.dom==='object')?perms.dom:{};
  const domItems=[];
  if(dom.owned===true) domItems.push('renders extension-owned UI');
  if(dom.mutates_core_views===true) domItems.push('can alter core WebUI views');
  if(domItems.length) rows.push(['DOM access',domItems.join(', ')]);

  const storage=(perms.storage&&typeof perms.storage==='object')?perms.storage:{};
  const ownedStorage=_extensionPermissionList(storage.owned||storage.owned_keys);
  const sharedStorage=_extensionPermissionList(storage.shared_webui_keys);
  if(ownedStorage) rows.push(['Owned storage keys',ownedStorage]);
  if(sharedStorage) rows.push(['Shared WebUI storage',sharedStorage]);

  if(perms.loopback_sidecar===true) rows.push(['Loopback sidecar','Can contact a declared local loopback helper']);
  if(perms.native_host===true) rows.push(['Native host','Requires a local native host or desktop app']);

  const filesystem=(perms.filesystem&&typeof perms.filesystem==='object')?perms.filesystem:{};
  if(filesystem.arbitrary===true){
    rows.push(['Filesystem','Can access arbitrary filesystem paths']);
  }else if(filesystem.serves_bundled_assets===true){
    rows.push(['Filesystem','Serves bundled extension assets only']);
  }
  if(perms.network_external===true||perms.external_network===true){
    rows.push(['External network','Can contact external network origins']);
  }
  return rows;
}

export function _extensionPermissionSummary(perms){
  const rows=_extensionPermissionRows(perms);
  const body=rows.length
    ? '<div class="extension-gallery-permission-list">'+rows.map(([label,value])=>`
      <div class="extension-gallery-permission-row">
        <span class="extension-gallery-permission-label">${esc(label)}</span>
        <span class="extension-gallery-permission-value">${esc(value)}</span>
      </div>`).join('')+'</div>'
    : `<div class="extension-gallery-permission-empty">${esc(t('ext_gallery_permissions_empty'))}</div>`;
  return `<details class="extension-gallery-perms">
    <summary>${esc(t('ext_gallery_permissions_show'))}</summary>
    ${body}
  </details>`;
}

export function _extensionPostInstallNote(entry,isInstalled){
  const lifecycle=(entry&&entry.lifecycle&&typeof entry.lifecycle==='object')?entry.lifecycle:{};
  const post=(entry&&entry.post_install&&typeof entry.post_install==='object')?entry.post_install:null;
  const needsSidecar=!!lifecycle.sidecar_start_required;
  const needsNative=!!lifecycle.native_host_start_required;
  const summary=post&&post.summary?String(post.summary):(
    (needsSidecar||needsNative)
      ? t('ext_gallery_local_component_required')
      : ''
  );
  if(!summary) return '';
  const docsUrl=_extensionSafeHttpUrl(post&&post.docs_url);
  const localAppLabel=post&&post.local_app_label?String(post.local_app_label):t('ext_gallery_local_app_label');
  const chips=[];
  if(post&&post.requires_local_app===true) chips.push(t('ext_gallery_required_suffix',localAppLabel));
  if(needsSidecar) chips.push(t('ext_gallery_sidecar_required'));
  if(needsNative) chips.push(t('ext_gallery_native_host_required'));
  const chipHtml=chips.length
    ? '<div class="extension-gallery-next-chips">'+chips.map(item=>`<span>${esc(item)}</span>`).join('')+'</div>'
    : '';
  const docsHtml=docsUrl
    ? `<a class="extension-gallery-next-link" href="${esc(docsUrl)}" target="_blank" rel="noopener noreferrer">${esc(t('ext_gallery_open_setup_guide'))}</a>`
    : '';
  return `<div class="extension-gallery-next-step">
    <div class="extension-gallery-next-label">${esc(t(isInstalled?'ext_gallery_next_step':'ext_gallery_after_install'))}</div>
    <div class="extension-gallery-next-summary">${esc(summary)}</div>
    ${chipHtml}
    ${docsHtml}
  </div>`;
}

export async function loadExtensionsGallery(){
  state._extensionsGalleryLoaded=true;
  const galleryEl=$('extensionsGallery');
  const installedEl=$('extensionsInstalled');
  if(galleryEl) galleryEl.innerHTML='<div class="extensions-loading">Loading gallery…</div>';
  if(installedEl) installedEl.innerHTML='<div class="extensions-loading">Loading installed extensions…</div>';
  try{
    const [regData,statusData]=await Promise.all([
      api('/api/extensions/registry'),
      api('/api/extensions/status'),
    ]);
    state._extensionsGalleryData={regData,statusData};
    _renderExtensionsGallery(regData.entries||[],statusData);
  }catch(e){
    state._extensionsGalleryLoaded=false;
    const msg=esc(e&&e.message?e.message:String(e));
    if(galleryEl) galleryEl.innerHTML='<div class="extensions-error">Failed to load gallery: '+msg+'</div>';
    if(installedEl) installedEl.innerHTML='<div class="extensions-error">Failed to load extension status.</div>';
  }
}

export function _renderExtensionsGallery(entries,statusData){
  const galleryEl=$('extensionsGallery');
  const installedEl=$('extensionsInstalled');
  _configureExtensionSettingsFromStatus(statusData);
  const installedIds=new Set();
  if(statusData&&statusData.gallery_installed){
    Object.keys(statusData.gallery_installed).forEach(id=>installedIds.add(id));
  }
  if(statusData&&Array.isArray(statusData.extensions)){
    statusData.extensions.forEach(e=>{ if(e&&e.id) installedIds.add(e.id); });
  }
  if(!Array.isArray(entries)||entries.length===0){
    if(galleryEl) galleryEl.innerHTML='<div class="extensions-empty">No extensions found in the registry.</div>';
    if(installedEl){
      installedEl.innerHTML=_extensionInstalledList(statusData&&statusData.extensions,!!(statusData&&statusData.extension_dir_configured));
      _bindExtensionToggleButtons(installedEl);
      _bindExtensionSettingsButtons(installedEl);
    }
    return;
  }
  const galleryCards=[];
  for(const entry of entries){
    const id=esc(String(entry.id||''));
    const name=esc(String(entry.name||entry.id||''));
    const author=esc(String(entry.author||''));
    const version=esc(String(entry.version||''));
    const desc=esc(String(entry.description||''));
    const caps=Array.isArray(entry.capabilities)?entry.capabilities:[];
    const perms=entry.permissions||null;
    const isInstalled=installedIds.has(String(entry.id||''));
    const restartRequired=!!(entry.lifecycle&&(entry.lifecycle.restart_required||entry.lifecycle.webui_restart_required));
    const badgesHtml=caps.map(c=>`<span class="extension-gallery-badge">${esc(String(c))}</span>`).join('');
    const metaBits=[];
    if(author) metaBits.push('by '+author);
    if(version) metaBits.push('v'+version);
    const sourceLinkHtml=_extensionSourceLink(entry);
    const metaHtml=(metaBits.length||sourceLinkHtml)
      ? `<div class="extension-gallery-meta">${metaBits.length?`<span>${metaBits.join(' · ')}</span>`:''}${sourceLinkHtml}</div>`
      : '';
    const permsHtml=perms?_extensionPermissionSummary(perms):'';
    const postInstallHtml=_extensionPostInstallNote(entry,isInstalled);
    const actionBtn=isInstalled
      ?`<button class="extension-gallery-uninstall-btn" data-ext-uninstall-id="${id}" type="button" data-i18n="ext_gallery_uninstall">Uninstall</button>`
      :`<button class="extension-gallery-install-btn" data-ext-install-id="${id}" type="button" data-i18n="ext_gallery_install">Install</button>`;
    const installedBadge=isInstalled?'<span class="extension-gallery-installed-badge">Installed</span>':'';
    const card=`<div class="extension-gallery-card">
      <div class="extension-gallery-head">
        <div class="extension-gallery-info">
          <div class="extension-gallery-name">${name}${installedBadge}</div>
          ${metaHtml}
        </div>
      </div>
      <div class="extension-gallery-desc">${desc}</div>
      ${badgesHtml?'<div class="extension-gallery-badge-row">'+badgesHtml+'</div>':''}
      ${postInstallHtml}
      ${permsHtml}
      <div class="extension-gallery-actions">${actionBtn}</div>
    </div>`;
    galleryCards.push(card);
  }
  if(galleryEl) galleryEl.innerHTML=galleryCards.length?galleryCards.join(''):'<div class="extensions-empty">No extensions found.</div>';
  if(installedEl){
    installedEl.innerHTML=_extensionInstalledList(statusData&&statusData.extensions,!!(statusData&&statusData.extension_dir_configured));
    _bindExtensionToggleButtons(installedEl);
    _bindExtensionSettingsButtons(installedEl);
  }
  _bindExtensionGalleryButtons(entries);
}

export function _bindExtensionGalleryButtons(entries){
  const entryMap=new Map();
  if(Array.isArray(entries)) entries.forEach(e=>{if(e&&e.id)entryMap.set(String(e.id),e);});
  document.querySelectorAll('[data-ext-install-id]').forEach(btn=>{
    const entry=entryMap.get(btn.dataset.extInstallId);
    if(entry) btn.addEventListener('click',()=>handleExtensionInstall(btn,entry));
  });
  document.querySelectorAll('[data-ext-uninstall-id]').forEach(btn=>{
    btn.addEventListener('click',()=>handleExtensionUninstall(btn,btn.dataset.extUninstallId));
  });
}

export async function handleExtensionInstall(btn,entry){
  if(!btn||btn.disabled) return;
  const previousText=btn.textContent;
  btn.disabled=true;
  btn.textContent=t('ext_gallery_installing');
  try{
    const result=await api('/api/extensions/install',{method:'POST',body:JSON.stringify({
      id:entry.id,
      download_url:entry.download_url||entry.download,
      sha256:entry.sha256,
    })});
    const restart=!!(entry.lifecycle&&(entry.lifecycle.restart_required||entry.lifecycle.webui_restart_required));
    const hasPostInstall=!!(entry.post_install||(entry.lifecycle&&(entry.lifecycle.sidecar_start_required||entry.lifecycle.native_host_start_required)));
    showToast(restart
      ? t('ext_gallery_install_restart_required')
      : (hasPostInstall?t('ext_gallery_install_followup'):t('ext_gallery_install_ok')));
    state._extensionsGalleryLoaded=false;
    await loadExtensionsGallery();
  }catch(e){
    btn.disabled=false;
    btn.textContent=previousText;
    showToast('Install failed: '+(e&&e.message?e.message:String(e)));
  }
}

export async function handleExtensionUninstall(btn,id){
  if(!btn||btn.disabled) return;
  const previousText=btn.textContent;
  btn.disabled=true;
  btn.textContent='Uninstalling…';
  try{
    await api('/api/extensions/uninstall',{method:'POST',body:JSON.stringify({id})});
    showToast('Extension uninstalled.');
    state._extensionsGalleryLoaded=false;
    await loadExtensionsGallery();
  }catch(e){
    btn.disabled=false;
    btn.textContent=previousText;
    showToast('Uninstall failed: '+(e&&e.message?e.message:String(e)));
  }
}

export async function copyExtensionsDiagnostics(){
  if(!state._extensionsStatusData) return;
  const text=JSON.stringify(state._extensionsStatusData,null,2);
  const success=()=>showToast(t('copied')||'Copied!');
  const fail=()=>showToast(t('copy_failed')||'Copy failed');
  if(typeof _copyText==='function'){
    _copyText(text).then(success).catch(fail);
    return;
  }
  if(typeof navigator!=='undefined'&&navigator&&navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(success).catch(fail);
  }else{
    fail();
  }
}
// ── Plugins panel (read-only plugin/hook visibility) ───────────────────────

export async function handlePluginEnableToggle(pluginKey, checked){
  try{
    const body={dashboard_plugins:{}};
    body.dashboard_plugins[pluginKey]=!!checked;
    await api('/api/settings',{method:'POST',body:JSON.stringify(body)});
    loadPluginsPanel();
  }catch(e){
    showToast(t('settings_save_failed')+e.message);
  }
}

export function _pluginActivationState(plugin){
  const activation=(plugin&&typeof plugin.activation==='string')
    ? plugin.activation
    : (plugin&&plugin.enabled===false ? 'disabled' : 'enabled');
  // Mirror _buildPluginCard's isProviderActive precedence: an explicit
  // is_active_provider===true overrides the activation string so the sort
  // bucket always matches the badge.
  if(plugin&&plugin.is_active_provider===true) return 'provider';
  if(activation==='exclusive'||activation==='provider'){
    if(plugin&&plugin.is_active_provider===false) return 'disabled';
    return 'provider';
  }
  if(activation==='enabled') return 'enabled';
  return 'disabled';
}

export function _partitionPluginsActiveFirst(plugins){
  const active=[];
  const inactive=[];
  for(const p of plugins){
    if(_pluginActivationState(p)==='disabled') inactive.push(p);
    else active.push(p);
  }
  return active.concat(inactive);
}

export async function loadPluginsPanel(){
  const list=$('pluginsList');
  const empty=$('pluginsEmpty');
  if(!list) return;
  try{
    const data=await api('/api/plugins');
    const plugins=Array.isArray((data||{}).plugins)?data.plugins:[];
    // Hide the Plugins tab when no plugins are installed (#3457)
    const tabBtn=document.querySelector('[data-settings-section="plugins"]');
    if(tabBtn) tabBtn.style.display=(data&&data.empty)?'none':'';
    list.innerHTML='';
    if(plugins.length===0){
      list.style.display='none';
      if(empty) empty.style.display='';
      return;
    }
    if(empty) empty.style.display='none';
    list.style.display='';
    for(const plugin of _partitionPluginsActiveFirst(plugins)){
      list.appendChild(_buildPluginCard(plugin));
    }
  }catch(e){
    list.innerHTML='<div style="color:var(--error);padding:12px;font-size:13px">'+t('plugins_load_failed')+esc(e.message||String(e))+'</div>';
  }
}

export function _buildPluginCard(plugin){
  const card=document.createElement('div');
  card.className='provider-card plugin-card';
  card.dataset.plugin=(plugin&&plugin.key)||'';
  // `activation` is the canonical state from /api/plugins (added in #2659).
  // Fall back to the older `enabled` boolean when the field is missing so
  // the panel still works against older backends.
  const activation=(plugin&&typeof plugin.activation==='string')
    ? plugin.activation
    : (plugin&&plugin.enabled===false ? 'disabled' : 'enabled');
  const isProvider=activation==='exclusive'||activation==='provider';
  const hooks=Array.isArray(plugin&&plugin.hooks)?plugin.hooks:[];
  // Provider plugins (memory/web/browser/etc.) register hooks on their
  // category's dispatcher, not the four agent-wide visibility hooks the
  // payload filters to. Show an explanatory line instead of the generic
  // "No registered lifecycle hooks" when the visibility-hook list is empty.
  const hookHtml=hooks.length
    ? hooks.map(h=>`<span class="plugin-hook-badge">${esc(h)}</span>`).join('')
    : '<span class="plugin-hook-empty">'+t(isProvider?'plugins_provider_no_hooks':'plugins_no_hooks')+'</span>';
  const version=(plugin&&plugin.version)?' · v'+esc(plugin.version):'';
  const desc=(plugin&&plugin.description)?esc(plugin.description):t('plugins_no_description');
const enabled=plugin&&plugin.enabled!==false;
  const tab=plugin&&plugin.tab;
  const isDashboardPlugin=!!(tab&&tab.path);
  // No inline onclick/onchange: an inline handler interpolates tab.path/key into
  // a JS-string-in-attribute context where HTML-escaping is insufficient (a
  // crafted value could break out). Render inert markup + bind listeners below
  // with the raw closure values.
  const openBtn=enabled&&tab&&tab.path
    ? `<a href="${esc(tab.path)}" class="plugin-open-btn">${esc(tab.label||plugin.name||'Open')} \u2197</a>`
    : '';
  const toggleHtml=enabled&&isDashboardPlugin
    ? `<div class="plugin-card-footer-row">
         <span class="plugin-toggle-label">${t('plugins_enable_toggle')||'Enabled'}</span>
         <label class="plugin-toggle-switch">
           <input type="checkbox" class="plugin-enable-toggle" checked>
           <span class="plugin-toggle-slider"></span>
         </label>
       </div>`
    : (isDashboardPlugin
    ? `<div class="plugin-card-footer-row">
         <span class="plugin-toggle-label">${t('plugins_enable_toggle')||'Enable'}</span>
         <label class="plugin-toggle-switch">
           <input type="checkbox" class="plugin-enable-toggle">
           <span class="plugin-toggle-slider"></span>
         </label>
       </div>`
    : '');
  const isProviderActive = plugin&&typeof plugin.is_active_provider==='boolean'
    ? plugin.is_active_provider
    : isProvider;
  let badgeText;
  let badgeClass;
  if(isProviderActive){
    badgeText=t('plugins_active_provider');
    badgeClass='plugin-card-badge-provider';
  }else if(activation==='enabled'){
    badgeText=t('plugins_enabled');
    badgeClass='';
  }else{
    badgeText=t('plugins_disabled');
    badgeClass='plugin-card-badge-disabled';
  }
  card.innerHTML=`
    <div class="provider-card-header plugin-card-header">
      <div class="provider-card-info">
        <div class="provider-card-name">${esc((plugin&&plugin.name)||t('plugins_unnamed'))}</div>
        <div class="provider-card-meta">${esc((plugin&&plugin.key)||'plugin')}${version}</div>
      </div>
      <span class="provider-card-badge ${badgeClass}">${badgeText}</span>
    </div>
    <div class="provider-card-body plugin-card-body">
      <div class="provider-card-hint">${desc}</div>
      <div class="provider-card-label">${t('plugins_registered_hooks')}</div>
      <div class="plugin-hook-list">${hookHtml}</div>
      ${openBtn ? `<div class="plugin-card-footer">${openBtn}</div>` : ''}
      ${toggleHtml}
    </div>
  `;
  // Bind handlers with the RAW closure values (not interpolated into inline JS),
  // so a hostile tab.path/key can't break out of a JS-string attribute context.
  if(tab&&tab.path){
    const _openEl=card.querySelector('.plugin-open-btn');
    if(_openEl){
      const _p=tab.path, _l=tab.label||plugin.name;
      _openEl.addEventListener('click', function(ev){ switchPluginPage(ev, _p, _l); });
    }
  }
  if(isDashboardPlugin){
    const _tog=card.querySelector('.plugin-enable-toggle');
    if(_tog){
      const _k=plugin.key;
      _tog.addEventListener('change', function(){ handlePluginEnableToggle(_k, this.checked); });
    }
  }
  return card;
}

// ── Plugin pages ─────────────────────────────────────────────────────────────


export async function switchPluginPage(event, path, label) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!state._currentPluginPage || state._currentPluginPage.path !== path) {
    await _loadPluginPage(path, label);
  }
  // Update state._currentPanel so clicking sidebar items won't short-circuit,
  // but keep the sidebar panel views intact (no panelPlugin exists).
  state._currentPanel = 'plugin';
  const mainEl = document.querySelector('main.main');
  if (mainEl) {
    MAIN_VIEW_PANELS.forEach(p => {
      mainEl.classList.toggle('showing-' + p, p === 'plugin');
    });
  }
}

export async function _loadPluginPage(path, label) {
  const container = $('pluginPageContainer');
  const titleEl = $('pluginPageTitle');
  if (!container) return;
  if (titleEl) titleEl.textContent = label || path;
  container.innerHTML = '';

  // Use an iframe for full isolation (styles, scripts, modals stay sandboxed).
  // Security note: plugins are locally-installed (~/.hermes/plugins/), similar
  // trust model to VS Code extensions — only install plugins you trust.
  const iframe = document.createElement('iframe');
  iframe.src = path;
  iframe.style.cssText = 'width:100%;height:100%;border:none;display:block;';
  iframe.setAttribute('title', label || 'Plugin');
  iframe.setAttribute('sandbox', 'allow-scripts allow-forms allow-popups');
  container.appendChild(iframe);
  state._currentPluginPage = { path, label };
}
