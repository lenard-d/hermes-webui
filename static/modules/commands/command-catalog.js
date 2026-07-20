import {cmdPersonality,cmdSkills,cmdUse} from './capability-commands.js';
import {handlePetSlashCommand} from './desktop-companion.js';
import {cmdCompact,cmdCompress} from './manual-compression.js';
import {cmdModel} from './model-command.js';
import {cmdReasoning,cmdTheme,cmdUsage,cmdVoice} from './preference-commands.js';
import {cmdGoal,cmdInterrupt,cmdQueue,cmdSteer,cmdStop,cmdYolo} from './run-controls.js';
import {
  cmdBackground,
  cmdBranch,
  cmdBtw,
  cmdClear,
  cmdNew,
  cmdRetry,
  cmdStatus,
  cmdTitle,
  cmdUndo,
} from './session-history.js';
import {cmdTerminal,cmdWorkspace} from './workspace-commands.js';

function cmdHelp(){
  const lines=COMMANDS.map(command=>{
    const usage=command.arg
      ?(String(command.arg).startsWith('[')?` ${command.arg}`:` <${command.arg}>`)
      :'';
    return `  /${command.name}${usage} — ${command.desc}`;
  });
  S.messages.push({role:'assistant',content:t('available_commands')+'\n'+lines.join('\n')});
  renderMessages();
  showToast(t('type_slash'));
}

const COMMANDS=[
  {name:'help',desc:t('cmd_help'),fn:cmdHelp},
  {name:'clear',desc:t('cmd_clear'),fn:cmdClear,noEcho:true},
  {name:'compress',desc:t('cmd_compress'),fn:cmdCompress,arg:'[focus topic]',noEcho:true},
  {name:'compact',desc:t('cmd_compact_alias'),fn:cmdCompact,noEcho:true},
  {name:'model',desc:t('cmd_model'),fn:cmdModel,arg:'model_name',subArgs:'models',noEcho:true},
  {name:'workspace',desc:t('cmd_workspace'),fn:cmdWorkspace,arg:'name',noEcho:true},
  {name:'terminal',desc:t('cmd_terminal'),fn:cmdTerminal,noEcho:true},
  {name:'new',desc:t('cmd_new'),fn:cmdNew,noEcho:true},
  {name:'usage',desc:t('cmd_usage'),fn:cmdUsage,noEcho:true},
  {name:'theme',desc:t('cmd_theme'),fn:cmdTheme,arg:'name',noEcho:true},
  {name:'personality',desc:t('cmd_personality'),fn:cmdPersonality,arg:'name',subArgs:'personalities'},
  {name:'skills',desc:t('cmd_skills'),fn:cmdSkills,arg:'query'},
  {name:'use',desc:t('cmd_use'),fn:cmdUse,arg:'skill-name',subArgs:'skills',noEcho:true},
  {name:'stop',desc:t('cmd_stop'),fn:cmdStop,noEcho:true},
  {name:'goal',desc:t('cmd_goal'),fn:cmdGoal,arg:'[status|pause|resume|clear|text]',subArgs:['status','pause','resume','clear']},
  {name:'queue',desc:t('cmd_queue'),fn:cmdQueue,arg:'message',noEcho:true},
  {name:'interrupt',desc:t('cmd_interrupt'),fn:cmdInterrupt,arg:'message',noEcho:true},
  {name:'steer',desc:t('cmd_steer'),fn:cmdSteer,arg:'message',noEcho:true},
  {name:'title',desc:t('cmd_title'),fn:cmdTitle,arg:'[title]'},
  {name:'retry',desc:t('cmd_retry'),fn:cmdRetry,noEcho:true},
  {name:'undo',desc:t('cmd_undo'),fn:cmdUndo,noEcho:true},
  {name:'btw',desc:t('cmd_btw'),fn:cmdBtw,arg:'question',noEcho:true},
  {name:'background',desc:t('cmd_background'),fn:cmdBackground,arg:'prompt',noEcho:true},
  {name:'status',desc:t('cmd_status'),fn:cmdStatus},
  {name:'voice',desc:t('cmd_voice'),fn:cmdVoice,noEcho:true},
  {name:'reasoning',desc:t('cmd_reasoning'),fn:cmdReasoning,arg:'show|hide|none|minimal|low|medium|high|xhigh|max',subArgs:['show','hide','none','minimal','low','medium','high','xhigh','max'],noEcho:true},
  {name:'yolo',desc:t('cmd_yolo'),fn:cmdYolo,noEcho:true},
  {name:'branch',desc:t('cmd_branch'),fn:cmdBranch,arg:'[name]',noEcho:true},
];

function parseCommand(text){
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\s+/);
  return {
    name:parts[0].toLowerCase(),
    args:parts.slice(1).join(' ').trim(),
  };
}

function executeCommand(text){
  const parsed=parseCommand(text);
  if(!parsed)return null;
  const command=COMMANDS.find(candidate=>candidate.name===parsed.name);
  if(!command)return null;
  if(command.fn(parsed.args)===false)return null;
  return {noEcho:!!command.noEcho};
}

const HANDLERS={};
HANDLERS.skills=cmdSkills;

export {
  COMMANDS,
  HANDLERS,
  executeCommand,
  handlePetSlashCommand,
  parseCommand,
};
