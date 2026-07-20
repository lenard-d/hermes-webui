import { state } from "./state.js";


// Panels domain: provider cards and actions

const _providerCardEls = new Map(); // providerId → entry used by save/remove/test handlers
const _SELF_HOSTED_DEFAULT_BASE_URLS = Object.freeze({
  ollama: 'http://localhost:11434/v1',
  lmstudio: 'http://localhost:1234/v1',
});

export async function _fetchProviderQuotaStatus(force=false){
  const endpoint=force?`/api/provider/quota?refresh=1&ts=${Date.now()}`:'/api/provider/quota';
  const status=await api(endpoint,{cache:'no-store'});
  if(status&&typeof status==='object') status.client_fetched_at=new Date().toISOString();
  return status;
}

export async function loadProvidersPanel(){
  const list=$('providersList');
  const empty=$('providersEmpty');
  if(!list) return;
  try{
    const data=await api('/api/providers');
    const quota=await _fetchProviderQuotaStatus(false).catch(e=>({ok:false,status:'unavailable',quota:null,message:e.message||t('provider_quota_unavailable'),client_fetched_at:new Date().toISOString()}));
    const providers=(data.providers||[]).filter(p=>p.configurable||p.is_oauth||p.is_custom||p.is_plugin_provider||p.is_self_hosted);
    list.innerHTML='';
    _providerCardEls.clear();
    const quotaCard=_buildProviderQuotaCard(quota);
    if(quotaCard){
      list.appendChild(quotaCard);
      renderProviderCostChart(quotaCard); // async, fire-and-forget
    }
    if(providers.length===0){
      list.style.display='none';
      if(empty) empty.style.display='';
      return;
    }
    if(empty) empty.style.display='none';
    list.style.display='';
    for(const p of providers){
      list.appendChild(_buildProviderCard(p));
    }
  }catch(e){
    list.innerHTML='<div style="color:var(--error);padding:12px;font-size:13px">Failed to load providers: '+esc(e.message||String(e))+'</div>';
  }
}

export async function _refreshProviderQuota(card,button){
  if(!card) return;
  if(button){
    button.disabled=true;
    button.textContent=t('provider_quota_refreshing');
    button.setAttribute('aria-busy','true');
  }
  let failed=false;
  let next;
  try{
    next=await _fetchProviderQuotaStatus(true);
    failed=next&&next.ok===false;
  }catch(e){
    failed=true;
    next={ok:false,status:'unavailable',quota:null,message:e.message||t('provider_quota_unavailable'),client_fetched_at:new Date().toISOString()};
  }
  try{
    const fresh=_buildProviderQuotaCard(next);
    if(fresh){
      card.replaceWith(fresh);
      // Re-render the 7-day spend chart onto the rebuilt card — the quota
      // refresh replaces the whole card, which would otherwise drop the chart
      // until the next full panel reload (#3600).
      renderProviderCostChart(fresh); // async, fire-and-forget
      if(typeof showToast==='function') showToast(failed?t('provider_quota_refresh_failed'):t('provider_quota_refresh_succeeded'));
      return;
    }
  }catch(e){
    failed=true;
  }
  if(card.isConnected&&button){
    button.disabled=false;
    button.textContent=t('provider_quota_refresh_usage');
    button.removeAttribute('aria-busy');
  }
  if(typeof showToast==='function') showToast(t('provider_quota_refresh_failed'));
}

export function _formatProviderQuotaMoney(value){
  if(value===null||value===undefined||value==='') return '—';
  const n=Number(value);
  if(!Number.isFinite(n)) return '—';
  return '$'+n.toFixed(2);
}

export function _formatProviderQuotaPercent(value){
  if(value===null||value===undefined||value==='') return '—';
  const n=Number(value);
  if(!Number.isFinite(n)) return '—';
  return Math.max(0,Math.min(100,Math.round(n)))+'%';
}

export function _formatProviderQuotaReset(value){
  if(!value) return '';
  const d=new Date(value);
  if(Number.isNaN(d.getTime())) return '';
  try{return d.toLocaleString();}catch(e){return value;}
}

export function _formatProviderQuotaWindowLabel(accountLimits,w){
  const raw=((w&&w.label)||t('provider_quota_window_fallback')).trim();
  const provider=((accountLimits&&accountLimits.provider)||'').toLowerCase();
  if(provider==='openai-codex'){
    if(raw.toLowerCase()==='session') return t('provider_quota_session_limit');
    if(raw.toLowerCase()==='weekly') return t('provider_quota_weekly_limit');
  }
  return raw||t('provider_quota_window_fallback');
}

export function _formatProviderQuotaLastChecked(status){
  const accountLimits=status&&status.account_limits;
  const value=(accountLimits&&accountLimits.fetched_at)||status&&status.client_fetched_at;
  if(!value) return t('provider_quota_last_checked_after_refresh');
  const d=new Date(value);
  if(Number.isNaN(d.getTime())) return t('provider_quota_last_checked_after_refresh');
  try{return t('provider_quota_last_checked',d.toLocaleString());}catch(e){return t('provider_quota_last_checked',value);}
}

export function _providerQuotaStateClass(value){
  return String(value||'unavailable').replace(/[^a-z0-9_-]/gi,'').toLowerCase()||'unavailable';
}

export function _providerQuotaStatusLabel(value){
  const state=_providerQuotaStateClass(value);
  const key={
    available:'provider_quota_status_available',
    exhausted:'provider_quota_status_exhausted',
    unavailable:'provider_quota_status_unavailable',
    failed:'provider_quota_status_failed',
    checked:'provider_quota_status_checked',
    no_key:'provider_quota_status_no_key',
    invalid_key:'provider_quota_status_invalid_key',
    unsupported:'provider_quota_status_unsupported',
  }[state];
  return key?t(key):state.replace(/_/g,' ');
}

export function _providerQuotaWindowMeta(used,reset){
  const meta=[];
  if(used!=='—') meta.push(t('provider_quota_used_meta',used));
  if(reset) meta.push(t('provider_quota_resets_meta',reset));
  return meta;
}

export function _providerQuotaRetryAfterText(value){
  const retry=_formatProviderQuotaReset(value);
  return retry?t('provider_quota_retry_after',retry):'';
}

export function _providerQuotaUnavailableReason(credential){
  const structured=_providerQuotaRetryAfterText(credential&&credential.retry_after);
  if(structured) return structured;
  const raw=String((credential&&credential.unavailable_reason)||'').trim();
  const match=raw.match(/\bretry after\s+([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?)/i);
  if(match){
    const parsed=_providerQuotaRetryAfterText(match[1]);
    if(parsed) return parsed;
  }
  return raw;
}

export function _providerQuotaPoolShouldDefaultOpen(pool){
  try{
    const saved=localStorage.getItem('hermes-provider-quota-pool-open');
    if(saved==='1') return true;
    if(saved==='0') return false;
  }catch(e){}
  const count=Array.isArray(pool&&pool.credentials)?pool.credentials.length:0;
  return count>0&&count<=3;
}

export function _buildProviderQuotaPoolBreakdown(accountLimits){
  const pool=accountLimits&&accountLimits.pool;
  if(!pool||!Array.isArray(pool.credentials)||pool.credentials.length===0) return '';
  const defaultOpen=_providerQuotaPoolShouldDefaultOpen(pool);
  const total=Number.isFinite(Number(pool.total_credentials))?Number(pool.total_credentials):pool.credentials.length;
  const available=Number.isFinite(Number(pool.available_credentials))?Number(pool.available_credentials):pool.credentials.filter(c=>c&&c.status==='available').length;
  const exhausted=Number.isFinite(Number(pool.exhausted_credentials))?Number(pool.exhausted_credentials):0;
  const failed=Number.isFinite(Number(pool.failed_credentials))?Number(pool.failed_credentials):0;
  const queried=Number.isFinite(Number(pool.queried_credentials))?Number(pool.queried_credentials):0;
  const summaryParts=[t('provider_quota_pool_summary_available',available,total)];
  if(exhausted>0) summaryParts.push(t('provider_quota_pool_summary_exhausted',exhausted));
  if(failed>0) summaryParts.push(t('provider_quota_pool_summary_failed',failed));
  if(queried>0) summaryParts.push(t('provider_quota_pool_summary_checked',queried));
  const planParts=Array.isArray(pool.plans)?pool.plans.filter(Boolean):[];
  const rows=pool.credentials.map((credential,idx)=>{
    const label=(credential&&credential.label)||t('provider_quota_credential_label',idx+1);
    const status=_providerQuotaStateClass(credential&&credential.status);
    const statusText=_providerQuotaStatusLabel(credential&&credential.status);
    const plan=credential&&credential.plan?` · ${credential.plan}`:'';
    const windows=Array.isArray(credential&&credential.windows)?credential.windows:[];
    const details=Array.isArray(credential&&credential.details)?credential.details.filter(Boolean):[];
    const unavailableReason=_providerQuotaUnavailableReason(credential);
    const windowHtml=windows.length?windows.map(w=>{
      const remaining=_formatProviderQuotaPercent(w&&w.remaining_percent);
      const used=_formatProviderQuotaPercent(w&&w.used_percent);
      const reset=_formatProviderQuotaReset(w&&w.reset_at);
      const meta=_providerQuotaWindowMeta(used,reset);
      const detail=(w&&w.detail)?String(w.detail).trim():'';
      return `<div class="provider-quota-pool-window"><span>${esc(_formatProviderQuotaWindowLabel(accountLimits,w))}</span><strong>${esc(remaining)}</strong>${meta.length?`<small>${esc(meta.join(' · '))}</small>`:''}${detail?`<small class="provider-quota-window-detail">${esc(detail)}</small>`:''}</div>`;
    }).join(''):`<div class="provider-quota-pool-note">${esc(unavailableReason||t('provider_quota_pool_no_windows'))}</div>`;
    const detailHtml=details.length?`<div class="provider-quota-pool-details">${details.map(d=>`<span>${esc(d)}</span>`).join('')}</div>`:'';
    return `
      <div class="provider-quota-pool-row provider-quota-pool-row-${status}">
        <div class="provider-quota-pool-row-head">
          <span>${esc(label)}${esc(plan)}</span>
          <strong>${esc(statusText)}</strong>
        </div>
        <div class="provider-quota-pool-windows">${windowHtml}</div>
        ${detailHtml}
      </div>
    `;
  }).join('');
  const planText=planParts.length?`<div class="provider-quota-pool-plans">${esc(t('provider_quota_pool_plans',planParts.join(', ')))}</div>`:'';
  return `
    <details class="provider-quota-pool"${defaultOpen?' open':''}>
      <summary><span class="provider-quota-pool-summary-label"><span class="provider-quota-pool-chevron" aria-hidden="true"></span><span>${esc(t('provider_quota_credential_pool'))}</span></span><strong>${esc(summaryParts.join(' · '))}</strong></summary>
      ${planText}
      <div class="provider-quota-pool-rows">${rows}</div>
    </details>
  `;
}

export function _buildProviderQuotaCard(status){
  if(!status) return null;
  const card=document.createElement('div');
  const state=(status.status||'unavailable').replace(/[^a-z0-9_-]/gi,'').toLowerCase()||'unavailable';
  card.className='provider-quota-card provider-quota-card-'+state;
  const accountLimits=status.account_limits||null;
  const providerBase=status.display_name||status.provider||t('provider_quota_active_provider');
  const provider=(accountLimits&&accountLimits.plan)?`${providerBase} · ${accountLimits.plan}`:providerBase;
  const quota=status.quota||null;
  let body='';
  if(accountLimits&&(status.status==='available'||accountLimits.pool)){
    const windows=Array.isArray(accountLimits.windows)?accountLimits.windows:[];
    const details=Array.isArray(accountLimits.details)&&!accountLimits.pool?accountLimits.details:[];
    const windowHtml=windows.map(w=>{
      const used=_formatProviderQuotaPercent(w&&w.used_percent);
      const reset=_formatProviderQuotaReset(w&&w.reset_at);
      const meta=_providerQuotaWindowMeta(used,reset);
      const detail=(w&&w.detail)?String(w.detail).trim():'';
      return `
        <div class="provider-quota-metric provider-quota-window">
          <span>${esc(_formatProviderQuotaWindowLabel(accountLimits,w))}</span>
          <strong>${esc(_formatProviderQuotaPercent(w&&w.remaining_percent))}</strong>
          ${meta.length?`<small>${esc(meta.join(' · '))}</small>`:''}
          ${detail?`<small class="provider-quota-window-detail">${esc(detail)}</small>`:''}
        </div>
      `;
    }).join('');
    const detailHtml=details.length
      ? `<div class="provider-quota-details">${details.map(d=>`<span>${esc(d)}</span>`).join('')}</div>`
      : '';
    const poolHtml=_buildProviderQuotaPoolBreakdown(accountLimits);
    body=windowHtml+detailHtml+poolHtml;
    if(!body) body=`<div class="provider-quota-message">${esc(status.message||t('provider_quota_account_limits_loaded'))}</div>`;
  }else if(status.status==='available'&&quota){
    body=`
      <div class="provider-quota-metric"><span>${esc(t('provider_quota_metric_remaining'))}</span><strong>${esc(_formatProviderQuotaMoney(quota.limit_remaining))}</strong></div>
      <div class="provider-quota-metric"><span>${esc(t('provider_quota_metric_used'))}</span><strong>${esc(_formatProviderQuotaMoney(quota.usage))}</strong></div>
      <div class="provider-quota-metric"><span>${esc(t('provider_quota_metric_limit'))}</span><strong>${esc(_formatProviderQuotaMoney(quota.limit))}</strong></div>
    `;
  }else{
    body=`<div class="provider-quota-message">${esc(status.message||t('provider_quota_unavailable'))}</div>`;
  }
  card.innerHTML=`
    <div class="provider-quota-header">
      <div>
        <div class="provider-quota-title">${esc(t('provider_quota_title'))}</div>
        <div class="provider-quota-subtitle">${esc(provider)}</div>
        <div class="provider-quota-checked">${esc(_formatProviderQuotaLastChecked(status))}</div>
      </div>
      <div class="provider-quota-actions">
        <span class="provider-quota-badge">${esc(_providerQuotaStatusLabel(state))}</span>
        <button class="provider-quota-refresh" type="button" data-provider-quota-refresh title="${esc(t('provider_quota_refresh_title'))}">${esc(t('provider_quota_refresh_usage'))}</button>
      </div>
    </div>
    <div class="provider-quota-body">${body}</div>
  `;
  const refreshBtn=card.querySelector('[data-provider-quota-refresh]');
  if(refreshBtn) refreshBtn.addEventListener('click',()=>_refreshProviderQuota(card,refreshBtn));
  const poolDetails=card.querySelector('.provider-quota-pool');
  if(poolDetails){
    poolDetails.addEventListener('toggle',()=>{
      try{localStorage.setItem('hermes-provider-quota-pool-open',poolDetails.open?'1':'0');}catch(e){}
    });
  }
  return card;
}

export async function renderProviderCostChart(card){
  let history;
  try{
    history=await api('/api/provider/cost-history?provider=openrouter');
  }catch(e){
    return; // silently skip if endpoint unavailable
  }
  const body=card.querySelector('.provider-quota-body');
  if(!body||body.querySelector('.provider-cost-chart-wrap')) return;
  if(!history||history.ok===false){
    const wrap=document.createElement('div');
    wrap.className='provider-cost-chart-wrap';
    _attachBudgetControls(wrap,history||{},card,0);
    body.appendChild(wrap);
    return;
  }
  const snaps=Array.isArray(history.snapshots)?history.snapshots:[];
  // need at least 2 snapshots to have one non-null delta
  const hasData=snaps.filter(s=>s.delta!=null).length>=1;
  if(!hasData){
    const empty=document.createElement('div');
    empty.className='provider-cost-chart-wrap';
    empty.innerHTML='<div class="provider-cost-chart-title">7-day spend</div><div class="provider-quota-message">Not enough data yet. Cost chart builds after 2 daily snapshots.</div>';
    body.appendChild(empty);
    _attachBudgetControls(empty,history,card,0);
    return;
  }
  const maxDelta=Math.max(...snaps.map(s=>s.delta!=null?Number(s.delta):0),1e-9);
  const nonNull=snaps.filter(s=>s.delta!=null).map(s=>Number(s.delta));
  const avg=nonNull.length?nonNull.reduce((a,b)=>a+b,0)/nonNull.length:0;
  const paceNum=avg*30;
  const pace='$'+paceNum.toFixed(2);
  const bars=snaps.map(s=>{
    const delta=s.delta!=null?Number(s.delta):null;
    const pct=delta!=null?Math.max((delta/maxDelta)*100,delta>0?2:0).toFixed(1):'0';
    const label=String(s.date||'').slice(5);
    const tip=delta!=null?`${s.date} · $${delta.toFixed(4)}`:`${s.date} · no baseline`;
    return `<div class="insights-daily-bar" title="${esc(tip)}"><div class="insights-daily-stack" aria-label="${esc(tip)}"><div class="insights-daily-bar-input" style="height:${pct}%"></div></div><span>${esc(label)}</span></div>`;
  }).join('');
  const wrap=document.createElement('div');
  wrap.className='provider-cost-chart-wrap';
  wrap.innerHTML=`<div class="provider-cost-chart-title">7-day spend <span class="provider-cost-chart-pace">Monthly pace: ${esc(pace)}</span></div><div class="provider-cost-chart-bars insights-daily-token-chart">${bars}</div>`;
  const monthly_budget=history&&history.monthly_budget!=null?history.monthly_budget:null;
  if(monthly_budget!=null&&paceNum>0){
    const paceSpan=wrap.querySelector('.provider-cost-chart-pace');
    if(paceSpan){
      const pct=Math.round((paceNum/monthly_budget)*100);
      const pctSpan=document.createElement('span');
      pctSpan.className='provider-cost-chart-pct'+(pct>=100?' over':pct>=80?' warn':'');
      pctSpan.textContent=`(${pct}%)`;
      paceSpan.appendChild(pctSpan);
    }
  }
  body.appendChild(wrap);
  _attachBudgetControls(wrap,history,card,paceNum);
}

export function _attachBudgetControls(wrap,history,card,paceNum){
  const budget=history&&history.monthly_budget!=null?Number(history.monthly_budget):null;
  const row=document.createElement('div');
  row.className='provider-cost-budget-row';

  const titleDiv=document.createElement('div');
  titleDiv.className='provider-cost-budget-title';
  titleDiv.textContent=t('provider_cost_budget_label');
  row.appendChild(titleDiv);

  const inputGroup=document.createElement('div');
  inputGroup.className='provider-cost-budget-input-group';

  const prefix=document.createElement('span');
  prefix.className='provider-cost-budget-prefix';
  prefix.textContent='$';
  inputGroup.appendChild(prefix);

  const input=document.createElement('input');
  input.type='number';
  input.min='0.01';
  input.step='0.01';
  input.className='provider-cost-budget-input';
  input.placeholder=t('provider_cost_budget_placeholder')||'e.g. 50.00';
  if(budget!=null) input.value=budget.toFixed(2);
  inputGroup.appendChild(input);

  const setBtn=document.createElement('button');
  setBtn.type='button';
  setBtn.className='provider-cost-budget-set';
  setBtn.textContent=t('provider_cost_budget_set');
  inputGroup.appendChild(setBtn);

  const clearBtn=document.createElement('button');
  clearBtn.type='button';
  clearBtn.className='provider-cost-budget-clear';
  clearBtn.textContent=t('provider_cost_budget_clear');
  if(budget==null) clearBtn.style.display='none';
  inputGroup.appendChild(clearBtn);

  row.appendChild(inputGroup);

  if(budget!=null&&paceNum>0){
    const pct=Math.round((paceNum/budget)*100);
    const barWrap=document.createElement('div');
    barWrap.className='provider-cost-budget-bar-wrap';
    const bar=document.createElement('div');
    bar.className='provider-cost-budget-bar';
    const fill=document.createElement('div');
    fill.className='provider-cost-budget-bar-fill'+(pct>=100?' over':pct>=80?' warn':'');
    fill.style.width=Math.min(100,pct)+'%';
    bar.appendChild(fill);
    barWrap.appendChild(bar);
    const pctLabel=document.createElement('span');
    pctLabel.className='provider-cost-budget-pct-label';
    pctLabel.textContent=t('provider_cost_budget_pct',pct,budget.toFixed(2));
    barWrap.appendChild(pctLabel);
    row.appendChild(barWrap);
  }

  wrap.appendChild(row);

  async function _saveBudget(value){
    try{
      await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_cost_budget:value})});
      const existing=card.querySelector('.provider-cost-chart-wrap');
      if(existing) existing.remove();
      renderProviderCostChart(card);
    }catch(e){
      if(typeof showToast==='function') showToast(t('provider_cost_budget_save_failed'));
    }
  }

  setBtn.addEventListener('click',()=>{
    const val=parseFloat(input.value);
    if(!isFinite(val)||val<=0) return;
    _saveBudget(val);
  });

  clearBtn.addEventListener('click',()=>{
    _saveBudget(null);
  });
}
export function _buildProviderCard(p){
  const card=document.createElement('div');
  card.className='provider-card';
  card.dataset.provider=p.id;
  // Use the is_oauth flag from the backend — it reflects _OAUTH_PROVIDERS in providers.py.
  // key_source can be 'oauth' (hermes auth), 'config_yaml' (token in config.yaml), or 'none'.
  const isOauth=p.is_oauth===true;
  // models_total reflects the complete catalog (e.g. 396 for a large-tier
  // Nous Portal account). The "models" array may be trimmed to a featured
  // subset for UI scannability — fall back to its length only when the
  // server didn't supply models_total (older builds, custom providers).
  const modelCount=Number.isFinite(p.models_total)
    ? p.models_total
    : (Array.isArray(p.models) ? p.models.length : 0);
  const sourceLabel=p.key_source==='oauth'
    ? t('providers_status_oauth')
    : p.key_source==='config_yaml'
      ? t('providers_status_configured')||'Configured'
      : (p.has_key ? t('providers_status_api_key') : t('providers_status_not_configured_label'));
  const metaParts=[];
  if(modelCount>0) metaParts.push(modelCount+(modelCount===1?' model':' models'));
  metaParts.push(sourceLabel);
  const metaText=metaParts.join(' · ');

  // Clickable header (toggles body)
  const header=document.createElement('button');
  header.type='button';
  header.className='provider-card-header';
  header.innerHTML=`
    <div class="provider-card-info">
      <div class="provider-card-name">${esc(p.display_name)}</div>
      <div class="provider-card-meta">${esc(metaText)}</div>
    </div>
    ${p.has_key?`<span class="provider-card-badge">${esc(t('providers_status_configured'))}</span>`:''}
    <svg class="provider-card-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" width="16" height="16"><path d="M6 9l6 6 6-6"/></svg>
  `;
  card.appendChild(header);

  const body=document.createElement('div');
  body.className='provider-card-body';

  if(isOauth){
    const hint=document.createElement('div');
    hint.className='provider-card-hint';
    if(p.key_source==='config_yaml'){
      hint.textContent=t('providers_oauth_config_yaml_hint')||'Token configured via config.yaml. To update, edit the providers section in your config.yaml or run hermes auth.';
    } else if(p.auth_error){
      hint.textContent=p.auth_error;
      hint.style.color='var(--accent)';
    } else if(p.has_key){
      hint.textContent=t('providers_oauth_hint');
    } else {
      hint.textContent=t('providers_oauth_not_configured_hint')||'Not authenticated. Run hermes auth in the terminal to configure this provider.';
      hint.style.color='var(--muted)';
    }
    body.appendChild(hint);
    card.appendChild(body);
    header.addEventListener('click',()=>card.classList.toggle('open'));
    return card;
  }

  let input=null;
  let focusInput=null;
  let saveBtn=null;
  if(p.is_self_hosted){
    const defaultBaseUrl=_SELF_HOSTED_DEFAULT_BASE_URLS[p.id];
    const baseUrlField=document.createElement('div');
    baseUrlField.className='provider-card-field';
    const baseUrlLabel=document.createElement('label');
    baseUrlLabel.className='provider-card-label';
    baseUrlLabel.textContent='Base URL';
    baseUrlField.appendChild(baseUrlLabel);
    const baseUrlRow=document.createElement('div');
    baseUrlRow.className='provider-card-row';
    const baseUrlInput=document.createElement('input');
    baseUrlInput.type='text';
    baseUrlInput.className='provider-card-input';
    baseUrlInput.placeholder=defaultBaseUrl||'http://localhost:11434/v1';
    baseUrlInput.value=(p.base_url||'').trim()||defaultBaseUrl||'';
    baseUrlInput.autocomplete='off';
    const testBtn=document.createElement('button');
    testBtn.type='button';
    testBtn.className='provider-card-btn provider-card-btn-ghost';
    testBtn.textContent='Test connection';
    const probeStatus=document.createElement('div');
    probeStatus.className='provider-card-hint';
    baseUrlRow.appendChild(baseUrlInput);
    baseUrlRow.appendChild(testBtn);
    baseUrlField.appendChild(baseUrlRow);
    baseUrlField.appendChild(probeStatus);

    const keyField=document.createElement('div');
    keyField.className='provider-card-field';
    const keyLabel=document.createElement('label');
    keyLabel.className='provider-card-label';
    keyLabel.textContent='API key (optional)';
    keyField.appendChild(keyLabel);
    const keyRow=document.createElement('div');
    keyRow.className='provider-card-row';
    const keyInput=document.createElement('input');
    keyInput.type='password';
    keyInput.className='provider-card-input';
    keyInput.autocomplete='off';
    keyInput.placeholder='Optional';
    keyRow.appendChild(keyInput);
    keyField.appendChild(keyRow);

    const modelField=document.createElement('div');
    modelField.className='provider-card-field';
    const modelLabel=document.createElement('label');
    modelLabel.className='provider-card-label';
    modelLabel.textContent=t('providers_status_model')||'Model';
    modelField.appendChild(modelLabel);
    const modelRow=document.createElement('div');
    modelRow.className='provider-card-row';
    const modelInput=document.createElement('input');
    modelInput.type='text';
    modelInput.className='provider-card-input';
    modelInput.autocomplete='off';
    modelInput.placeholder='model id';
    const modelDatalist=document.createElement('datalist');
    const modelListId='providerModelList-'+p.id;
    modelDatalist.id=modelListId;
    modelInput.setAttribute('list',modelListId);
    const setModelChoices=(models)=>{
      modelDatalist.innerHTML='';
      const choices=Array.isArray(models)?models:[];
      for(const model of choices){
        const modelId=model&&model.id?model.id:model;
        const option=document.createElement('option');
        option.value=modelId;
        modelDatalist.appendChild(option);
      }
    };
    const initialModelChoices=Array.isArray(p.models)?p.models:[];
    setModelChoices(initialModelChoices);

    const saveRow=document.createElement('div');
    saveRow.className='provider-card-row';
    saveRow.style.marginTop='6px';
    saveBtn=document.createElement('button');
    saveBtn.type='button';
    saveBtn.className='provider-card-btn provider-card-btn-primary';
    saveBtn.textContent=t('providers_save');
    saveBtn.onclick=()=>_saveSelfHostedProvider(p.id);
    saveBtn.disabled=true;
    saveRow.appendChild(saveBtn);
    if(p.has_key){
      const removeBtn=document.createElement('button');
      removeBtn.type='button';
      removeBtn.className='provider-card-btn provider-card-btn-danger';
      removeBtn.textContent=t('providers_remove');
      removeBtn.onclick=()=>_removeProviderKey(p.id);
      saveRow.appendChild(removeBtn);
    }
    modelRow.appendChild(modelInput);
    modelField.appendChild(modelRow);
    body.appendChild(baseUrlField);
    body.appendChild(keyField);
    body.appendChild(modelField);
    body.appendChild(saveRow);
    body.appendChild(modelDatalist);
    card.appendChild(body);

    const checkSaveEnabled=()=>{
      const hasUrl=baseUrlInput.value.trim().length>0;
      const hasModel=modelInput.value.trim().length>0;
      saveBtn.disabled=!(hasUrl&&hasModel);
    };
    baseUrlInput.addEventListener('input',checkSaveEnabled);
    modelInput.addEventListener('input',checkSaveEnabled);
    checkSaveEnabled();

    _providerCardEls.set(p.id,{
      card,
      baseUrlInput,
      apiKeyInput:keyInput,
      modelInput,
      modelDatalist,
      saveBtn,
      testBtn,
      probeStatus,
      isSelfHosted:true,
      setModelChoices,
      updateSaveState:checkSaveEnabled,
    });
    focusInput=modelInput;
    testBtn.onclick=()=>_testSelfHostedConnection(p.id);
    header.addEventListener('click',e=>{
      if(e.target.closest('.provider-card-body')) return;
      card.classList.toggle('open');
      if(card.classList.contains('open')) setTimeout(()=>focusInput&&focusInput.focus(),0);
    });
    return card;
  }

  if(p.configurable){
    const field=document.createElement('div');
    field.className='provider-card-field';
    const label=document.createElement('label');
    label.className='provider-card-label';
    label.textContent=t('providers_status_api_key');
    field.appendChild(label);

    const row=document.createElement('div');
    row.className='provider-card-row';
    input=document.createElement('input');
    input.type='password';
    input.className='provider-card-input';
    input.placeholder=p.has_key?t('providers_key_placeholder_replace'):t('providers_key_placeholder_new');
    input.autocomplete='off';
    const toggleBtn=document.createElement('button');
    toggleBtn.type='button';
    toggleBtn.className='provider-card-btn provider-card-btn-ghost';
    toggleBtn.textContent='Show';
    toggleBtn.onclick=()=>{
      const revealed=input.type==='text';
      input.type=revealed?'password':'text';
      toggleBtn.textContent=revealed?'Show':'Hide';
    };
    saveBtn=document.createElement('button');
    saveBtn.type='button';
    saveBtn.className='provider-card-btn provider-card-btn-primary';
    saveBtn.textContent=t('providers_save');
    saveBtn.onclick=()=>_saveProviderKey(p.id);
    saveBtn.disabled=true;
    row.appendChild(input);
    row.appendChild(toggleBtn);
    row.appendChild(saveBtn);
    if(p.has_key){
      const removeBtn=document.createElement('button');
      removeBtn.type='button';
      removeBtn.className='provider-card-btn provider-card-btn-danger';
      removeBtn.textContent=t('providers_remove');
      removeBtn.onclick=()=>_removeProviderKey(p.id);
      row.appendChild(removeBtn);
    }
    field.appendChild(row);
    body.appendChild(field);
    focusInput=input;

  }else{
    const hint=document.createElement('div');
    hint.className='provider-card-hint';
    hint.textContent=p.is_custom
      ? 'Custom provider loaded from config.yaml / hermes model. Edit it from the CLI or config file.'
      : 'Provider is managed outside the WebUI.';
    body.appendChild(hint);
  }

  // Model list — show when provider has known models
  if(modelCount>0){
    const modelSection=document.createElement('div');
    modelSection.className='provider-card-models';
    const modelLabel=document.createElement('div');
    modelLabel.className='provider-card-label';
    modelLabel.textContent='Models';
    modelSection.appendChild(modelLabel);
    const modelList=document.createElement('div');
    modelList.className='provider-card-model-tags';
    const renderedModels=Array.isArray(p.models)?p.models:[];
    for(const m of renderedModels){
      const tag=document.createElement('span');
      tag.className='provider-card-model-tag';
      tag.textContent=m.id||m.label||m;
      modelList.appendChild(tag);
    }
    // When the rendered list is a strict subset of the total catalog (Nous
    // Portal large-tier accounts hit this with ~400-model catalogs), show
    // a "+N more" trailing pill so the user knows the picker is intentionally
    // capped — and they can still reach the full catalog via the /model
    // slash command (its autocomplete consumes the un-trimmed list from
    // /api/models's extra_models field). #1567.
    const totalCount=Number.isFinite(p.models_total)?p.models_total:renderedModels.length;
    const hiddenCount=Math.max(0, totalCount - renderedModels.length);
    if(hiddenCount>0){
      const more=document.createElement('span');
      more.className='provider-card-model-tag provider-card-model-tag-more';
      more.textContent='+'+hiddenCount+' more';
      more.title='The /model slash command can autocomplete every model in this provider\'s catalog.';
      modelList.appendChild(more);
    }
    modelSection.appendChild(modelList);
    body.appendChild(modelSection);
  }

  // Refresh models for this provider
  const refreshRow=document.createElement('div');
  refreshRow.className='provider-card-row';
  refreshRow.style.marginTop='6px';
  const refreshBtn=document.createElement('button');
  refreshBtn.type='button';
  refreshBtn.className='provider-card-btn provider-card-btn-ghost';
  refreshBtn.style.display='flex';
  refreshBtn.style.alignItems='center';
  refreshBtn.style.gap='5px';
  refreshBtn.innerHTML=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg> ${t('providers_refresh_models')||'Refresh Models'}`;
  refreshBtn.onclick=()=>_refreshProviderModels(p.id, refreshBtn);
  refreshRow.appendChild(refreshBtn);
  body.appendChild(refreshRow);
  card.appendChild(body);

  if(input&&saveBtn){
    _providerCardEls.set(p.id,{card,input,saveBtn,hasKey:p.has_key});
    input.addEventListener('input',()=>{saveBtn.disabled=!input.value.trim();});
  }
  header.addEventListener('click',e=>{
    // Don't toggle when clicking inside body (defensive; body isn't inside header)
    if(e.target.closest('.provider-card-body')) return;
    card.classList.toggle('open');
    if(card.classList.contains('open')) setTimeout(()=>focusInput&&focusInput.focus(),0);
  });
  return card;
}
export async function _saveProviderKey(providerId){
  const els=_providerCardEls.get(providerId);
  if(!els) return;
  const key=els.input.value.trim();
  if(!key){
    showToast(t('providers_enter_key'));
    return;
  }
  els.saveBtn.disabled=true;
  els.saveBtn.textContent=t('providers_saving');
  try{
    const res=await api('/api/providers',{method:'POST',body:JSON.stringify({provider:providerId,api_key:key})});
    if(res.ok){
      showToast(res.provider+' key '+res.action);
      els.input.value='';
      // Invalidate every dropdown surface that caches /api/models so the
      // newly-configured provider's models show up without a server restart
      // or page reload (#1539). Server-side invalidate_models_cache() is
      // already called by api/providers.py:set_provider_key.
      _refreshModelDropdownsAfterProviderChange();
      await loadProvidersPanel(); // refresh list
    }else{
      showToast(res.error||'Failed to save key');
      els.saveBtn.disabled=false;
      els.saveBtn.textContent=t('providers_save');
    }
  }catch(e){
    showToast('Error: '+e.message);
    els.saveBtn.disabled=false;
    els.saveBtn.textContent=t('providers_save');
  }
}

export async function _removeProviderKey(providerId){
  const els=_providerCardEls.get(providerId);
  if(!els) return;
  if(els.saveBtn){els.saveBtn.disabled=true;els.saveBtn.textContent=t('providers_removing');}
  try{
    const res=await api('/api/providers/delete',{method:'POST',body:JSON.stringify({provider:providerId})});
    if(res.ok){
      showToast(res.provider+' key '+t('providers_key_removed').toLowerCase());
      // Drop the removed provider from every cached dropdown surface so it
      // disappears immediately — composer picker, /model slash command,
      // Settings → Default Model, configured-model badges (#1539).
      // Without this, a stale list from before the delete keeps offering
      // the now-removed provider's models until the page is reloaded.
      _refreshModelDropdownsAfterProviderChange();
      await loadProvidersPanel(); // refresh list
    }else{
      showToast(res.error||'Failed to remove key');
      if(els.saveBtn){els.saveBtn.disabled=false;els.saveBtn.textContent=t('providers_save');}
    }
  }catch(e){
    // A 403 from /api/providers/delete fires when the CSRF cookie/header
    // pair has drifted. The server distinguishes three reasons in
    // api/routes.py:_csrf_rejection_error ("Session expired - reload the
    // page", "Cross-origin mismatch - check reverse proxy headers", and
    // the fallback "Cross-origin request rejected"); api()'s catch lifts
    // that string onto e.message. Pass it through verbatim so the
    // deployment-shape failure #2572 calls out keeps its actionable hint
    // instead of being flattened to a single generic toast.
    if(e&&e.status===403){
      showToast(e.message||'Session expired. Reload the page and try again.',6000,'error');
    }else{
      showToast('Error: '+e.message);
    }
    if(els.saveBtn){els.saveBtn.disabled=false;els.saveBtn.textContent=t('providers_save');}
  }
}

export async function _testSelfHostedConnection(providerId){
  const els=_providerCardEls.get(providerId);
  if(!els||!els.isSelfHosted) return;
  const baseUrl=(els.baseUrlInput.value||'').trim();
  const apiKey=(els.apiKeyInput.value||'').trim();
  if(!baseUrl){
    showToast('Base URL is required');
    return;
  }

  const testBtn=els.testBtn;
  if(!testBtn) return;
  const prevLabel=testBtn.textContent;
  testBtn.disabled=true;
  testBtn.textContent='Testing...';
  if(els.probeStatus){
    els.probeStatus.style.color='var(--muted)';
    els.probeStatus.textContent='Testing connection...';
  }

  try{
    const res=await api('/api/onboarding/probe',{
      method:'POST',
      body:JSON.stringify({provider:providerId,base_url:baseUrl,api_key:apiKey||undefined}),
    });
    if(res&&res.ok){
      const models=Array.isArray(res.models)?res.models:[];
      if(els.setModelChoices){
        els.setModelChoices(models);
      }
      if(els.probeStatus){
        const count=models.length;
        els.probeStatus.style.color='var(--ok)';
        els.probeStatus.textContent=`Connected. ${count} model(s) available.`;
      }
      if(!els.modelInput.value&&models.length&&models[0]){
        els.modelInput.value=models[0].id||models[0];
      }
      if(els.updateSaveState){
        els.updateSaveState();
      }
    }else{
      const err=(res&&res.error)||'unreachable';
      const detail=(res&&res.detail)?` (${res.detail})`:'';
      if(els.probeStatus){
        els.probeStatus.style.color='var(--accent)';
        els.probeStatus.textContent=`${err}${detail}`;
      }
      showToast(`Connection test failed: ${err}`);
    }
  }catch(e){
    if(els.probeStatus){
      els.probeStatus.style.color='var(--accent)';
      els.probeStatus.textContent=e&&e.message?e.message:'Connection test failed';
    }
    showToast('Connection test failed: '+(e&&e.message||'request error'));
  }finally{
    testBtn.disabled=false;
    testBtn.textContent=prevLabel;
  }
}

export async function _saveSelfHostedProvider(providerId){
  const els=_providerCardEls.get(providerId);
  if(!els||!els.isSelfHosted) return;
  const baseUrl=(els.baseUrlInput.value||'').trim();
  const key=(els.apiKeyInput.value||'').trim();
  const model=(els.modelInput.value||'').trim();
  if(!baseUrl){
    showToast('Base URL is required');
    return;
  }
  if(!model){
    showToast('Model is required');
    return;
  }
  if(!els.saveBtn) return;
  const saveBtn=els.saveBtn;
  const prevLabel=saveBtn.textContent;
  saveBtn.disabled=true;
  saveBtn.textContent='Saving...';
  try{
    const payload={provider:providerId,base_url:baseUrl,model:model};
    if(key) payload.api_key=key;
    const res=await api('/api/providers/self-hosted',{method:'POST',body:JSON.stringify(payload)});
    if(res&&res.ok){
      showToast(`${res.provider} configured`);
      if(els.apiKeyInput) els.apiKeyInput.value='';
      _refreshModelDropdownsAfterProviderChange();
      await loadProvidersPanel();
    }else{
      showToast(res&&res.error||'Failed to save provider');
      saveBtn.disabled=false;
      saveBtn.textContent=prevLabel;
    }
  }catch(e){
    showToast('Error: '+(e&&e.message||'Failed to save provider'));
    saveBtn.disabled=false;
    saveBtn.textContent=prevLabel;
  }
}

// Shared dropdown-cache flush invoked after a provider add/remove. The
// server-side TTL cache is already invalidated by /api/providers and
// /api/providers/delete (via api/providers.py:set_provider_key); this
// flushes the JS-side caches so the next render rebuilds from a fresh
// /api/models response. Wrapped in a try/catch so a UI module that hasn't
// loaded yet (e.g. during early Settings open) cannot break the save flow.
export function _refreshModelDropdownsAfterProviderChange(){
  try{
    if(typeof window._invalidateSlashModelCache==='function'){
      window._invalidateSlashModelCache();
    }
    // Fire-and-forget: don't block the providers panel refresh on a
    // dropdown rebuild. The composer/Settings dropdowns will catch up
    // on the very next paint frame.
    if(typeof window._ensureModelDropdownReady==='function'){
      window._modelDropdownReady=null;
      Promise.resolve(window._ensureModelDropdownReady()).catch(()=>{});
    }else if(typeof populateModelDropdown==='function'){
      Promise.resolve(populateModelDropdown()).catch(()=>{});
    }
  }catch(_e){
    // Swallow — dropdown refresh is best-effort, providers panel must still update.
  }
}

export async function _refreshProviderModels(providerId, btn){
  btn.disabled=true;
  const orig=btn.innerHTML;
  btn.innerHTML=`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg> ${t('providers_refreshing')||'Refreshing...'}`;
  try{
    const res=await api('/api/models/refresh',{method:'POST',body:JSON.stringify({provider:providerId})});
    if(res.ok){
      showToast(t('providers_models_refreshed')||('Models refreshed for '+res.provider));
      _refreshModelDropdownsAfterProviderChange();
    }else{
      showToast(res.error||'Failed to refresh models');
    }
  }catch(e){
    showToast(e.status===404?'Refresh not available for this provider.':(e.message||'Failed to refresh models'));
  }finally{
    btn.disabled=false;
    btn.innerHTML=orig;
  }
}
