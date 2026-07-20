// Panels domain: MCP, gateway, and checkpoints
window.HermesPanels = window.HermesPanels || {};

// Event wiring


// ── MCP Server Management ──
function _mcpStatusLabel(status){
  const key={
    active:'mcp_status_active',
    configured:'mcp_status_configured',
    disabled:'mcp_status_disabled',
    invalid_config:'mcp_status_invalid_config',
  }[status]||'mcp_status_unknown';
  return t(key);
}
function toggleMcpServer(name, enabled){
  api('/api/mcp/servers/'+encodeURIComponent(name),{
    method:'PATCH',
    body:JSON.stringify({enabled:enabled}),
  }).then(r=>{
    if(r&&r.ok){
      _refreshMcpToolsetsCatalog();
      showToast(t(enabled?'mcp_enabled_toast':'mcp_disabled_toast',name));
    }
    else showToast(t('mcp_toggle_failed'),'error');
    loadMcpServers();
  }).catch(()=>{showToast(t('mcp_toggle_failed'),'error');loadMcpServers();});
}
function _refreshMcpToolsetsCatalog(payload){
  if(typeof window.invalidateToolsetsCatalog==='function') window.invalidateToolsetsCatalog(payload);
}
function loadMcpServers(){
  const list=$('mcpServerList');
  if(!list) return;
  list.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('loading'))}</div>`;
  api('/api/mcp/servers').then(r=>{
    if(!r||!Array.isArray(r.servers)) return;
    _refreshMcpToolsetsCatalog(r);
    if(!r.servers.length){
      list.innerHTML=`<div class="mcp-empty-state" style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('mcp_no_servers'))}</div>`;
      return;
    }
    list.innerHTML=r.servers.map(s=>{
      const transportLabel=s.transport==='http'?'HTTP':s.transport==='stdio'?'stdio':(''+(s.transport||'unknown'));
      const transportClass=s.transport==='http'?'mcp-http':s.transport==='stdio'?'mcp-stdio':'mcp-unknown';
      const transportBadge=`<span class="mcp-transport-badge ${transportClass}">${esc(transportLabel)}</span>`;
      const status=s.status||'configured';
      const statusBadge=`<span class="mcp-status-badge mcp-status-${esc(status)}">${esc(_mcpStatusLabel(status))}</span>`;
      const toolCount=s.tool_count===null||typeof s.tool_count==='undefined'?'—':String(s.tool_count);
      const detail=s.transport==='http'
        ? (s.url||'')
        : (s.transport==='stdio'?`${s.command||''} ${Array.isArray(s.args)?s.args.join(' '):''}`:t('mcp_status_invalid_config'));
      const envInfo=s.env?Object.entries(s.env).map(([k,v])=>`${k}=${v}`).join(', '):'';
      const headersInfo=s.headers?Object.entries(s.headers).map(([k,v])=>`${k}=${v}`).join(', '):'';
      const secretInfo=[envInfo,headersInfo].filter(Boolean).join(' | ');
      const isEnabled=s.enabled!==false;
      const encodedName=encodeURIComponent(s.name).replace(/'/g,"\\'");
      const toggleBtn=r.toggle_supported
        ?`<button type="button" class="mcp-toggle-btn ${isEnabled?'mcp-toggle-enabled':'mcp-toggle-disabled'}" title="${esc(t(isEnabled?'mcp_disable_server':'mcp_enable_server'))}" onclick="toggleMcpServer('${encodedName}',${!isEnabled})">${esc(t(isEnabled?'mcp_enabled_yes':'mcp_enabled_no'))}</button>`
        :`<span>${esc(t(isEnabled?'mcp_enabled_yes':'mcp_enabled_no'))}</span>`;
      return `<div class="mcp-server-row">
        <div class="mcp-server-row-head">
          <span class="mcp-server-name">${esc(s.name)}</span>
          ${transportBadge}
          ${statusBadge}
        </div>
        <div class="mcp-server-detail">${esc(detail)}${secretInfo?' | '+esc(secretInfo):''}</div>
        <div class="mcp-server-meta"><span class="mcp-tool-count">${esc(t('mcp_tool_count',toolCount))}</span>${toggleBtn}</div>
      </div>`;
    }).join('');
  }).catch(()=>{list.innerHTML=`<div class="mcp-error-state" style="color:#ef4444;font-size:12px;padding:6px 0">${esc(t('mcp_load_failed'))}</div>`});
}
let _mcpToolsCache=[];
let _mcpToolsMeta={};
let _mcpToolsPage=1;
let _mcpToolsPageSize=5;
const MCP_TOOLS_PAGE_SIZE_OPTIONS=[5,10,20,40];
function _filterMcpToolsForSearch(tools, query){
  const q=(query||'').trim().toLowerCase();
  if(!q) return Array.isArray(tools)?tools:[];
  return (Array.isArray(tools)?tools:[]).filter(tool=>{
    const hay=[tool.name,tool.server,tool.description].map(v=>String(v||'').toLowerCase()).join(' ');
    return hay.includes(q);
  });
}
function _mcpToolSchemaText(schemaSummary){
  if(!Array.isArray(schemaSummary)||!schemaSummary.length) return t('mcp_tools_schema_empty');
  return schemaSummary.map(p=>{
    const req=p.required?'*':'';
    const desc=p.description?` — ${p.description}`:'';
    return `${p.name}${req}: ${p.type||'unknown'}${desc}`;
  }).join('\n');
}
function _mcpToolsSummary(total, filtered, page, pages, query){
  const trimmedQuery=(query||'').trim();
  if(!filtered){
    if(trimmedQuery) return t('mcp_tools_summary_no_matches',trimmedQuery,total);
    return total?t('mcp_tools_summary_none'):'';
  }
  const pageSize=_mcpToolsPageSize||5;
  const start=(page-1)*pageSize+1;
  const end=Math.min(filtered,page*pageSize);
  const searchNote=trimmedQuery?t('mcp_tools_summary_matching',trimmedQuery):'';
  const totalNote=filtered===total?'':t('mcp_tools_summary_total_note',total);
  return t('mcp_tools_summary_showing',start,end,filtered,searchNote,totalNote,page,pages);
}
function _mcpToolPageSizeControl(){
  const options=MCP_TOOLS_PAGE_SIZE_OPTIONS.map(size=>`<option value="${size}" ${size===_mcpToolsPageSize?'selected':''}>${size}</option>`).join('');
  return `<label class="mcp-tool-page-size">${esc(t('mcp_tools_page_size_prefix'))} <select aria-label="${esc(t('mcp_tools_per_page_aria'))}" onchange="setMcpToolsPageSize(this.value)">${options}</select> ${esc(t('mcp_tools_page_size_suffix'))}</label>`;
}
function _mcpToolsEmptyMessage(query){
  const base=esc(t(query?'mcp_tools_no_matches':'mcp_tools_no_tools'));
  const unavailable=Array.isArray(_mcpToolsMeta.unavailable_servers)?_mcpToolsMeta.unavailable_servers:[];
  if(query||!unavailable.length) return base;
  return `${base}<br><span class="mcp-tool-empty-detail">${esc(t('mcp_tools_inactive_configured_servers',unavailable.join(', ')))}</span>`;
}
function _renderMcpToolPager(filteredCount, page, pages){
  const pager=$('mcpToolPager');
  if(!pager) return;
  if(pages<=1){
    pager.innerHTML='';
    return;
  }
  pager.innerHTML=`<button type="button" class="mcp-tool-page-btn" onclick="setMcpToolsPage(${page-1})" ${page<=1?'disabled':''} aria-label="${esc(t('mcp_tools_previous_page_aria'))}">${esc(t('mcp_tools_previous_page'))}</button>
    <span class="mcp-tool-page-label">${page} / ${pages}</span>
    <button type="button" class="mcp-tool-page-btn" onclick="setMcpToolsPage(${page+1})" ${page>=pages?'disabled':''} aria-label="${esc(t('mcp_tools_next_page_aria'))}">${esc(t('mcp_tools_next_page'))}</button>`;
}
function _renderMcpTools(tools, query){
  const list=$('mcpToolList');
  const toolbar=$('mcpToolToolbar');
  if(!list) return;
  const filtered=_filterMcpToolsForSearch(tools, query);
  const total=Array.isArray(tools)?tools.length:0;
  const pages=Math.max(1,Math.ceil(filtered.length/_mcpToolsPageSize));
  _mcpToolsPage=Math.min(Math.max(1,_mcpToolsPage||1),pages);
  if(toolbar) toolbar.innerHTML=`<span class="mcp-tool-summary">${esc(_mcpToolsSummary(total,filtered.length,_mcpToolsPage,pages,query))}</span>${_mcpToolPageSizeControl()}`;
  _renderMcpToolPager(filtered.length,_mcpToolsPage,pages);
  if(!filtered.length){
    list.innerHTML=`<div class="mcp-tool-empty-state" style="color:var(--muted);font-size:12px;padding:6px 0">${_mcpToolsEmptyMessage(query)}</div>`;
    return;
  }
  const visible=filtered.slice((_mcpToolsPage-1)*_mcpToolsPageSize,_mcpToolsPage*_mcpToolsPageSize);
  list.innerHTML=visible.map(tool=>{
    const status=tool.status||'unknown';
    const statusBadge=`<span class="mcp-status-badge mcp-status-${esc(status)}">${esc(_mcpStatusLabel(status))}</span>`;
    const schemaText=_mcpToolSchemaText(tool.schema_summary);
    return `<div class="mcp-tool-row">
      <div class="mcp-server-row-head">
        <span class="mcp-tool-name">${esc(tool.name)}</span>
        <span class="mcp-tool-server">${esc(tool.server||'unknown')}</span>
        ${statusBadge}
      </div>
      <div class="mcp-server-detail">${esc(tool.description||'')}</div>
      <pre class="mcp-tool-schema">${esc(schemaText)}</pre>
    </div>`;
  }).join('');
}
function setMcpToolsPage(page){
  _mcpToolsPage=page;
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
  const list=$('mcpToolList');
  if(list) list.scrollTop=0;
}
function setMcpToolsPageSize(size){
  const next=Number(size);
  if(!MCP_TOOLS_PAGE_SIZE_OPTIONS.includes(next)) return;
  _mcpToolsPageSize=next;
  _mcpToolsPage=1;
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
  const list=$('mcpToolList');
  if(list) list.scrollTop=0;
}
function filterMcpTools(){
  _mcpToolsPage=1;
  const input=$('mcpToolSearch');
  _renderMcpTools(_mcpToolsCache,input?input.value:'');
  const list=$('mcpToolList');
  if(list) list.scrollTop=0;
}
function loadMcpTools(){
  const list=$('mcpToolList');
  const toolbar=$('mcpToolToolbar');
  const pager=$('mcpToolPager');
  if(!list) return;
  if(toolbar) toolbar.textContent='';
  if(pager) pager.innerHTML='';
  list.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:6px 0">${esc(t('loading'))}</div>`;
  api('/api/mcp/tools').then(r=>{
    _mcpToolsCache=(r&&Array.isArray(r.tools))?r.tools:[];
    _mcpToolsMeta=r||{};
    _mcpToolsPage=1;
    filterMcpTools();
  }).catch(()=>{list.innerHTML=`<div class="mcp-tool-error-state" style="color:#ef4444;font-size:12px;padding:6px 0">${esc(t('mcp_tools_load_failed'))}</div>`});
}
let _gatewayActionInFlight=false;
function _gatewayActionButton(action){
  const labels={start:t('gateway_start'),stop:t('gateway_stop'),restart:t('gateway_restart')};
  return `<button class="sm-btn gateway-action-btn" data-gateway-action="${esc(action)}" onclick="_gatewayAction('${esc(action)}')" ${_gatewayActionInFlight?'disabled':''} style="padding:5px 10px;font-size:12px">${esc(labels[action]||action)}</button>`;
}
function _gatewayActionControls(r){
  const actions=(r&&r.running)?['stop','restart']:['start'];
  return `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${actions.map(_gatewayActionButton).join('')}</div>`;
}
function _renderGatewayStatus(r){
  const card=$('gatewayStatusCard');
  if(!card||!r) return;
  if(!r.configured){
    card.innerHTML=`<div style="color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#f59e0b;display:inline-block"></span>${esc(t('gateway_not_configured'))}</div>${_gatewayActionControls(r)}`;
    return;
  }
  if(!r.running){
    const reason = _gatewayStatusReason(r);
    const statusLabel = reason === 'gateway_stale_running_state'
      ? t('gateway_metadata_stale')
      : reason === 'remote_gateway_unreachable'
        ? t('gateway_endpoint_unreachable')
        : t('gateway_not_running');
    card.innerHTML=`<div style="color:var(--muted);font-size:12px;display:flex;align-items:center;gap:6px"><span style="width:8px;height:8px;border-radius:50%;background:#ef4444;display:inline-block"></span>${esc(statusLabel)}</div>${_gatewayActionControls(r)}`;
    return;
  }
  const platformIcons={telegram:'💬',discord:'🎮',slack:'📝',web:'🌐',api:'🔌'};
  let badges='';
  if(r.platforms&&r.platforms.length){
    badges=r.platforms.map(p=>{
      const icon=platformIcons[p.name]||'📡';
      return `<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;background:var(--code-bg);border:1px solid var(--border2);border-radius:12px;font-size:12px;font-weight:500">${icon} ${esc(p.label)}</span>`;
    }).join(' ');
  }
  const lastActive=r.last_active?`<span style="font-size:11px;color:var(--muted)">${esc(t('gateway_last_active'))}: ${esc(new Date(r.last_active).toLocaleString())}</span>`:'';
  const sessionInfo=r.session_count?`<span style="font-size:11px;color:var(--muted)">${r.session_count} ${esc(r.session_count!==1?t('gateway_sessions'):t('gateway_session'))}</span>`:'';
  card.innerHTML=`<div style="display:flex;align-items:center;gap:6px;margin-bottom:8px"><span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block"></span><span style="font-size:13px;font-weight:500;color:#22c55e">${esc(t('gateway_running'))}</span></div>${badges?`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${badges}</div>`:''}<div style="display:flex;gap:12px">${sessionInfo}${lastActive}</div>${_gatewayActionControls(r)}`;
}
function loadGatewayStatus(){
  const card=$('gatewayStatusCard');
  if(!card) return;
  return api('/api/gateway/status').then(r=>_renderGatewayStatus(r)).catch(()=>{card.innerHTML=`<div style="color:#ef4444;font-size:12px">${esc(t('gateway_status_load_failed'))}</div>`});
}
async function _gatewayAction(action){
  if(_gatewayActionInFlight) return;
  _gatewayActionInFlight=true;
  const buttons=[...document.querySelectorAll('.gateway-action-btn')];
  buttons.forEach(btn=>{btn.disabled=true;});
  try{
    const result=await api(`/api/gateway/${encodeURIComponent(action)}`,{method:'POST',body:JSON.stringify({}),timeoutMs:70000,timeoutToast:false});
    if(typeof showToast==='function') showToast(result&&result.message?result.message:t(`gateway_${action}_success`),3000,'success');
  }catch(e){
    const msg=e&&e.message?e.message:String(e||'');
    if(typeof showToast==='function') showToast(`${t(`gateway_${action}_failed`)}${msg?': '+msg:''}`,5000,'error');
  }finally{
    _gatewayActionInFlight=false;
    await loadGatewayStatus();
  }
}
// Load MCP servers when system settings tab opens
const _origSwitchSettings=switchSettingsSection;
switchSettingsSection=function(name, opts){
  _origSwitchSettings(name, opts);
  if(name==='preferences') updateNotificationPermissionStatus();
  if(name==='system'){loadMcpServers();loadMcpTools();loadGatewayStatus();}
};

// ── Checkpoints / Rollback ──────────────────────────────────────────────────

async function _loadCheckpoints(workspace){
  const container=$('checkpointListContainer');
  if(!container) return;
  try{
    const data=await api(`/api/rollback/list?workspace=${encodeURIComponent(workspace)}`);
    const checkpoints=data.checkpoints||[];
    if(!checkpoints.length){
      container.innerHTML=`<div style="color:var(--muted);font-size:12px;padding:8px 0">${esc(t('checkpoint_empty'))}</div>`;
      return;
    }
    let html='';
    for(const ck of checkpoints){
      const shortId=ck.id||ck.commit||'?';
      const msg=ck.message||'checkpoint';
      const date=ck.date_display||ck.date||'';
      const files=ck.files||0;
      html+=`
        <div class="detail-row" style="align-items:center;padding:6px 0;border-bottom:1px solid var(--border,rgba(255,255,255,0.08))">
          <div style="flex:1;min-width:0">
            <div style="font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(msg)}">${esc(msg)}</div>
            <div style="font-size:11px;color:var(--muted);margin-top:2px">
              <code style="font-size:10px">${esc(shortId)}</code>
              ${date ? ` · ${esc(date)}` : ''}
              ${files ? ` · ${esc(t('checkpoint_files'))}: ${files}` : ''}
            </div>
          </div>
          <div style="display:flex;gap:4px;flex-shrink:0;margin-left:8px">
            <button class="panel-head-btn" title="${esc(t('checkpoint_view_diff'))}" onclick="event.stopPropagation();_viewCheckpointDiff('${esc(workspace)}','${esc(ck.id)}')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            </button>
            <button class="panel-head-btn" title="${esc(t('checkpoint_restore'))}" onclick="event.stopPropagation();_restoreCheckpoint('${esc(workspace)}','${esc(ck.id)}','${esc(msg.replace(/'/g,"\\'"))}')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
            </button>
          </div>
        </div>`;
    }
    container.innerHTML=html;
  }catch(e){
    container.innerHTML=`<div style="color:var(--error,#f87171);font-size:12px;padding:8px 0">${esc(t('checkpoint_error'))}: ${esc(e.message)}</div>`;
  }
}

async function _viewCheckpointDiff(workspace,checkpoint){
  const modal=document.getElementById('checkpointDiffModal');
  if(!modal){
    const m=document.createElement('div');
    m.id='checkpointDiffModal';
    m.style.cssText='position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)';
    m.innerHTML=`
      <div style="background:var(--bg,${getComputedStyle(document.documentElement).getPropertyValue('--bg')||'#1a1a2e'});border:1px solid var(--border,rgba(255,255,255,0.12));border-radius:12px;width:90vw;max-width:800px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4)">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border,rgba(255,255,255,0.08))">
          <div id="checkpointDiffModalTitle" style="font-weight:600;font-size:14px"></div>
          <button onclick="document.getElementById('checkpointDiffModal').style.display='none'" style="background:none;border:none;color:var(--fg);cursor:pointer;font-size:18px;padding:0 4px">&times;</button>
        </div>
        <div id="checkpointDiffModalBody" style="flex:1;overflow:auto;padding:12px 16px">
          <div style="color:var(--muted);font-size:12px">${esc(t('checkpoint_loading'))}</div>
        </div>
      </div>`;
    m.onclick=(e)=>{if(e.target===m) m.style.display='none';};
    document.body.appendChild(m);
  }
  modal.style.display='flex';
  $('checkpointDiffModalTitle').textContent=t('checkpoint_diff_title');
  $('checkpointDiffModalBody').innerHTML=`<div style="color:var(--muted);font-size:12px">${esc(t('checkpoint_loading'))}</div>`;
  try{
    const data=await api(`/api/rollback/diff?workspace=${encodeURIComponent(workspace)}&checkpoint=${encodeURIComponent(checkpoint)}`);
    const body=$('checkpointDiffModalBody');
    if(!data.total_changes){
      body.innerHTML=`<div style="color:var(--muted);font-size:12px">${esc(t('checkpoint_diff_no_changes'))}</div>`;
      return;
    }
    let html=`<div style="font-size:12px;margin-bottom:8px">${esc(t('checkpoint_diff_files_changed',data.total_changes))}</div>`;
    if(data.files_changed){
      html+='<div style="margin-bottom:8px">';
      for(const f of data.files_changed){
        const icon=f.status==='deleted'?'−':'~';
        const color=f.status==='deleted'?'var(--error,#f87171)':'var(--accent,#60a5fa)';
        html+=`<div style="font-size:12px;padding:2px 0"><span style="color:${color};font-weight:bold;margin-right:6px">${icon}</span><code style="font-size:11px">${esc(f.file)}</code></div>`;
      }
      html+='</div>';
    }
    if(data.diff){
      html+=`<pre style="background:var(--bg-secondary,rgba(0,0,0,0.3));border:1px solid var(--border,rgba(255,255,255,0.08));border-radius:8px;padding:12px;font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;word-break:break-all;max-height:50vh;overflow-y:auto;color:var(--fg)">${esc(data.diff)}</pre>`;
    }
    body.innerHTML=html;
  }catch(e){
    $('checkpointDiffModalBody').innerHTML=`<div style="color:var(--error,#f87171);font-size:12px">${esc(e.message)}</div>`;
  }
}

async function _restoreCheckpoint(workspace,checkpoint,message){
  const label=message||checkpoint;
  const ok=await showConfirmDialog({title:t('checkpoint_restore_confirm_title'),message:t('checkpoint_restore_confirm_message',label),confirmLabel:t('checkpoint_restore'),danger:true,focusCancel:true});
  if(!ok) return;
  try{
    const data=await api('/api/rollback/restore',{method:'POST',body:JSON.stringify({workspace,checkpoint})});
    if(data&&data.ok){
      showToast(t('checkpoint_restored')+(data.files_restored_count?` (${data.files_restored_count} ${t('checkpoint_files').toLowerCase()})`:''));
    }else{
      showToast((data&&data.error)||'Restore failed','error');
    }
  }catch(e){
    showToast(t('checkpoint_restore')+': '+e.message,'error');
  }
}


function updateNotificationPermissionStatus(){
  const el=$('notificationPermissionStatus');
  const btn=$('notificationPermissionButton');
  const btnWrap=$('notificationPermissionButtonWrap');
  if(!el) return;
  if(!('Notification' in window)){
    const unsupported=t('notifications_unsupported');
    el.textContent=unsupported;
    if(btn){
      btn.disabled=true;
      btn.title='';
      btn.setAttribute('aria-label', unsupported);
      btn.setAttribute('aria-disabled','true');
    }
    if(btnWrap) btnWrap.title=unsupported;
    return;
  }
  const perm=Notification.permission||'default';
  const label=t('notifications_permission_status', perm);
  el.textContent=label;
  if(btn){
    const granted=perm==='granted';
    btn.disabled=granted;
    btn.title=granted?'':label;
    btn.setAttribute('aria-label', label);
    btn.setAttribute('aria-disabled', granted?'true':'false');
  }
  if(btnWrap) btnWrap.title=label;
}

window.HermesPanels.gateway = {
  _mcpStatusLabel,
  toggleMcpServer,
  _refreshMcpToolsetsCatalog,
  loadMcpServers,
  _filterMcpToolsForSearch,
  _mcpToolSchemaText,
  _mcpToolsSummary,
  _mcpToolPageSizeControl,
  _mcpToolsEmptyMessage,
  _renderMcpToolPager,
  _renderMcpTools,
  setMcpToolsPage,
  setMcpToolsPageSize,
  filterMcpTools,
  loadMcpTools,
  _gatewayActionButton,
  _gatewayActionControls,
  _renderGatewayStatus,
  loadGatewayStatus,
  _gatewayAction,
  _loadCheckpoints,
  _viewCheckpointDiff,
  _restoreCheckpoint,
  updateNotificationPermissionStatus,
};
