import {
  clearForcedSkillDirective,
  publishForcedSkillDirective,
} from './forced-skill-directive.js';

async function cmdSkills(args){
  try{
    const data=await api('/api/skills');
    let skills=data.skills||[];
    if(args){
      const query=args.toLowerCase();
      skills=skills.filter(skill=>
        (skill.name||'').toLowerCase().includes(query)
        ||(skill.description||'').toLowerCase().includes(query)
        ||(skill.category||'').toLowerCase().includes(query)
      );
    }
    if(!skills.length){
      S.messages.push({
        role:'assistant',
        content:args?`No skills matching "${args}".`:'No skills found.',
      });
      renderMessages();
      return;
    }
    const byCategory={};
    for(const skill of skills){
      const category=skill.category||'General';
      if(!byCategory[category])byCategory[category]=[];
      byCategory[category].push(skill);
    }
    const lines=[];
    for(const [category,items] of Object.entries(byCategory).sort()){
      lines.push(`**${category}**`);
      for(const skill of items){
        const description=skill.description
          ?` — ${skill.description.slice(0,80)}${skill.description.length>80?'...':''}`
          :'';
        lines.push(`  \`${skill.name}\`${description}`);
      }
      lines.push('');
    }
    const header=args
      ?`Skills matching "${args}" (${skills.length}):\n\n`
      :`Available skills (${skills.length}):\n\n`;
    S.messages.push({role:'assistant',content:header+lines.join('\n')});
    renderMessages();
    showToast(t('type_slash'));
  }catch(error){
    showToast('Failed to load skills: '+error.message);
  }
}

async function cmdUse(args){
  if(!args){
    S.messages.push({
      role:'assistant',
      content:'Usage: `/use <skill-name>` — forces the agent to consult that skill before its next response.',
    });
    renderMessages();
    return;
  }
  let resolve;
  const pending = {sessionId:S.session&&S.session.session_id||null,promise:null};
  pending.promise = new Promise(done=>{resolve=done;});
  publishForcedSkillDirective(pending);
  const isCurrentSession=()=>!pending.sessionId||(S.session&&S.session.session_id)===pending.sessionId;
  try{
    const data=await api('/api/skills');
    const match=(data.skills||[]).find(skill=>(skill.name||'').toLowerCase()===args.toLowerCase());
    if(!match){
      resolve(null);
      clearForcedSkillDirective(pending);
      if(isCurrentSession()){
        S.messages.push({
          role:'assistant',
          content:`No skill named \`${args}\`. Use \`/skills\` to see available skills.`,
        });
        renderMessages();
      }
      return;
    }
    const detail=await api(`/api/skills/content?name=${encodeURIComponent(match.name)}`);
    const content=detail&&typeof detail.content==='string' ? detail.content.trim() : '';
    if(!content)throw new Error(`Skill \`${match.name}\` has no readable content.`);
    const directive=`[USER OVERRIDE] You MUST follow the skill '${match.name}' content provided below before responding to the next message.`;
    resolve({name:match.name,directive,content});
    if(isCurrentSession()){
      S.messages.push({role:'assistant',content:`Next turn: skill \`${match.name}\` will be forced.`});
      renderMessages();
    }
    showToast(`Skill \`${match.name}\` will be used for next turn.`);
  }catch(error){
    resolve(null);
    clearForcedSkillDirective(pending);
    showToast('Failed to load skills: '+error.message);
  }
}

async function cmdPersonality(args){
  if(!S.session){showToast(t('no_active_session'));return;}
  if(!args){
    try{
      const data=await api('/api/personalities');
      if(!data.personalities||!data.personalities.length){
        showToast(t('no_personalities'));
        return;
      }
      const list=data.personalities
        .map(personality=>`  **${personality.name}**${personality.description?' — '+personality.description:''}`)
        .join('\n');
      S.messages.push({
        role:'assistant',
        content:t('available_personalities')+'\n\n'+list+t('personality_switch_hint'),
      });
      renderMessages();
    }catch(_){
      showToast(t('personalities_load_failed'));
    }
    return;
  }
  const name=args.trim();
  if(['none','default','clear'].includes(name.toLowerCase())){
    try{
      await api('/api/personality/set',{
        method:'POST',
        body:JSON.stringify({session_id:S.session.session_id,name:''}),
      });
      showToast(t('personality_cleared'));
    }catch(error){
      showToast(t('failed_colon')+error.message);
    }
    return;
  }
  try{
    await api('/api/personality/set',{
      method:'POST',
      body:JSON.stringify({session_id:S.session.session_id,name}),
    });
    S.messages.push({role:'assistant',content:t('personality_set')+`**${name}**`});
    renderMessages();
    showToast(t('personality_set')+name);
  }catch(error){
    showToast(t('failed_colon')+error.message);
  }
}

export {cmdPersonality,cmdSkills,cmdUse};
