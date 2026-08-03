import { _IMAGE_EXTS, _SVG_EXTS, _mediaKindForName } from './media-and-quota.js';
import { $, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, S, esc } from './state.js';

function renderTray(){
  const tray=$('attachTray');tray.innerHTML='';
  if(!S.pendingFiles.length){
    tray.classList.remove('has-files');
    if(typeof updateSendBtn==='function')updateSendBtn();
    return;
  }
  tray.classList.add('has-files');
  if(typeof updateSendBtn==='function')updateSendBtn();
  S.pendingFiles.forEach((f,i)=>{
    const chip=document.createElement('div');chip.className='attach-chip';
    const mediaKind=_mediaKindForName(f.name);
    if(_IMAGE_EXTS.test(f.name)||_SVG_EXTS.test(f.name)||mediaKind==='audio'||mediaKind==='video'){
      const blobUrl=URL.createObjectURL(f);
      chip.className='attach-chip attach-chip--media attach-chip--'+mediaKind;
      chip.dataset.blobUrl=blobUrl;
      if(mediaKind==='image'){
        chip.innerHTML=`<img class="attach-thumb" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(_SVG_EXTS.test(f.name)){
        chip.innerHTML=`<img class="attach-thumb attach-thumb--svg" src="${esc(blobUrl)}" alt="${esc(f.name)}" title="${esc(f.name)}"><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(mediaKind==='audio'){
        chip.innerHTML=`<span class="attach-chip-media">🎵 ${esc(f.name)}</span><audio controls preload="metadata" src="${esc(blobUrl)}"></audio><button title="${t('remove_title')}">${li('x',12)}</button>`;
      } else if(mediaKind==='video'){
        chip.innerHTML=`<span class="attach-chip-media">🎬 ${esc(f.name)}</span><video controls preload="metadata" src="${esc(blobUrl)}"></video><button title="${t('remove_title')}">${li('x',12)}</button>`;
      }
    } else {
      chip.innerHTML=`${li('paperclip',12)} ${esc(f.name)} <button title="${t('remove_title')}">${li('x',12)}</button>`;
    }
    chip.querySelector('button').onclick=()=>{
      if(chip.dataset.blobUrl) URL.revokeObjectURL(chip.dataset.blobUrl);
      S.pendingFiles.splice(i,1);renderTray();
    };
    tray.appendChild(chip);
  });
}

function _uploadTooLargeMessage(file){
  const fileSizeMb=Math.ceil(((file&&file.size)||0)/1024/1024);
  return t('upload_too_large',MAX_UPLOAD_MB,fileSizeMb);
}

function _showUploadTooLarge(file){
  const message=`${t('upload_failed')}${file&&file.name?file.name:'file'} \u2014 ${_uploadTooLargeMessage(file)}`;
  if(typeof setStatus==='function')setStatus(`\u274c ${message}`);
  else if(typeof showToast==='function')showToast(message,5000,'error');
}

function addFiles(files){
  for(const f of files){
    if(f&&f.size>MAX_UPLOAD_BYTES){_showUploadTooLarge(f);continue;}
    if(!S.pendingFiles.find(p=>p.name===f.name))S.pendingFiles.push(f);
  }
  renderTray();
}

export {
  renderTray,
  _uploadTooLargeMessage,
  _showUploadTooLarge,
  addFiles,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  renderTray: { enumerable: true, get: () => renderTray, set: (value) => { renderTray = value; } },
  _uploadTooLargeMessage: { enumerable: true, get: () => _uploadTooLargeMessage, set: (value) => { _uploadTooLargeMessage = value; } },
  _showUploadTooLarge: { enumerable: true, get: () => _showUploadTooLarge, set: (value) => { _showUploadTooLarge = value; } },
  addFiles: { enumerable: true, get: () => addFiles, set: (value) => { addFiles = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
