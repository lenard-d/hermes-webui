import { $, S } from './state.js';

const _uploadPendingFilesProgressBySession=new Map();

function _uploadPendingFilesCurrentSession(sessionId){
  return !!(!sessionId||(S.session&&S.session.session_id===sessionId));
}

function _uploadPendingFilesHideProgressBar(){
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  if(!bar||!barWrap)return;
  barWrap.classList.remove('active');
  bar.style.width='0%';
  if(barWrap.dataset)delete barWrap.dataset.uploadSessionId;
}

function _uploadPendingFilesShowProgressBar(owner,percent){
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  if(!bar||!barWrap)return;
  if(barWrap.dataset)barWrap.dataset.uploadSessionId=owner;
  barWrap.classList.add('active');
  bar.style.width=`${Math.max(0,Math.min(100,Number(percent)||0))}%`;
}

function _uploadPendingFilesSyncProgressForSession(sessionId){
  const owner=String(sessionId||'');
  const state=owner?_uploadPendingFilesProgressBySession.get(owner):null;
  if(state){_uploadPendingFilesShowProgressBar(owner,state.percent);return;}
  _uploadPendingFilesHideProgressBar();
}

function _uploadPendingFilesUpdateProgress(sessionId,percent){
  const bar=$('uploadBar');const barWrap=$('uploadBarWrap');
  if(!bar||!barWrap)return;
  const owner=String(sessionId||'');
  const activeForOwner=barWrap.dataset&&barWrap.dataset.uploadSessionId===owner;
  if(percent===null){
    if(owner)_uploadPendingFilesProgressBySession.delete(owner);
    if(activeForOwner)_uploadPendingFilesHideProgressBar();
    return;
  }
  const clamped=Math.max(0,Math.min(100,Number(percent)||0));
  if(owner)_uploadPendingFilesProgressBySession.set(owner,{percent:clamped});
  if(!_uploadPendingFilesCurrentSession(sessionId)){
    if(activeForOwner)_uploadPendingFilesHideProgressBar();
    return;
  }
  _uploadPendingFilesShowProgressBar(owner,clamped);
}

export {
  _uploadPendingFilesProgressBySession,
  _uploadPendingFilesCurrentSession,
  _uploadPendingFilesHideProgressBar,
  _uploadPendingFilesShowProgressBar,
  _uploadPendingFilesSyncProgressForSession,
  _uploadPendingFilesUpdateProgress,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _uploadPendingFilesProgressBySession: { enumerable: true, get: () => _uploadPendingFilesProgressBySession },
  _uploadPendingFilesCurrentSession: { enumerable: true, get: () => _uploadPendingFilesCurrentSession, set: (value) => { _uploadPendingFilesCurrentSession = value; } },
  _uploadPendingFilesHideProgressBar: { enumerable: true, get: () => _uploadPendingFilesHideProgressBar, set: (value) => { _uploadPendingFilesHideProgressBar = value; } },
  _uploadPendingFilesShowProgressBar: { enumerable: true, get: () => _uploadPendingFilesShowProgressBar, set: (value) => { _uploadPendingFilesShowProgressBar = value; } },
  _uploadPendingFilesSyncProgressForSession: { enumerable: true, get: () => _uploadPendingFilesSyncProgressForSession, set: (value) => { _uploadPendingFilesSyncProgressForSession = value; } },
  _uploadPendingFilesUpdateProgress: { enumerable: true, get: () => _uploadPendingFilesUpdateProgress, set: (value) => { _uploadPendingFilesUpdateProgress = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
