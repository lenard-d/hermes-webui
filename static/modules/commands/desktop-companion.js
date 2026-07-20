const DESKTOP_COMPANION_EXTENSION_ID='desktop-companion';
const DESKTOP_COMPANION_NAME='Desktop Companion';
const DESKTOP_COMPANION_INSTALL_PATH='Settings -> Extensions -> Gallery -> Desktop Companion';
const DESKTOP_COMPANION_SETUP_GUIDE_URL='https://github.com/franksong2702/hermes-webui-desktop-companion#after-gallery-install';
const DESKTOP_COMPANION_LOCAL_APP_LABEL='Desktop Companion app';

function _getDesktopCompanionStatusGlobal(){
  if(typeof window==='undefined') return null;
  return window.__HERMES_WEBUI_DESKTOP_COMPANION_STATUS__||null;
}

function _getDesktopCompanionExtensionStatus(status){
  return Array.isArray(status&&status.extensions)
    ? status.extensions.find(ext=>String(ext&&ext.id||'')===DESKTOP_COMPANION_EXTENSION_ID)||null
    : null;
}

function _petSlashCommandArgs(rawCommandText){
  return String(rawCommandText||'').trim().replace(/^\/pet\b\s*/i,'').trim().split(/\s+/).filter(Boolean).join(' ');
}

function _desktopCompanionMissingMessage(){
  return `${DESKTOP_COMPANION_NAME} is not installed yet.

Install it from ${DESKTOP_COMPANION_INSTALL_PATH}, then follow the setup guide: ${DESKTOP_COMPANION_SETUP_GUIDE_URL}.

The guide also shows how to start the ${DESKTOP_COMPANION_LOCAL_APP_LABEL}.`;
}

function _desktopCompanionDisabledMessage(){
  return `${DESKTOP_COMPANION_NAME} is installed but disabled.

Enable it in Settings -> Extensions, then reload WebUI if you just changed that and start the ${DESKTOP_COMPANION_LOCAL_APP_LABEL}.

Setup guide: ${DESKTOP_COMPANION_SETUP_GUIDE_URL}`;
}

function _desktopCompanionReloadMessage(){
  return `${DESKTOP_COMPANION_NAME} is enabled, but the adapter status is not loaded yet.

Reload WebUI if you just enabled it, then start the ${DESKTOP_COMPANION_LOCAL_APP_LABEL}.`;
}

function _desktopCompanionConnectMessage(){
  return `${DESKTOP_COMPANION_NAME} is enabled, but the local app is not connected yet.

Start or connect the ${DESKTOP_COMPANION_LOCAL_APP_LABEL}, then retry /pet.

Setup guide: ${DESKTOP_COMPANION_SETUP_GUIDE_URL}`;
}

function _desktopCompanionStatusUnavailableMessage(){
  return `${DESKTOP_COMPANION_NAME} status is unavailable right now.

Reload WebUI or check your connection, then retry /pet.`;
}

function _desktopCompanionUnavailableMessage(){
  return `${DESKTOP_COMPANION_NAME} is installed and connected, but /pet is not available yet in this Desktop Companion version.

Update the ${DESKTOP_COMPANION_LOCAL_APP_LABEL}, then follow the setup guide: ${DESKTOP_COMPANION_SETUP_GUIDE_URL}.`;
}

function _desktopCompanionHookErrorMessage(){
  return `${DESKTOP_COMPANION_NAME} is installed and connected, but it hit an error while handling /pet.

Check the browser console and the ${DESKTOP_COMPANION_LOCAL_APP_LABEL}, then retry /pet.`;
}

async function handlePetSlashCommand(rawCommandText,meta){
  const command=String(rawCommandText||'');
  const commandName=String((meta&&meta.name)||'pet').trim()||'pet';
  const args=_petSlashCommandArgs(command);
  let status;
  try{
    status=await api('/api/extensions/status');
  }catch(_e){
    return {handled:false,message:_desktopCompanionStatusUnavailableMessage()};
  }
  const companion=_getDesktopCompanionExtensionStatus(status);
  if(!companion){
    return {handled:false,message:_desktopCompanionMissingMessage()};
  }
  if(companion.effective_enabled!==true){
    return {handled:false,message:_desktopCompanionDisabledMessage()};
  }
  const companionStatus=_getDesktopCompanionStatusGlobal();
  if(!companionStatus){
    return {handled:false,message:_desktopCompanionReloadMessage()};
  }
  if(companionStatus.connected!==true){
    return {handled:false,message:_desktopCompanionConnectMessage()};
  }
  const hook=typeof window!=='undefined'&&window.__hermesHandlePetSlashCommand;
  if(typeof hook==='function'){
    try{
      const result=await hook({
        command,
        args,
        source:'webui-slash-command',
        metadata:{name:commandName},
      });
      if(result){
        return {
          handled:true,
          message:result&&typeof result==='object'&&'message' in result
            ? String(result.message??'')
            : '',
        };
      }
    }catch(_e){
      if(typeof console!=='undefined'&&console.error){
        console.error('[hermes] Desktop Companion /pet hook error:',_e);
      }
      return {handled:false,message:_desktopCompanionHookErrorMessage()};
    }
  }
  return {handled:false,message:_desktopCompanionUnavailableMessage()};
}

export {handlePetSlashCommand};
