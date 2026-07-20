import { _thinkingActivityNode } from './activity-presentation.js';
import { buildToolCard } from './tool-card-presentation.js';
import { _toolIdentity } from './tool-identity.js';
import { _syncToolRowsContainer, _toolWorklogListEl } from './worklog-tool-groups.js';
import { _appendWorklogReason } from './worklog-reasoning.js';

function _filterNewWorklogTools(cards, seenTools){
  const out=[];
  for(const tc of Array.from(cards||[]).filter(Boolean)){
    const key=_toolIdentity(tc);
    if(key&&seenTools&&seenTools.has(key)) continue;
    if(key&&seenTools) seenTools.add(key);
    out.push(tc);
  }
  return out;
}

function _appendWorklogStep(group, anchor, cards, thinkingText, opts){
  const list=_toolWorklogListEl(group);
  if(!group||!list) return;
  let wroteProse=false;
  const seenReasons=opts&&opts.seenReasons;
  if(!opts||opts.includeAnchorReason!==false){
    const anchorKey=anchor&&anchor.dataset&&anchor.dataset.msgIdx?`anchor:${anchor.dataset.msgIdx}`:'';
    if(!anchorKey||!seenReasons||!seenReasons.has(anchorKey)){
      const reason=_appendWorklogReason(list, anchor);
      if(reason){
        wroteProse=true;
        if(anchorKey&&seenReasons) seenReasons.add(anchorKey);
      }
    }
  }
  if(thinkingText){
    const thinkingKey=(opts&&opts.thinkingKey)||`reason:${String(thinkingText).trim()}`;
    const thinkingDisclosureKey=(opts&&opts.thinkingDisclosureKey)||thinkingKey;
    if(!seenReasons||!seenReasons.has(thinkingKey)){
      const thinking=_thinkingActivityNode(thinkingText, false, thinkingDisclosureKey);
      if(thinking){
        list.appendChild(thinking);
        wroteProse=true;
        if(seenReasons) seenReasons.add(thinkingKey);
      }
    }
  }
  const toolCards=_filterNewWorklogTools(cards, opts&&opts.seenTools);
  if(toolCards.length){
    const last=list.lastElementChild;
    let tools=(!wroteProse&&last&&last.classList&&last.classList.contains('wl-step-tools')&&last.getAttribute('data-worklog-tools')==='1')
      ? last
      : null;
    if(!tools){
      tools=document.createElement('div');
      tools.className='wl-step-tools tool-worklog-tools';
      tools.setAttribute('data-worklog-tools','1');
      list.appendChild(tools);
    }
    for(const tc of toolCards) tools.appendChild(buildToolCard(tc));
    _syncToolRowsContainer(tools, !!(opts&&opts.live));
  }
}

export {
  _filterNewWorklogTools,
  _appendWorklogStep,
};

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _filterNewWorklogTools: { enumerable: true, get: () => _filterNewWorklogTools, set: (value) => { _filterNewWorklogTools = value; } },
  _appendWorklogStep: { enumerable: true, get: () => _appendWorklogStep, set: (value) => { _appendWorklogStep = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
