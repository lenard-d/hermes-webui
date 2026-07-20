async function cmdWorkspace(args){
  if(!args){showToast(t('workspace_usage'));return;}
  try{
    const data=await api('/api/workspaces');
    const query=args.toLowerCase();
    const workspace=(data.workspaces||[]).find(candidate=>
      (candidate.name||'').toLowerCase().includes(query)||candidate.path.toLowerCase().includes(query)
    );
    if(!workspace){showToast(t('no_workspace_match')+`"${args}"`);return;}
    if(typeof switchToWorkspace==='function'){
      await switchToWorkspace(workspace.path,workspace.name||workspace.path);
    }else{
      showToast(t('switched_workspace')+(workspace.name||workspace.path));
    }
  }catch(error){
    showToast(t('workspace_switch_failed')+error.message);
  }
}

async function cmdTerminal(){
  let data=null;
  try{
    data=await api('/api/workspaces');
    if(typeof syncTerminalBackendState==='function')syncTerminalBackendState(data);
    if(data&&data.terminal_remote_backend){
      const message=typeof _terminalRemoteBackendUnsupportedMessage==='function'
        ?_terminalRemoteBackendUnsupportedMessage()
        :'Embedded terminal is only supported for local terminal backends.';
      showToast(message,3200,'warning');
      if(typeof syncTerminalButton==='function')syncTerminalButton();
      return;
    }
  }catch(_){}
  if(!S.session&&typeof newSession==='function'){
    if(!S._profileSwitchWorkspace&&!S._profileDefaultWorkspace){
      const first=(data&&data.workspaces||[])[0];
      S._profileSwitchWorkspace=(data&&data.last)||(first&&first.path)||null;
    }
    await newSession(false, {worktree: false});
    if(typeof renderSessionList==='function')await renderSessionList();
  }
  if(!S.session||!S.session.workspace){
    showToast(t('terminal_no_workspace_title'),2600,'warning');
    if(typeof syncTerminalButton==='function')syncTerminalButton();
    return;
  }
  if(typeof toggleComposerTerminal==='function')await toggleComposerTerminal(true);
}

export {cmdTerminal,cmdWorkspace};
