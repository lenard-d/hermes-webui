// Panels domain: skills and memory
window.HermesPanels = window.HermesPanels || {};

// ── Skills panel ──
async function loadSkills() {
  if (_skillsData) { renderSkills(_skillsData); return; }
  const box = $('skillsList');
  try {
    const data = await api('/api/skills');
    _skillsData = data.skills || [];
    // Prune collapsed state to only keep categories present in fresh data,
    // avoiding stale keys when categories are renamed or removed server-side.
    const liveCats = new Set(_skillsData.map(s => s.category || '(general)'));
    for (const c of _collapsedCats) { if (!liveCats.has(c)) _collapsedCats.delete(c); }
    renderSkills(_skillsData);
  } catch(e) { box.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">Error: ${esc(e.message)}</div>`; }
}

let _collapsedCats = new Set(); // persisted collapsed state across re-renders

function _toggleCatCollapse(cat) {
  if (_collapsedCats.has(cat)) _collapsedCats.delete(cat);
  else _collapsedCats.add(cat);
  // Toggle DOM without full re-render
  document.querySelectorAll('.skills-category').forEach(sec => {
    const header = sec.querySelector('.skills-cat-header');
    if (header && header.dataset.cat === cat) {
      const collapsed = _collapsedCats.has(cat);
      sec.classList.toggle('collapsed', collapsed);
      header.querySelector('.cat-chevron').style.transform = collapsed ? '' : 'rotate(90deg)';
      sec.querySelectorAll('.skill-item').forEach(el => el.style.display = collapsed ? 'none' : '');
    }
  });
}

function renderSkills(skills) {
  const query = ($('skillsSearch').value || '').toLowerCase();
  const filtered = query ? skills.filter(s =>
    (s.name||'').toLowerCase().includes(query) ||
    (s.description||'').toLowerCase().includes(query) ||
    (s.category||'').toLowerCase().includes(query)
  ) : skills;
  // Group by category
  const cats = {};
  for (const s of filtered) {
    const cat = s.category || '(general)';
    if (!cats[cat]) cats[cat] = [];
    cats[cat].push(s);
  }
  const box = $('skillsList');
  box.innerHTML = '';
  if (!filtered.length) { box.innerHTML = `<div style="padding:12px;color:var(--muted);font-size:12px">${esc(t('skills_no_match'))}</div>`; return; }
  for (const [cat, items] of Object.entries(cats).sort()) {
    const collapsed = _collapsedCats.has(cat);
    const sec = document.createElement('div');
    sec.className = 'skills-category' + (collapsed ? ' collapsed' : '');
    const hdr = document.createElement('div');
    hdr.className = 'skills-cat-header';
    hdr.dataset.cat = cat;
    hdr.innerHTML = `<span class="cat-chevron" style="display:inline-flex;transition:transform .15s;${collapsed ? '' : 'transform:rotate(90deg)'}">${li('chevron-right',12)}</span> ${esc(cat)} <span style="opacity:.5">(${items.length})</span>`;
    hdr.onclick = () => _toggleCatCollapse(cat);
    sec.appendChild(hdr);
    for (const skill of items.sort((a,b) => a.name.localeCompare(b.name))) {
      const el = document.createElement('div');
      el.className = 'skill-item' + (skill.disabled ? ' disabled' : '');
      el.style.display = collapsed ? 'none' : '';
      const isDisabled = skill.disabled || false;
      const toggle = document.createElement('span');
      toggle.className = 'skill-toggle' + (isDisabled ? '' : ' enabled');
      toggle.title = isDisabled ? t('skill_disabled') : t('skill_enabled');
      toggle.addEventListener('click', (ev) => {
        ev.stopPropagation();
        toggleSkill(skill.name, !isDisabled);
      });
      const nameEl = document.createElement('span');
      nameEl.className = 'skill-name';
      nameEl.textContent = skill.name;
      const descEl = document.createElement('span');
      descEl.className = 'skill-desc';
      descEl.textContent = skill.description || '';
      el.append(toggle, nameEl, descEl);
      el.onclick = () => openSkill(skill.name, el);
      sec.appendChild(el);
    }
    box.appendChild(sec);
  }
}

function filterSkills() {
  if (_skillsData) renderSkills(_skillsData);
}


async function toggleSkill(name, currentlyEnabled) {
  const newEnabled = !currentlyEnabled;
  try {
    const result = await api('/api/skills/toggle', {
      method: 'POST',
      body: JSON.stringify({ name, enabled: newEnabled })
    });
    if (result && result.ok) {
      if (_skillsData) {
        const skill = _skillsData.find(s => s.name === name);
        if (skill) skill.disabled = !newEnabled;
      }
      if(typeof window!=='undefined'&&typeof window.invalidateSlashSkillCaches==='function') window.invalidateSlashSkillCaches();
      renderSkills(_skillsData || []);
    } else {
      setStatus((result && result.error) || t('skill_toggle_failed'));
    }
  } catch(e) {
    setStatus(t('skill_toggle_failed') + e.message);
  }
}

// Currently selected skill detail — kept across panel switches so re-entering
// the Skills view shows the last-viewed skill.
let _currentSkillDetail = null; // { name, category, content }
let _skillMode = 'empty'; // 'empty' | 'read' | 'create' | 'edit'
let _skillPreFormDetail = null; // snapshot of previously-viewed skill when entering a form
let _editingSkillName = null;

function _stripYamlFrontmatter(content) {
  if (!content) return { frontmatter: null, body: '' };
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(content);
  if (!m) return { frontmatter: null, body: content };
  return { frontmatter: m[1], body: content.slice(m[0].length) };
}

function _skillMarkdownHtml(markdown) {
  return `<div class="preview-md">${renderMd(markdown || '')}</div>`;
}

function _enhanceSkillMarkdown(root) {
  if (!root) return;
  requestAnimationFrame(() => {
    const mdRoot = root.querySelector('.preview-md') || root;
    if (typeof highlightCode === 'function') highlightCode(mdRoot);
    if (typeof renderKatexBlocks === 'function') renderKatexBlocks(mdRoot);
  });
}

function _renderSkillDetail(name, content, linkedFiles) {
  const title = $('skillDetailTitle');
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  const editBtn = $('btnEditSkillDetail');
  const delBtn = $('btnDeleteSkillDetail');
  if (title) title.textContent = name;
  const { frontmatter, body: markdownBody } = _stripYamlFrontmatter(content);
  let html = '';
  if (frontmatter) {
    html += `<details class="skill-frontmatter"><summary>${esc(t('skill_metadata'))}</summary><pre><code>${esc(frontmatter)}</code></pre></details>`;
  }
  html += _skillMarkdownHtml(markdownBody || '(no content)');
  const lf = linkedFiles || {};
  const categories = Object.entries(lf).filter(([,files]) => files && files.length > 0);
  if (categories.length) {
    html += `<div class="skill-linked-files"><div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px">${esc(t('linked_files'))}</div>`;
    for (const [cat, files] of categories) {
      html += `<div class="skill-linked-section"><h4>${esc(cat)}</h4>`;
      for (const f of files) {
        html += `<a class="skill-linked-file" href="#" data-skill-name="${esc(name)}" data-skill-file="${esc(f)}">${esc(f)}</a>`;
      }
      html += '</div>';
    }
    html += '</div>';
  }
  body.innerHTML = `<div class="main-view-content skill-detail-content">${html}</div>`;
  _enhanceSkillMarkdown(body);
  body.querySelectorAll('.skill-linked-file').forEach(a => {
    a.addEventListener('click', e => { e.preventDefault(); openSkillFile(a.dataset.skillName, a.dataset.skillFile); });
  });
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _skillMode = 'read';
  _setSkillHeaderButtons('read');
}

function _renderSkillError(name, message) {
  const title = $('skillDetailTitle');
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  if (title) title.textContent = name;
  if (body) {
    body.innerHTML = `<div class="main-view-content"><div class="detail-form-error" style="display:block">${esc(message || t('skill_load_failed'))}</div></div>`;
    body.style.display = '';
  }
  if (empty) empty.style.display = 'none';
  _currentSkillDetail = null;
  _skillMode = 'empty';
  _setSkillHeaderButtons('empty');
}

function _setSkillHeaderButtons(mode) {

  const header = $('mainSkills') && $('mainSkills').querySelector('.main-view-header');  const editBtn = $('btnEditSkillDetail');
  const delBtn = $('btnDeleteSkillDetail');
  const cancelBtn = $('btnCancelSkillDetail');
  const saveBtn = $('btnSaveSkillDetail');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  if (mode === 'read') { if (header) header.style.display = 'flex';  show(editBtn); show(delBtn); hide(cancelBtn); hide(saveBtn); }
  else if (mode === 'create' || mode === 'edit') { if (header) header.style.display = 'flex'; hide(editBtn); hide(delBtn); show(cancelBtn); show(saveBtn); }
  else { if (header) header.style.display = 'none';  hide(editBtn); hide(delBtn); hide(cancelBtn); hide(saveBtn); }
}

async function openSkill(name, el) {
  // Highlight active skill in the sidebar list
  document.querySelectorAll('.skill-item').forEach(e => e.classList.remove('active'));
  if (el) el.classList.add('active');
  _skillPreFormDetail = null;
  _editingSkillName = null;
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(name)}`);
    if (data && (data.success === false || data.error)) {
      const message = data.error || t('skill_load_failed');
      _renderSkillError(name, message);
      setStatus(t('skill_load_failed') + message);
      return;
    }
    _currentSkillDetail = { name, content: data.content || '', linked_files: data.linked_files || {} };
    _renderSkillDetail(name, data.content || '', data.linked_files || {});
    _closeMobileSidebarAfterPanelSelection();
  } catch(e) { setStatus(t('skill_load_failed') + e.message); }
}

async function openSkillFile(skillName, filePath) {
  try {
    const data = await api(`/api/skills/content?name=${encodeURIComponent(skillName)}&file=${encodeURIComponent(filePath)}`);
    if (data && data.error) {
      _renderSkillError(skillName, data.error);
      setStatus(t('skill_file_load_failed') + data.error);
      return;
    }
    const body = $('skillDetailBody');
    if (!body) return;
    const ext = (filePath.split('.').pop() || '').toLowerCase();
    const isMd = ['md','markdown'].includes(ext);
    const backLabel = t('skills_back_to').replace('{0}', skillName);
    const header = `<div class="skill-file-breadcrumb"><a href="#" class="skill-file-back" data-skill-name="${esc(skillName)}">&larr; ${esc(backLabel)}</a><span class="skill-file-path">${esc(filePath)}</span></div>`;
    let content;
    if (isMd) {
      content = `<div class="main-view-content">${_skillMarkdownHtml(data.content || '')}</div>`;
    } else {
      const escaped = esc(data.content || '');
      content = `<pre class="skill-file-code"><code>${escaped}</code></pre>`;
    }
    body.innerHTML = header + content;
    body.style.display = '';
    const empty = $('skillDetailEmpty');
    if (empty) empty.style.display = 'none';
    body.querySelectorAll('.skill-file-back').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        if (_currentSkillDetail && _currentSkillDetail.name === a.dataset.skillName) {
          _renderSkillDetail(_currentSkillDetail.name, _currentSkillDetail.content, _currentSkillDetail.linked_files);
        } else {
          openSkill(a.dataset.skillName, null);
        }
      });
    });
    if (isMd) _enhanceSkillMarkdown(body);
    else requestAnimationFrame(() => { if (typeof highlightCode === 'function') highlightCode(); });
  } catch(e) { setStatus(t('skill_file_load_failed') + e.message); }
}

function editCurrentSkill() {
  if (!_currentSkillDetail) return;
  const s = _currentSkillDetail;
  let category = '';
  if (_skillsData) {
    const match = _skillsData.find(x => x.name === s.name);
    if (match) category = match.category || '';
  }
  _skillPreFormDetail = { name: s.name, content: s.content, linked_files: s.linked_files };
  _editingSkillName = s.name;
  _skillMode = 'edit';
  _renderSkillForm({ name: s.name, category, content: s.content || '', isEdit: true });
}

function openSkillCreate() {
  if (typeof switchPanel === 'function' && _currentPanel !== 'skills') switchPanel('skills');
  _skillPreFormDetail = _currentSkillDetail ? { ..._currentSkillDetail } : null;
  _editingSkillName = null;
  _skillMode = 'create';
  _renderSkillForm({ name: '', category: '', content: '', isEdit: false });
}

function _renderSkillForm({ name, category, content, isEdit }) {
  const title = $('skillDetailTitle');
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  if (!body || !title) return;
  title.textContent = isEdit ? t('skills_edit') + ' · ' + name : t('new_skill');
  const nameDisabled = isEdit ? 'disabled' : '';
  const nameHint = isEdit ? `<div class="detail-form-hint">${esc(t('skill_rename_not_supported') || 'Renaming a skill is not supported. Create a new skill and delete the old one to rename.')}</div>` : '';
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); saveSkillForm();">
        <div class="detail-form-row">
          <label for="skillFormName">${esc(t('skill_name') || 'Name')}</label>
          <input type="text" id="skillFormName" value="${esc(name || '')}" placeholder="my-skill" autocomplete="off" ${nameDisabled} required>
          ${nameHint}
        </div>
        <div class="detail-form-row">
          <label for="skillFormCategory">${esc(t('skill_category') || 'Category')}</label>
          <input type="text" id="skillFormCategory" value="${esc(category || '')}" placeholder="${esc(t('skill_category_placeholder') || 'Optional, e.g. devops')}" autocomplete="off">
        </div>
        <div class="detail-form-row">
          <label for="skillFormContent">${esc(t('skill_content') || 'SKILL.md content')}</label>
          <textarea id="skillFormContent" rows="18" placeholder="${esc(t('skill_content_placeholder') || 'YAML frontmatter + markdown body')}">${esc(content || '')}</textarea>
        </div>
        <div id="skillFormError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _setSkillHeaderButtons(isEdit ? 'edit' : 'create');
  const focusEl = isEdit ? $('skillFormCategory') : $('skillFormName');
  if (focusEl) focusEl.focus();
}

function cancelSkillForm() {
  _editingSkillName = null;
  if (_skillPreFormDetail) {
    const snap = _skillPreFormDetail;
    _skillPreFormDetail = null;
    _currentSkillDetail = snap;
    _renderSkillDetail(snap.name, snap.content || '', snap.linked_files || {});
    return;
  }
  // Revert to empty state
  _skillPreFormDetail = null;
  _currentSkillDetail = null;
  _skillMode = 'empty';
  const body = $('skillDetailBody');
  const empty = $('skillDetailEmpty');
  const title = $('skillDetailTitle');
  if (body) { body.innerHTML = ''; body.style.display = 'none'; }
  if (empty) empty.style.display = '';
  if (title) title.textContent = '';
  _setSkillHeaderButtons('empty');
}

async function saveSkillForm() {
  const nameInput = $('skillFormName');
  const catInput = $('skillFormCategory');
  const contentInput = $('skillFormContent');
  const errEl = $('skillFormError');
  if (!nameInput || !contentInput || !errEl) return;
  const name = (nameInput.value || '').trim().toLowerCase().replace(/\s+/g, '-');
  const category = (catInput ? (catInput.value || '').trim() : '');
  const content = contentInput.value;
  errEl.style.display = 'none';
  if (!name) { errEl.textContent = t('skill_name_required'); errEl.style.display = ''; return; }
  if (!content.trim()) { errEl.textContent = t('content_required'); errEl.style.display = ''; return; }
  try {
    await api('/api/skills/save', {method:'POST', body: JSON.stringify({name, category: category||undefined, content})});
    showToast(_editingSkillName ? t('skill_updated') : t('skill_created'));
    _skillsData = null;
    _cronSkillsCache = null;
    if(typeof window!=='undefined'&&typeof window.invalidateSlashSkillCaches==='function') window.invalidateSlashSkillCaches();
    _editingSkillName = null;
    _skillPreFormDetail = null;
    await loadSkills();
    // Reload the saved skill in read mode with fresh content
    const row = document.querySelector(`.skill-item .skill-name`);
    const match = document.querySelectorAll('.skill-item');
    let targetEl = null;
    match.forEach(el => {
      const nm = el.querySelector('.skill-name');
      if (nm && nm.textContent === name) targetEl = el;
    });
    await openSkill(name, targetEl);
  } catch(e) { errEl.textContent = t('error_prefix') + e.message; errEl.style.display = ''; }
}

// Back-compat aliases (delete flow + any old callers)
const submitSkillSave = saveSkillForm;
function toggleSkillForm(){ openSkillCreate(); }

async function deleteCurrentSkill() {
  if (!_currentSkillDetail) return;
  const name = _currentSkillDetail.name;
  const message = t('skill_delete_confirm')
    ? t('skill_delete_confirm').replace('{0}', name)
    : `Delete skill "${name}"?`;
  const ok = await showConfirmDialog({
    title: t('delete_title') || 'Delete',
    message,
    confirmLabel: t('delete_title') || 'Delete',
    danger: true,
    focusCancel: true,
  });
  if (!ok) return;
  try {
    await api('/api/skills/delete', { method:'POST', body: JSON.stringify({ name }) });
    _currentSkillDetail = null;
    _skillPreFormDetail = null;
    _skillsData = null;
    _cronSkillsCache = null;
    if(typeof window!=='undefined'&&typeof window.invalidateSlashSkillCaches==='function') window.invalidateSlashSkillCaches();
    _skillMode = 'empty';
    const body = $('skillDetailBody');
    const empty = $('skillDetailEmpty');
    const title = $('skillDetailTitle');
    if (body) { body.innerHTML = ''; body.style.display = 'none'; }
    if (empty) empty.style.display = '';
    if (title) title.textContent = '';
    _setSkillHeaderButtons('empty');
    await loadSkills();
    showToast(t('skill_deleted') || 'Skill deleted');
  } catch(e) { setStatus(t('error_prefix') + e.message); }
}
// ── Memory (main view) ──
let _memoryData = null;
let _notesSourcesData = null;
let _notesSearchResults = [];
let _notesSelectedSource = 'joplin';
let _notesPreviewNote = null;
let _notesSearchError = '';
let _notesSearchLoading = false;
let _currentMemorySection = null; // 'memory' | 'user' | 'soul' | 'project_context' | 'external_notes'
let _memoryMode = 'empty'; // 'empty' | 'read' | 'edit'

const MEMORY_SECTIONS = [
  { key: 'memory', labelKey: 'my_notes', emptyKey: 'no_notes_yet', iconKey: 'brain' },
  { key: 'user',   labelKey: 'user_profile', emptyKey: 'no_profile_yet', iconKey: 'user' },
  { key: 'soul',   labelKey: 'agent_soul', emptyKey: 'no_soul_yet', iconKey: 'sparkles' },
  { key: 'project_context', label: 'Project Context', empty: 'No project context file found for this workspace.', iconKey: 'file-text', readOnly: true },
  { key: 'external_notes', labelKey: 'external_notes_sources', emptyKey: 'external_notes_empty', iconKey: 'book-open' },
];

function _memorySectionMeta(key) {
  return MEMORY_SECTIONS.find(s => s.key === key) || MEMORY_SECTIONS[0];
}

function _memorySectionLabel(meta) {
  if (meta.label) return meta.label;
  return t(meta.labelKey);
}

function _memorySectionEmpty(meta) {
  if (meta.empty) return meta.empty;
  return t(meta.emptyKey);
}

function _memorySectionContent(key) {
  if (!_memoryData) return '';
  if (key === 'user') return _memoryData.user || '';
  if (key === 'soul') return _memoryData.soul || '';
  if (key === 'project_context') return _memoryData.project_context || '';
  return _memoryData.memory || '';
}

function _memorySectionMtime(key) {
  if (!_memoryData) return 0;
  if (key === 'user') return _memoryData.user_mtime || 0;
  if (key === 'soul') return _memoryData.soul_mtime || 0;
  if (key === 'project_context') return _memoryData.project_context_mtime || 0;
  return _memoryData.memory_mtime || 0;
}

function _memorySectionPath(key) {
  if (!_memoryData) return '';
  if (key === 'user') return _memoryData.user_path || '';
  if (key === 'soul') return _memoryData.soul_path || '';
  if (key === 'project_context') return _memoryData.project_context_path || '';
  if (key === 'memory') return _memoryData.memory_path || '';
  return '';
}

function _setMemoryHeaderButtons(mode) {
  const header = $('mainMemory') && $('mainMemory').querySelector('.main-view-header');
  const show = b => b && (b.style.display = '');
  const hide = b => b && (b.style.display = 'none');
  const editBtn = $('btnEditMemoryDetail');
  const cancelBtn = $('btnCancelMemoryDetail');
  const saveBtn = $('btnSaveMemoryDetail');
  const meta = _memorySectionMeta(_currentMemorySection);
  if (mode === 'read') {
    // Any read view has a populated title → header must be visible. Only the
    // Edit affordance is gated on the section being editable (read-only
    // sections like Project Context / External Notes still show the header).
    if (header) header.style.display = 'flex';
    if (_currentMemorySection !== 'external_notes' && !meta.readOnly) show(editBtn); else hide(editBtn);
    hide(cancelBtn); hide(saveBtn);
  }
  else if (mode === 'edit') { if (header) header.style.display = 'flex'; hide(editBtn); show(cancelBtn); show(saveBtn); }
  else { if (header) header.style.display = 'none'; hide(editBtn); hide(cancelBtn); hide(saveBtn); }
}

function _renderExternalNotesSources() {
  const title = $('memoryDetailTitle');
  const body = $('memoryDetailBody');
  const empty = $('memoryDetailEmpty');
  if (!title || !body) return;
  title.textContent = t('external_notes_sources');
  const data = _notesSourcesData || {};
  const sources = Array.isArray(data.sources) ? data.sources : [];
  const recall = data.automatic_recall_unchanged !== false
    ? `<div class="memory-detail-mtime">${esc(t('external_notes_auto_recall_hint'))}</div>`
    : '';
  if (!sources.length) {
    body.innerHTML = `<div class="main-view-content">${recall}<div class="memory-empty">${esc(t('external_notes_empty'))}</div></div>`;
  } else {
    const selected = sources.find(src => (src.name || '').toLowerCase() === (_notesSelectedSource || '').toLowerCase()) || sources[0];
    _notesSelectedSource = (selected && selected.name) || 'joplin';
    const sourceOptions = sources.map(src => `<option value="${esc(src.name||'')}" ${src.name===_notesSelectedSource?'selected':''}>${esc(src.label||src.name||'')}</option>`).join('');
    const recentAiNotes = Array.isArray(data.recent_ai_notes) ? data.recent_ai_notes : [];
    const recentAiHtml = recentAiNotes.length
      ? `<section class="notes-source-card notes-ai-recent-card">
          <div class="notes-source-card-head notes-ai-recent-head"><strong>${li('bot', 14)}${esc(t('external_notes_recent_ai'))}</strong><span class="detail-badge">${esc(t('external_notes_auto'))}</span></div>
          <div class="notes-ai-recent-list">${recentAiNotes.map(note => {
            const updated = note.updated_time ? new Date(Number(note.updated_time)).toLocaleString() : '';
            return `<button type="button" class="notes-result-card notes-ai-recent-item" onclick="previewExternalNote('${esc(note.source||'joplin')}','${esc(note.id||'')}')"><strong>${esc(note.title||note.label||'Untitled')}</strong><span>${li('clock', 14)}${esc(note.label||t('external_notes_recent_ai_reason'))}${updated ? ` · ${esc(updated)}` : ''}</span></button>`;
          }).join('')}</div>
        </section>`
      : '';
    const searchError = _notesSearchError ? `<div class="detail-form-error">${esc(_notesSearchError)}</div>` : '';
    const resultHtml = _notesSearchResults.length
      ? `<div class="notes-search-results">${_notesSearchResults.map(note => `<button type="button" class="notes-result-card" onclick="previewExternalNote('${esc(note.source||_notesSelectedSource)}','${esc(note.id||'')}')"><strong>${esc(note.title||'Untitled')}</strong>${note.snippet?`<span>${esc(note.snippet)}</span>`:''}</button>`).join('')}</div>`
      : `<div class="memory-empty">${esc(t('external_notes_search_empty'))}</div>`;
    const previewHtml = _notesPreviewNote
      ? `<section class="notes-source-card notes-preview-card"><div class="notes-source-card-head"><strong>${esc(_notesPreviewNote.title||'Untitled')}</strong><span class="detail-badge">${esc(_notesPreviewNote.source||_notesSelectedSource)}</span></div><div class="memory-content preview-md">${renderMd(_notesPreviewNote.body||'')}</div></section>`
      : '';
    const cards = sources.map(src => {
      const status = src.active ? t('source_active') : (src.status || t('source_configured'));
      const tools = Array.isArray(src.tools) ? src.tools : [];
      const hintHtml = src.tool_source === 'configured_hint'
        ? `<div class="memory-detail-mtime">${esc(t('external_notes_configured_hint'))}</div>`
        : '';
      const toolHtml = tools.length
        ? `<ul class="notes-source-tools">${tools.map(tool => `<li><strong>${esc(tool.name||'')}</strong>${tool.description?` — ${esc(tool.description)}`:''}</li>`).join('')}</ul>`
        : `<div class="memory-empty">${esc(t('external_notes_no_tools'))}</div>`;
      return `<section class="notes-source-card">
        <div class="notes-source-card-head"><strong>${esc(src.label||src.name||'')}</strong><span class="detail-badge ${src.active?'active':''}">${esc(status)}</span></div>
        <div class="memory-detail-mtime">${esc(t('external_notes_tool_count', src.tool_count||0))}</div>
        ${hintHtml}
        ${toolHtml}
      </section>`;
    }).join('');
    const searchUi = `<section class="notes-source-card notes-search-card">
      <form class="notes-search-form" onsubmit="event.preventDefault(); searchExternalNotes();">
        <select id="externalNotesSource" onchange="selectExternalNotesSource(this.value)">${sourceOptions}</select>
        <input id="externalNotesQuery" type="search" placeholder="${esc(t('external_notes_search_placeholder'))}" />
        <button type="submit" class="btn-secondary">${esc(_notesSearchLoading ? t('loading') : t('search'))}</button>
      </form>
      ${searchError}
      ${resultHtml}
    </section>`;
    body.innerHTML = `<div class="main-view-content">${recall}${recentAiHtml}${searchUi}${previewHtml}${cards}</div>`;
  }
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _memoryMode = 'read';
  _setMemoryHeaderButtons('read');
}

function _renderMemoryDetail(section) {
  if (section === 'external_notes') {
    _renderExternalNotesSources();
    return;
  }

  const meta = _memorySectionMeta(section);
  const title = $('memoryDetailTitle');
  const body = $('memoryDetailBody');
  const empty = $('memoryDetailEmpty');
  if (!title || !body) return;
  title.textContent = _memorySectionLabel(meta);
  const content = _memorySectionContent(section);
  const mtime = _memorySectionMtime(section);
  const mtimeStr = mtime ? new Date(mtime * 1000).toLocaleString() : '';
  const mtimeHtml = mtimeStr ? `<div class="memory-detail-mtime">${esc(mtimeStr)}</div>` : '';
  const path = _memorySectionPath(section);
  const fileName = section === 'project_context' && _memoryData
    ? (_memoryData.project_context_name || (path.split(/[\\/]/).pop() || ''))
    : (path.split(/[\\/]/).pop() || '');
  const pathHtml = path ? `<div class="memory-detail-mtime">${esc(fileName)} · ${esc(path)}</div>` : '';
  const shadowed = section === 'project_context' && _memoryData && Array.isArray(_memoryData.project_context_shadowed)
    ? _memoryData.project_context_shadowed
    : [];
  const shadowedHtml = shadowed.length
    ? `<div class="memory-detail-mtime">${esc(shadowed.map(item => `${item.name || 'Context file'} present, shadowed by ${item.shadowed_by || fileName || 'active context'}`).join('; '))}</div>`
    : '';
  const inner = content
    ? `<div class="memory-content preview-md">${renderMd(content)}</div>`
    : `<div class="memory-empty">${esc(_memorySectionEmpty(meta))}</div>`;
  body.innerHTML = `<div class="main-view-content">${pathHtml}${mtimeHtml}${shadowedHtml}${inner}</div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _memoryMode = 'read';
  _setMemoryHeaderButtons('read');
}

function _renderMemoryEdit(section) {
  const meta = _memorySectionMeta(section);
  const title = $('memoryDetailTitle');
  const body = $('memoryDetailBody');
  const empty = $('memoryDetailEmpty');
  if (!title || !body) return;
  title.textContent = _memorySectionLabel(meta);
  const content = _memorySectionContent(section);
  body.innerHTML = `
    <div class="main-view-content">
      <form class="detail-form" onsubmit="event.preventDefault(); submitMemorySave();">
        <div class="detail-form-row">
          <label for="memEditContent">${esc(t('memory_notes_label'))}</label>
          <textarea id="memEditContent" rows="20" spellcheck="false">${esc(content)}</textarea>
        </div>
        <div id="memEditError" class="detail-form-error" style="display:none"></div>
      </form>
    </div>`;
  body.style.display = '';
  if (empty) empty.style.display = 'none';
  _memoryMode = 'edit';
  _setMemoryHeaderButtons('edit');
  const ta = $('memEditContent');
  if (ta) ta.focus();
}

async function loadNotesSources(force) {
  if (_notesSourcesData && !force) return _notesSourcesData;
  try {
    _notesSourcesData = await api('/api/notes/sources');
  } catch (e) {
    _notesSourcesData = {sources: [], automatic_recall_unchanged: true, error: e && e.message ? e.message : String(e)};
  }
  return _notesSourcesData;
}

function selectExternalNotesSource(source) {
  _notesSelectedSource = source || 'joplin';
  _notesSearchResults = [];
  _notesPreviewNote = null;
  _notesSearchError = '';
  _renderExternalNotesSources();
}

async function searchExternalNotes() {
  const input = $('externalNotesQuery');
  const sourceEl = $('externalNotesSource');
  const q = input ? input.value.trim() : '';
  _notesSelectedSource = sourceEl ? sourceEl.value : (_notesSelectedSource || 'joplin');
  _notesPreviewNote = null;
  _notesSearchError = '';
  if (!q) {
    _notesSearchResults = [];
    _renderExternalNotesSources();
    return;
  }
  _notesSearchLoading = true;
  _renderExternalNotesSources();
  try {
    const data = await api(`/api/notes/search?source=${encodeURIComponent(_notesSelectedSource)}&q=${encodeURIComponent(q)}&limit=20`);
    _notesSearchResults = Array.isArray(data.results) ? data.results : [];
    _notesSearchError = data.error || '';
  } catch (e) {
    _notesSearchResults = [];
    _notesSearchError = e && e.message ? e.message : String(e);
  } finally {
    _notesSearchLoading = false;
    _renderExternalNotesSources();
    const nextInput = $('externalNotesQuery');
    if (nextInput) nextInput.value = q;
  }
}

async function previewExternalNote(source, id) {
  _notesSearchError = '';
  try {
    const data = await api(`/api/notes/item?source=${encodeURIComponent(source||_notesSelectedSource)}&id=${encodeURIComponent(id||'')}`);
    _notesPreviewNote = data && data.note ? data.note : null;
  } catch (e) {
    _notesPreviewNote = null;
    _notesSearchError = e && e.message ? e.message : String(e);
  }
  _renderExternalNotesSources();
}

async function openMemorySection(section, el) {
  if (section === 'external_notes' && _memoryData && !_memoryData.external_notes_enabled) return;
  _currentMemorySection = section;
  document.querySelectorAll('#memoryPanel .side-menu-item').forEach(e => e.classList.remove('active'));
  if (el) el.classList.add('active');
  if (section === 'external_notes') {
    await loadNotesSources(false);
  }
  _renderMemoryDetail(section);
  _closeMobileSidebarAfterPanelSelection();
}

function editCurrentMemory() {
  const meta = _memorySectionMeta(_currentMemorySection);
  if (!_currentMemorySection || _currentMemorySection === 'external_notes' || meta.readOnly) return;
  _renderMemoryEdit(_currentMemorySection);
}

function cancelMemoryEdit() {
  if (!_currentMemorySection) return;
  _renderMemoryDetail(_currentMemorySection);
}

// Legacy alias (kept for any stale references)
function toggleMemoryEdit() { editCurrentMemory(); }
function closeMemoryEdit() { cancelMemoryEdit(); }

async function submitMemorySave() {
  if (!_currentMemorySection) return;
  if (_memorySectionMeta(_currentMemorySection).readOnly) return;
  const ta = $('memEditContent');
  const errEl = $('memEditError');
  if (!ta) return;
  if (errEl) errEl.style.display = 'none';
  try {
    await api('/api/memory/write', {method:'POST', body: JSON.stringify({section: _currentMemorySection, content: ta.value})});
    showToast(t('memory_saved'));
    await loadMemory(true);
    _renderMemoryDetail(_currentMemorySection);
  } catch(e) {
    if (errEl) { errEl.textContent = t('error_prefix') + e.message; errEl.style.display = ''; }
  }
}

async function loadMemory(force) {
  const panel = $('memoryPanel');
  try {
    const memoryUrl = S.session && S.session.session_id
      ? `/api/memory?session_id=${encodeURIComponent(S.session.session_id)}`
      : '/api/memory';
    const data = await api(memoryUrl);
    _memoryData = data;
    if (_currentMemorySection === 'external_notes' && !data.external_notes_enabled) {
      _currentMemorySection = null;
    }
    if (_currentMemorySection === 'external_notes') {
      await loadNotesSources(!!force);
    }
    if (panel) {
      panel.innerHTML = '';
      for (const s of MEMORY_SECTIONS) {
        if (s.key === 'external_notes' && !_memoryData.external_notes_enabled) continue;
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'side-menu-item';
        if (_currentMemorySection === s.key) el.classList.add('active');
        el.innerHTML = `${li(s.iconKey,16)}<span>${esc(_memorySectionLabel(s))}</span>`;
        const sectionPath = _memorySectionPath(s.key);
        if (sectionPath) el.title = sectionPath;
        el.onclick = () => openMemorySection(s.key, el);
        panel.appendChild(el);
      }
    }
    if (_currentMemorySection && _memoryMode !== 'edit') {
      _renderMemoryDetail(_currentMemorySection);
    }
  } catch(e) {
    if (panel) panel.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">${esc(t('error_prefix'))}${esc(e.message)}</div>`;
  }
}

window.HermesPanels.knowledge = {
  loadSkills,
  _toggleCatCollapse,
  renderSkills,
  filterSkills,
  toggleSkill,
  _stripYamlFrontmatter,
  _skillMarkdownHtml,
  _enhanceSkillMarkdown,
  _renderSkillDetail,
  _renderSkillError,
  _setSkillHeaderButtons,
  openSkill,
  openSkillFile,
  editCurrentSkill,
  openSkillCreate,
  _renderSkillForm,
  cancelSkillForm,
  saveSkillForm,
  toggleSkillForm,
  deleteCurrentSkill,
  _memorySectionMeta,
  _memorySectionLabel,
  _memorySectionEmpty,
  _memorySectionContent,
  _memorySectionMtime,
  _memorySectionPath,
  _setMemoryHeaderButtons,
  _renderExternalNotesSources,
  _renderMemoryDetail,
  _renderMemoryEdit,
  loadNotesSources,
  selectExternalNotesSource,
  searchExternalNotes,
  previewExternalNote,
  openMemorySection,
  editCurrentMemory,
  cancelMemoryEdit,
  toggleMemoryEdit,
  closeMemoryEdit,
  submitMemorySave,
  loadMemory,
};
