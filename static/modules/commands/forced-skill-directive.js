let _forcedSkillDirectivePending=null;

function publishForcedSkillDirective(pending){
  _forcedSkillDirectivePending=pending;
}

function getForcedSkillDirective(){
  return _forcedSkillDirectivePending;
}

function clearForcedSkillDirective(pending){
  if(_forcedSkillDirectivePending!==pending)return false;
  _forcedSkillDirectivePending=null;
  return true;
}

export {
  clearForcedSkillDirective,
  getForcedSkillDirective,
  publishForcedSkillDirective,
};
