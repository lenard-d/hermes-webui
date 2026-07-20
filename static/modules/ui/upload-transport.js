import { setStatus, showToast } from './composer.js';
import { _ARCHIVE_EXTS } from './media-and-quota.js';
import { MAX_UPLOAD_BYTES, S, _redirectIfUnauth } from './state.js';
import { renderTray, _uploadTooLargeMessage } from './upload-tray.js';
import {
  _uploadPendingFilesCurrentSession,
  _uploadPendingFilesUpdateProgress,
} from './upload-status.js';

async function uploadPendingFiles(options={}){
  const opts=options||{};
  const pendingFiles=Array.isArray(opts.files)?opts.files.filter(Boolean):[...(S.pendingFiles||[])];
  const sessionId=String(opts.sessionId||(S.session&&S.session.session_id)||'');
  if(!pendingFiles.length||!sessionId)return[];
  const clearPending=!(opts&&opts.clearPending===false);
  const names=[];let failures=0;
  _uploadPendingFilesUpdateProgress(sessionId,0);
  const total=pendingFiles.length;
  for(let i=0;i<total;i++){
    const f=pendingFiles[i];
    try{
      if(f&&f.size>MAX_UPLOAD_BYTES)throw new Error(_uploadTooLargeMessage(f));
      const fd=new FormData();
      fd.append('session_id',sessionId);fd.append('file',f,f.name);
      const isArchive=_ARCHIVE_EXTS.test(f.name);
      const url=new URL(isArchive?'api/upload/extract':'api/upload',document.baseURI||location.href).href;
      const res=await fetch(url,{method:'POST',credentials:'include',body:fd});
      if(_redirectIfUnauth(res)) return;
      if(!res.ok){const err=await res.text();throw new Error(err);}
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      if(isArchive){
        names.push({name: data.dest, path: data.dest, extracted: data.extracted});
        if(typeof loadDir==='function'&&_uploadPendingFilesCurrentSession(sessionId))loadDir(S.currentDir||'.');
      }else{
        names.push({name: data.filename, path: data.path, mime: data.mime, size: data.size, is_image: !!data.is_image});
      }
    }catch(e){failures++;setStatus(`\u274c ${t('upload_failed')}${f.name} \u2014 ${e.message}`);}
    _uploadPendingFilesUpdateProgress(sessionId,Math.round((i+1)/total*100));
  }
  _uploadPendingFilesUpdateProgress(sessionId,null);
  if(clearPending&&_uploadPendingFilesCurrentSession(sessionId)){S.pendingFiles=[];renderTray();}
  else if(_uploadPendingFilesCurrentSession(sessionId))renderTray();
  if(failures===total&&total>0)throw new Error(t('all_uploads_failed',total));
  const extracted=names.filter(n=>n.extracted);
  if(extracted.length)showToast(t('archive_extracted',extracted.reduce((s,n)=>s+n.extracted,0),extracted.length));
  return names;
}

export { uploadPendingFiles };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  uploadPendingFiles: { enumerable: true, get: () => uploadPendingFiles, set: (value) => { uploadPendingFiles = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
