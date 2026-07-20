import { state } from "./state.js";



// Panels domain: logs and insights

// ── Logs panel ──
export function _selectedLogsFile() {
  const el = $('logsFile');
  const value = (el && el.value) || 'agent';
  return ['agent','errors','gateway'].includes(value) ? value : 'agent';
}

export function _selectedLogsTail() {
  const el = $('logsTail');
  const value = Number((el && el.value) || 200);
  return [100,200,500,1000].includes(value) ? value : 200;
}

export function _severityForLine(line) {
  const text = String(line || '').toUpperCase();
  if (/\b(ERROR|CRITICAL|TRACEBACK)\b/.test(text)) return 'error';
  if (/\b(WARNING|WARN)\b/.test(text)) return 'warning';
  if (/\b(DEBUG)\b/.test(text)) return 'debug';
  if (/\b(INFO)\b/.test(text)) return 'info';
  return 'other';
}

export function _filteredLogsLines() {
  if (state._logsSeverityFilter === 'all') return state._lastLogsLines;
  return state._lastLogsLines.filter(line => {
    const sev = _severityForLine(line);
    if (state._logsSeverityFilter === 'errors') return sev === 'error';
    if (state._logsSeverityFilter === 'warnings') return sev === 'warning' || sev === 'error';
    return true;
  });
}

export function _applyLogsSeverityFilter() {
  const el = $('logsSeverityFilter');
  state._logsSeverityFilter = (el && el.value) || 'all';
  // Re-render from cached lines without re-fetching
  _renderLogs({ lines: state._lastLogsLines, hint: '', truncated: false, _fromFilter: true });
}

export function _logLineSeverityClass(line) {
  const text = String(line || '').toUpperCase();
  if (/\b(WARNING|WARN)\b/.test(text)) return 'log-line-warning';
  if (/\b(DEBUG)\b/.test(text)) return 'log-line-debug';
  if (/\b(INFO)\b/.test(text)) return 'log-line-info';
  if (/\b(ERROR|CRITICAL|TRACEBACK)\b/.test(text)) return 'log-line-error';
  return '';
}

export function _syncLogsWrap() {
  const out = $('logsOutput');
  const wrap = $('logsWrap');
  if (out && wrap) out.classList.toggle('wrap', !!wrap.checked);
}

export async function loadLogs(animate) {
  const box = $('logsOutput');
  const status = $('logsStatus');
  const refreshBtn = $('logsRefreshBtn');
  if (!box) return;
  if (animate && refreshBtn) {
    refreshBtn.style.opacity = '0.5';
    refreshBtn.disabled = true;
  }
  const file = _selectedLogsFile();
  const tail = _selectedLogsTail();
  try {
    if (status) status.textContent = t('logs_loading');
    const data = await api('/api/logs?file=' + encodeURIComponent(file) + '&tail=' + encodeURIComponent(tail));
    _renderLogs(data);
  } catch(e) {
    state._lastLogsLines = [];
    box.innerHTML = `<div class="logs-empty">${esc(t('error_prefix') + e.message)}</div>`;
    if (status) status.textContent = t('logs_load_failed');
  } finally {
    if (animate && refreshBtn) {
      refreshBtn.style.opacity = '';
      refreshBtn.disabled = false;
    }
    _syncLogsAutoRefresh();
  }
}

export function _renderLogs(data) {
  const box = $('logsOutput');
  const status = $('logsStatus');
  if (!box) return;
  const rawLines = Array.isArray(data && data.lines) ? data.lines : [];
  // Only update cache when loading fresh data (not when re-rendering from filter)
  if (data && !data._fromFilter) state._lastLogsLines = rawLines.slice();
  const displayLines = _filteredLogsLines();
  const hint = data && data.hint ? `<div class="logs-hint">${esc(data.hint)}</div>` : '';
  const truncated = data && data.truncated ? `<div class="logs-hint warn">${esc(t('logs_truncated_hint'))}</div>` : '';
  const filterNote = state._logsSeverityFilter !== 'all'
    ? `<div class="logs-hint">${esc(displayLines.length + ' / ' + state._lastLogsLines.length + ' ' + t('logs_filter_active'))}</div>`
    : '';
  if (!displayLines.length) {
    box.innerHTML = `${hint}${truncated}${filterNote}<div class="logs-empty">${esc(t('logs_empty'))}</div>`;
  } else {
    box.innerHTML = `${hint}${truncated}${filterNote}` + displayLines.map(line => {
      const cls = _logLineSeverityClass(line);
      return `<div class="log-line ${cls}">${esc(line)}</div>`;
    }).join('');
  }
  _syncLogsWrap();
  if (status) {
    const bytes = data && Number(data.total_bytes || 0);
    const when = data && data.mtime ? new Date(data.mtime * 1000).toLocaleString() : t('logs_no_mtime');
    status.textContent = `${rawLines.length} / ${data.tail || _selectedLogsTail()} lines · ${bytes.toLocaleString()} bytes · ${when}`;
  }
}

export function _startLogsAutoRefresh() {
  if (state._logsAutoRefreshTimer) return;
  state._logsAutoRefreshTimer = setInterval(() => {
    if (state._currentPanel !== 'logs') { _stopLogsAutoRefresh(); return; }
    const toggle = $('logsAutoRefresh');
    if (toggle && !toggle.checked) return;
    loadLogs(false);
  }, 5000);
}

export function _stopLogsAutoRefresh() {
  if (state._logsAutoRefreshTimer) {
    clearInterval(state._logsAutoRefreshTimer);
    state._logsAutoRefreshTimer = null;
  }
}

export function _syncLogsAutoRefresh() {
  const toggle = $('logsAutoRefresh');
  if (state._currentPanel === 'logs' && (!toggle || toggle.checked)) _startLogsAutoRefresh();
  else _stopLogsAutoRefresh();
}

export async function copyLogsAll() {
  const lines = _filteredLogsLines();
  const text = lines.join('\n');
  try {
    await _copyText(text);
    showToast(t('logs_copied'));
  } catch(e) {
    showToast(t('copy_failed'), 'error');
  }
}
// ── Insights panel ──
const STATIC_MODEL_HEALTH_ROWS = [
  {id:'openai/gpt-5.4-mini', provider:'OpenAI', inputCostPerM:0.25, outputCostPerM:2.00, replacement:'Default economical general-purpose model'},
  {id:'openai/gpt-5.4', provider:'OpenAI', inputCostPerM:2.00, outputCostPerM:10.00, replacement:'Use for complex synthesis; fall back to Mini for routine turns'},
  {id:'anthropic/claude-sonnet-4.5', provider:'Anthropic', inputCostPerM:3.00, outputCostPerM:15.00, replacement:'Strong coding and analysis option; use Mini for low-risk chat'},
  {id:'google/gemini-2.5-pro', provider:'Google', inputCostPerM:1.25, outputCostPerM:10.00, replacement:'Long-context research option; use Flash for speed-sensitive work'},
  {id:'google/gemini-2.5-flash', provider:'Google', inputCostPerM:0.30, outputCostPerM:2.50, replacement:'Low-latency replacement for lighter multimodal or research turns'},
];

export function _renderModelHealthCost(row) {
  const input = Number(row.inputCostPerM || 0);
  const output = Number(row.outputCostPerM || 0);
  return `$${input.toFixed(2)} / $${output.toFixed(2)}`;
}

export function _renderStaticModelHealthTable() {
  const rows = STATIC_MODEL_HEALTH_ROWS.map(row => `<div class="insights-table-row">
    <span class="insights-model-name" title="${esc(row.id)}">${esc(row.id)}</span>
    <span>${esc(row.provider)}</span>
    <span>${esc(_renderModelHealthCost(row))}</span>
    <span class="insights-model-health-replacement">${esc(row.replacement)}</span>
  </div>`).join('');
  return `<details class="insights-card insights-model-health-card"><summary><span class="insights-card-title">${esc(t('insights_model_health_title'))}</span></summary><div class="insights-table insights-model-health-table"><div class="insights-table-head"><span>${esc(t('insights_model_name'))}</span><span>${esc(t('insights_model_health_provider'))}</span><span>${esc(t('insights_model_health_cost_per_m'))}</span><span>${esc(t('insights_model_health_replacement'))}</span></div>${rows}</div></details>`;
}

export async function loadInsights(animate) {
  const box = $('insightsContent');
  const refreshBtn = $('insightsRefreshBtn');
  if (!box) return;
  if (animate && refreshBtn) {
    refreshBtn.style.opacity = '0.5';
    refreshBtn.disabled = true;
  }
  const period = ($('insightsPeriod') || {}).value || '30';
  try {
    const [data, wikiStatus, skillUsage] = await Promise.all([
      api(`/api/insights?days=${period}`),
      api('/api/wiki/status').catch(err => ({status:'error', error: err.message || String(err)})),
      api('/api/skills/usage').catch(() => ({usage:{}, skill_names:[], total_invocations:0, unique_skills_used:0})),
    ]);
    _renderInsights(data, box, wikiStatus, skillUsage);
    if (typeof _syncSystemHealthMonitorVisibility === 'function') _syncSystemHealthMonitorVisibility();
    if (typeof pollSystemHealth === 'function') void pollSystemHealth();
  } catch(e) {
    box.innerHTML = `<div style="color:var(--accent);font-size:12px">${esc(t('error_prefix') + e.message)}</div>`;
  } finally {
    if (animate && refreshBtn) {
      refreshBtn.style.opacity = '';
      refreshBtn.disabled = false;
    }
  }
}

export function _formatLlmWikiTimestamp(value) {
  if (!value) return 'Never';
  try { return new Date(value).toLocaleString(); }
  catch (_) { return String(value); }
}

export function _renderSystemHealthPanel() {
  return `
    <section class="insights-card system-health-panel loading" id="systemHealthPanel" aria-label="Host resource health" aria-live="polite">
      <div class="system-health-head">
        <div>
          <div class="insights-card-title">System health</div>
          <div class="system-health-sub">Current VPS resource usage</div>
        </div>
        <span class="system-health-status" id="systemHealthStatus"><span class="system-health-dot" aria-hidden="true"></span>Loading…</span>
      </div>
      <div class="system-health-metrics">
        <div class="system-health-metric" data-system-health-metric="cpu">
          <div class="system-health-label"><span>CPU</span><span class="system-health-value" data-system-health-value>—</span></div>
          <div class="system-health-bar" role="progressbar" aria-label="CPU usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="system-health-bar-fill"></div></div>
        </div>
        <div class="system-health-metric" data-system-health-metric="memory">
          <div class="system-health-label"><span>RAM</span><span class="system-health-value" data-system-health-value>—</span></div>
          <div class="system-health-bar" role="progressbar" aria-label="RAM usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="system-health-bar-fill"></div></div>
        </div>
        <div class="system-health-metric" data-system-health-metric="disk">
          <div class="system-health-label"><span>Disk</span><span class="system-health-value" data-system-health-value>—</span></div>
          <div class="system-health-bar" role="progressbar" aria-label="Disk usage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="system-health-bar-fill"></div></div>
        </div>
      </div>
      <div class="system-health-foot">Live snapshot only; historical resource charts can build on this surface later.</div>
    </section>`;
}

export function _renderLlmWikiStatus(d) {
  const status = d || {status:'error'};
  const isReady = status.available && status.status === 'ready';
  const isEmpty = status.available && status.status === 'empty';
  const isError = status.status === 'error';
  const badgeClass = isReady ? 'ok' : isError ? 'err' : isEmpty ? 'warn' : 'muted';
  const badgeText = isReady ? 'Available' : isError ? 'Error' : isEmpty ? 'Empty' : 'Unavailable';
  const rawDocsUrl = status.docs_url || 'https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/research/research-llm-wiki';
  // Guard against unsafe URL schemes (e.g. js: / data:) if docs_url ever
  // becomes config-driven. esc() HTML-escapes but doesn't validate URL scheme.
  const docsUrl = /^https?:\/\//i.test(rawDocsUrl) ? rawDocsUrl : '#';
  const toggleNote = status.toggle_available
    ? 'Toggle available from configured Hermes Agent setting.'
    : (status.toggle_reason || 'No stable LLM Wiki on/off config flag was detected, so this panel is read-only.');
  const statusNote = isReady
    ? 'LLM Wiki is configured and page metadata is visible without exposing wiki content.'
    : isEmpty
      ? 'LLM Wiki exists but has no entity, concept, comparison, or query pages yet.'
      : isError
        ? `Unable to inspect LLM Wiki status${status.error ? ': ' + status.error : ''}.`
        : 'No LLM Wiki directory was found. Set WIKI_PATH or skills.config.wiki.path to enable status visibility.';
  return `
    <div class="insights-card wiki-status-card" id="llmWikiStatusCard">
      <div class="wiki-status-head">
        <div>
          <div class="insights-card-title">LLM Wiki</div>
          <div class="wiki-status-sub">Knowledge-base observability</div>
        </div>
        <span class="wiki-status-badge ${badgeClass}">${esc(badgeText)}</span>
      </div>
      <div class="wiki-status-note">${esc(statusNote)}</div>
      <div class="wiki-status-grid">
        <div><span>Enabled</span><strong>${status.enabled ? 'Yes' : 'No'}</strong></div>
        <div><span>Entries</span><strong>${Number(status.entry_count || 0).toLocaleString()}</strong></div>
        <div><span>Pages</span><strong>${Number(status.page_count || 0).toLocaleString()}</strong></div>
        <div><span>raw/ files</span><strong>${Number(status.raw_source_count || 0).toLocaleString()}</strong></div>
        <div><span>Last updated</span><strong>${esc(_formatLlmWikiTimestamp(status.last_updated))}</strong></div>
        <div><span>Last writer</span><strong>${esc(status.last_writer || 'Not available')}</strong></div>
      </div>
      <div class="wiki-status-footer">
        <span>${esc(toggleNote)}</span>
        <div style="display:flex;align-items:center;gap:8px;">
          ${isReady || isEmpty ? `<button class="wiki-browse-btn" onclick="_openWikiBrowser()">${esc(t('wiki_browse'))}</button>` : ''}
          <a href="${esc(docsUrl)}" target="_blank" rel="noopener noreferrer">Docs</a>
        </div>
      </div>
    </div>`;
}

export async function _openWikiBrowser() {
  const existing = document.getElementById('wikiBrowserOverlay');
  if (existing) { existing.style.display = 'flex'; return; }

  const overlay = document.createElement('div');
  overlay.id = 'wikiBrowserOverlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';

  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.style.display = 'none'; });
  document.addEventListener('keydown', function escHandler(e) {
    if (e.key === 'Escape') { overlay.style.display = 'none'; document.removeEventListener('keydown', escHandler); }
  });

  const panel = document.createElement('div');
  panel.style.cssText = 'background:var(--bg);border:1px solid var(--border);border-radius:8px;width:min(720px,95vw);max-height:80vh;display:flex;flex-direction:column;overflow:hidden;';

  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--border);">
      <strong style="font-size:14px;">${esc(t('wiki_browse'))}</strong>
      <button onclick="document.getElementById('wikiBrowserOverlay').style.display='none'" style="background:none;border:none;cursor:pointer;font-size:18px;color:var(--muted);">&#x2715;</button>
    </div>
    <div style="padding:10px 16px;border-bottom:1px solid var(--border);">
      <input id="wikiBrowserSearch" type="text" placeholder="${esc(t('wiki_search_placeholder'))}" style="width:100%;padding:6px 10px;background:var(--input-bg,var(--bg));border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:13px;box-sizing:border-box;" />
    </div>
    <div id="wikiBrowserList" style="flex:1;overflow-y:auto;padding:8px 0;min-height:80px;"></div>
    <div id="wikiBrowserContent" style="display:none;flex:1;overflow-y:auto;padding:16px;border-top:1px solid var(--border);"></div>`;

  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  const listEl = document.getElementById('wikiBrowserList');
  const contentEl = document.getElementById('wikiBrowserContent');
  const searchEl = document.getElementById('wikiBrowserSearch');
  let _pages = [];

  function _renderWikiPageList(filter) {
    const q = (filter || '').toLowerCase();
    const visible = q ? _pages.filter(p => p.name.toLowerCase().includes(q)) : _pages;
    if (!visible.length) {
      listEl.innerHTML = `<div style="padding:12px 16px;color:var(--muted);font-size:13px;">${esc(t('wiki_no_pages'))}</div>`;
      return;
    }
    listEl.innerHTML = visible.map(p =>
      `<div class="wiki-browser-item" data-path="${esc(p.path)}" style="padding:7px 16px;cursor:pointer;font-size:13px;border-radius:4px;margin:0 6px;" onmouseover="this.style.background='var(--hover,rgba(255,255,255,0.07))'" onmouseout="this.style.background=''" onclick="window._wikiBrowserOpenPage(this.dataset.path)">${esc(p.name)}</div>`
    ).join('');
  }

  window._wikiBrowserOpenPage = async function(path) {
    contentEl.innerHTML = '<div style="padding:12px;color:var(--muted);font-size:13px;">Loading...</div>';
    contentEl.style.display = 'block';
    listEl.style.display = 'none';
    try {
      const data = await api('/api/wiki/page?path=' + encodeURIComponent(path));
      if (typeof renderMarkdownPreviewContent === 'function') {
        contentEl.innerHTML = '<button onclick="window._wikiBrowserBack()" style="margin-bottom:10px;background:none;border:1px solid var(--border);border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px;color:var(--text);">&#8592; Back</button><div id="wikiBrowserMd"></div>';
        renderMarkdownPreviewContent({content: data.content, el: document.getElementById('wikiBrowserMd')});
      } else {
        contentEl.innerHTML = '<button onclick="window._wikiBrowserBack()" style="margin-bottom:10px;background:none;border:1px solid var(--border);border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px;color:var(--text);">&#8592; Back</button><pre style="white-space:pre-wrap;word-break:break-word;font-size:12px;margin:0;">' + esc(data.content) + '</pre>';
      }
    } catch(e) {
      contentEl.innerHTML = '<button onclick="window._wikiBrowserBack()" style="margin-bottom:10px;background:none;border:1px solid var(--border);border-radius:4px;padding:3px 10px;cursor:pointer;font-size:12px;color:var(--text);">&#8592; Back</button><div style="color:var(--error,#f55);">' + esc(e.message || String(e)) + '</div>';
    }
  };

  window._wikiBrowserBack = function() {
    contentEl.style.display = 'none';
    listEl.style.display = '';
  };

  searchEl.addEventListener('input', () => _renderWikiPageList(searchEl.value));

  try {
    const data = await api('/api/wiki/browse');
    _pages = Array.isArray(data && data.pages) ? data.pages : [];
    if (!_pages.length) {
      listEl.innerHTML = `<div style="padding:12px 16px;color:var(--muted);font-size:13px;">${esc(t('wiki_no_pages'))}</div>`;
    } else {
      _renderWikiPageList('');
    }
  } catch(e) {
    listEl.innerHTML = `<div style="padding:12px 16px;color:var(--error,#f55);font-size:13px;">${esc(e.message || String(e))}</div>`;
  }
}

/**
 * Bucket daily token rows for chart display.
 * Returns rows unchanged when length <= 30 (per-day resolution).
 * For longer ranges, groups consecutive days into buckets:
 *   31–90 days → 2-day buckets
 *   91–180 days → 3-day buckets
 *   181–365 days → 8-day buckets
 * Result is always <= ~52 bars.
 * Each bucket row has:
 *   - label: short label for axis (e.g. MM-DD or MM-DD–MM-DD)
 *   - title: full tooltip title (e.g. 2026-01-01 – 2026-01-05)
 *   - date: first date in bucket (used for date label slicing)
 *   - input_tokens, output_tokens, sessions, cost: summed across bucket
 */
export function _bucketDailyTokensForChart(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return [];
  const len = rows.length;
  if (len <= 30) return rows;  // per-day resolution for 7/30-day ranges

  // Target <= 75 bars; derive bucket size
  let bucketSize;
  if (len <= 90) {
    bucketSize = 2;
  } else if (len <= 180) {
    bucketSize = 3;
  } else if (len <= 365) {
    bucketSize = 8;  // <=52 bars for 365 days (ceil(365/8)=46)
  } else {
    bucketSize = 8;  // fallback for >365 (shouldn't occur in practice)
  }

  const result = [];
  for (let i = 0; i < len; i += bucketSize) {
    const slice = rows.slice(i, i + bucketSize);
    const input_tokens = slice.reduce((s, r) => s + Number(r.input_tokens || 0), 0);
    const output_tokens = slice.reduce((s, r) => s + Number(r.output_tokens || 0), 0);
    const cache_read_tokens = slice.reduce((s, r) => s + Number(r.cache_read_tokens || 0), 0);
    const sessions = slice.reduce((s, r) => s + Number(r.sessions || 0), 0);
    const cost = slice.reduce((s, r) => s + Number(r.cost || 0), 0);

    const firstDate = slice[0].date;
    const lastDate = slice[slice.length - 1].date;

    // Label: short form for axis
    const firstLabel = String(firstDate).slice(5);  // MM-DD
    const lastLabel = String(lastDate).slice(5);
    const label = (firstDate === lastDate) ? firstLabel : (firstLabel + '–' + lastLabel);

    result.push({
      label,
      title: firstDate + (firstDate !== lastDate ? ' – ' + lastDate : ''),
      date: firstDate,
      input_tokens,
      output_tokens,
      cache_read_tokens,
      sessions,
      cost,
    });
  }
  return result;
}

export function _renderSkillUsage(d) {
  const usage = d.usage || {};
  const skillNames = d.skill_names || [];
  const totalInvocations = d.total_invocations || 0;
  const uniqueUsed = d.unique_skills_used || 0;
  const entries = Object.entries(usage)
    .map(([name, meta]) => [name, Number(meta && meta.use_count) || 0, Number(meta && meta.view_count) || 0, Number(meta && meta.patch_count) || 0])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10);
  if (!entries.length) {
    return `<div class="insights-card" id="skillUsageCard"><div class="insights-card-title">${esc(t('insights_skill_usage_title'))}</div><div class="insights-empty">${esc(t('insights_skill_usage_no_data'))}</div><div style="font-size:11px;color:var(--muted);margin-top:4px">${esc(t('insights_skill_usage_no_data_hint'))}</div></div>`;
  }
  const rows = entries.map(([name, useCount, viewCount, patchCount]) => {
    const share = totalInvocations > 0 ? (useCount / totalInvocations * 100).toFixed(1) : '0.0';
    return `<div class="insights-table-row"><span class="insights-model-name" title="${esc(name)}">${esc(name)}</span><span>${useCount}</span><span>${viewCount}</span><span>${patchCount}</span><span>${share}%</span></div>`;
  }).join('');
  return `<div class="insights-card" id="skillUsageCard"><div class="insights-card-title">${esc(t('insights_skill_usage_title'))}</div><div class="skill-usage-grid" style="margin-bottom:8px"><div><span>${esc(t('insights_skill_usage_total'))}</span><strong>${totalInvocations.toLocaleString()}</strong></div><div><span>${esc(t('insights_skill_usage_skills_used'))}</span><strong>${uniqueUsed}/${skillNames.length}</strong></div></div><div class="insights-table skill-usage-table"><div class="insights-table-head"><span>${esc(t('insights_skill_usage_col_skill'))}</span><span>${esc(t('insights_skill_usage_col_uses'))}</span><span>${esc(t('insights_skill_usage_col_views'))}</span><span>${esc(t('insights_skill_usage_col_patches'))}</span><span>${esc(t('insights_skill_usage_col_share'))}</span></div>${rows}</div><div class="wiki-status-footer" style="margin-top:8px">${esc(t('insights_skill_usage_footer'))}</div></div>`;
}

export function _renderInsights(d, box, wikiStatus, skillUsage) {
  const fmtNum = n => Number(n || 0).toLocaleString();
  const fmtCost = c => {
    const value = Number(c || 0);
    return value > 0 ? '$' + value.toFixed(value < 1 ? 4 : 2) : t('insights_no_cost');
  };
  const fmtTokens = n => {
    const value = Number(n || 0);
    return value >= 1e6 ? (value/1e6).toFixed(1) + 'M' : value >= 1e3 ? (value/1e3).toFixed(1) + 'K' : fmtNum(value);
  };

  // Overview cards
  const overviewCards = [
    { label: t('insights_sessions'), value: fmtNum(d.total_sessions), icon: li('message-square', 18) },
    { label: t('insights_messages'), value: fmtNum(d.total_messages), icon: li('hash', 18) },
    { label: t('insights_tokens'), value: fmtTokens(d.total_tokens), icon: li('cpu', 18) },
    { label: t('insights_cost'), value: fmtCost(d.total_cost), icon: li('dollar-sign', 18) },
  ];

  // Daily token trend — bucket long ranges to avoid horizontal overflow
  const dailyTokens = Array.isArray(d.daily_tokens) ? d.daily_tokens : [];
  const chartRows = _bucketDailyTokensForChart(dailyTokens);
  let dailyHtml = '';
  if (chartRows.length) {
    const maxDailyTokens = Math.max(...chartRows.map(r => Number(r.input_tokens || 0) + Number(r.output_tokens || 0)), 1);
    const labelEvery = Math.max(Math.ceil(chartRows.length / 7), 1);
    dailyHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_daily_tokens'))}</div><div class="insights-daily-token-chart">` +
      chartRows.map((r, idx) => {
        const input = Number(r.input_tokens || 0);
        const output = Number(r.output_tokens || 0);
        const inputPct = Math.max((input / maxDailyTokens) * 100, input ? 2 : 0).toFixed(1);
        const outputPct = Math.max((output / maxDailyTokens) * 100, output ? 2 : 0).toFixed(1);
        const showLabel = idx === 0 || idx === chartRows.length - 1 || idx % labelEvery === 0;
        const titleDate = r.title || r.date;
        const cacheRead = Number(r.cache_read_tokens || 0);
        // Bounded daily cache hit rate: reads / (input + reads), 0-100%.
        const cacheDenom = input + cacheRead;
        const cacheStr = (cacheRead > 0 && cacheDenom > 0)
          ? ` · ${Math.min(100, Math.round((cacheRead / cacheDenom) * 100))}% ${t('insights_model_cache')}`
          : '';
        const title = `${titleDate} · ${fmtTokens(input)} ${t('insights_input_tokens')} · ${fmtTokens(output)} ${t('insights_output_tokens')}${cacheStr} · ${fmtCost(r.cost)} · ${fmtNum(r.sessions)} ${t('insights_sessions')}`;
        const labelText = r.label !== undefined ? r.label : String(r.date).slice(5);
        return `<div class="insights-daily-bar" title="${esc(title)}"><div class="insights-daily-stack" aria-label="${esc(title)}"><div class="insights-daily-bar-output" style="height:${outputPct}%"></div><div class="insights-daily-bar-input" style="height:${inputPct}%"></div></div><span>${showLabel ? esc(labelText) : ''}</span></div>`;
      }).join('') +
      `</div><div class="insights-daily-legend"><span><i class="insights-daily-legend-input"></i>${esc(t('insights_input_tokens'))}</span><span><i class="insights-daily-legend-output"></i>${esc(t('insights_output_tokens'))}</span></div></div>`;
  } else {
    dailyHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_daily_tokens'))}</div><div class="insights-empty">${esc(t('insights_no_usage_data'))}</div></div>`;
  }

  // Models table
  let modelsHtml = '';
  if (d.models && d.models.length) {
    modelsHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_models'))}</div><div class="insights-table insights-model-table"><div class="insights-table-head"><span>${esc(t('insights_model_name'))}</span><span>${esc(t('insights_model_sessions'))}</span><span>${esc(t('insights_model_tokens'))}</span><span title="${esc(t('insights_cache_hit'))}">${esc(t('insights_model_cache'))}</span><span>${esc(t('insights_model_cost'))}</span><span>${esc(t('insights_model_share'))}</span></div>` +
      d.models.map(m => {
        const share = Number(m.cost_share || m.token_share || m.session_share || 0);
        const title = `${m.model} · ${fmtTokens(m.input_tokens)} ${t('insights_input_tokens')} · ${fmtTokens(m.output_tokens)} ${t('insights_output_tokens')}`;
        const cachePct = (m.cache_hit_percent === null || m.cache_hit_percent === undefined) ? null : Number(m.cache_hit_percent);
        const cacheCell = cachePct === null
          ? '<span class="insights-model-cache insights-model-cache-empty">—</span>'
          : `<span class="insights-model-cache" title="${esc(t('insights_cache_hit'))}: ${fmtTokens(m.cache_read_tokens || 0)}">${cachePct}%</span>`;
        return `<div class="insights-table-row"><span class="insights-model-name" title="${esc(m.model)}">${esc(m.model)}</span><span>${fmtNum(m.sessions)}</span><span class="insights-model-tokens" title="${esc(title)}">${fmtTokens(m.total_tokens || 0)}</span>${cacheCell}<span class="insights-model-cost">${fmtCost(m.cost)}</span><span>${share}%</span></div>`;
      }).join('') +
      `</div></div>`;
  } else {
    modelsHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_models'))}</div><div class="insights-empty">${esc(t('insights_no_usage_data'))}</div></div>`;
  }
  const modelHealthHtml = _renderStaticModelHealthTable();

  // Activity by day of week
  let dowHtml = '';
  if (d.activity_by_day) {
    const maxDow = Math.max(...d.activity_by_day.map(x => x.sessions), 1);
    dowHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_activity_by_day'))}</div><div class="insights-bars">` +
      d.activity_by_day.map(r => {
        const pct = (r.sessions / maxDow * 100).toFixed(0);
        return `<div class="insights-bar-row"><span class="insights-bar-label">${r.day}</span><div class="insights-bar-track"><div class="insights-bar-fill" style="width:${pct}%"></div></div><span class="insights-bar-value">${r.sessions}</span></div>`;
      }).join('') +
      `</div></div>`;
  }

  // Activity by hour
  let hodHtml = '';
  if (d.activity_by_hour) {
    const maxHod = Math.max(...d.activity_by_hour.map(x => x.sessions), 1);
    const peakHour = d.activity_by_hour.reduce((a, b) => b.sessions > a.sessions ? b : a, {hour:0,sessions:0});
    hodHtml = `<div class="insights-card"><div class="insights-card-title">${esc(t('insights_activity_by_hour'))} <span style="font-weight:400;font-size:11px;color:var(--muted)">${esc(t('insights_peak_hour').replace('{hour}', peakHour.hour + ':00'))}</span></div><div class="insights-bars">` +
      d.activity_by_hour.map(r => {
        const pct = (r.sessions / maxHod * 100).toFixed(0);
        const isPeak = r.hour === peakHour.hour && peakHour.sessions > 0;
        return `<div class="insights-bar-row"><span class="insights-bar-label">${String(r.hour).padStart(2,'0')}</span><div class="insights-bar-track"><div class="insights-bar-fill${isPeak ? ' insights-bar-peak' : ''}" style="width:${pct}%"></div></div><span class="insights-bar-value">${r.sessions}</span></div>`;
      }).join('') +
      `</div></div>`;
  }

  // Token breakdown
  const tokenCards = `
    <div class="insights-card">
      <div class="insights-card-title">${esc(t('insights_token_breakdown'))}</div>
      <div class="insights-token-row">
        <span class="insights-token-label">${esc(t('insights_input_tokens'))}</span>
        <span class="insights-token-value">${fmtTokens(d.total_input_tokens)}</span>
      </div>
      <div class="insights-token-row">
        <span class="insights-token-label">${esc(t('insights_output_tokens'))}</span>
        <span class="insights-token-value">${fmtTokens(d.total_output_tokens)}</span>
      </div>
      <div class="insights-token-row insights-token-total">
        <span class="insights-token-label">${esc(t('insights_total'))}</span>
        <span class="insights-token-value">${fmtTokens(d.total_tokens)}</span>
      </div>
    </div>`;

  box.innerHTML = `
    ${_renderSystemHealthPanel()}
    ${_renderLlmWikiStatus(wikiStatus)}
    ${_renderSkillUsage(skillUsage)}
    <div class="insights-grid">
      ${overviewCards.map(c => `<div class="insights-stat"><div class="insights-stat-icon">${c.icon}</div><div class="insights-stat-info"><div class="insights-stat-value">${c.value}</div><div class="insights-stat-label">${esc(c.label)}</div></div></div>`).join('')}
    </div>
    ${dailyHtml}
    ${modelHealthHtml}
    <div class="insights-row insights-usage-grid">
      ${tokenCards}
      ${modelsHtml}
    </div>
    ${dowHtml}
    ${hodHtml}
    <div style="text-align:center;color:var(--muted);font-size:10px;margin-top:12px;opacity:.6">${esc(t('insights_footer').replace('{days}', d.period_days))}</div>
  `;
}

export async function clearConversation() {
  if(!S.session) return;
  const _clrMsg=await showConfirmDialog({title:t('clear_conversation_title'),message:t('clear_conversation_message'),confirmLabel:t('clear'),danger:true,focusCancel:true});
  if(!_clrMsg) return;
  try {
    const data = await api('/api/session/clear', {method:'POST',
      body: JSON.stringify({session_id: S.session.session_id})});
    S.session = data.session;
    S.messages = [];
    S.toolCalls = [];
    syncTopbar();
    renderMessages();
    showToast(t('conversation_cleared'));
  } catch(e) { setStatus(t('clear_failed') + e.message); }
}
