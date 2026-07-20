import { hashWorklogDetailKey } from './worklog-disclosure-identity.js';

function _toolIdentity(tc){
  if(!tc) return '';
  const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
  if(tid) return `id:${tid}`;
  const args=tc.args&&typeof tc.args==='object'?tc.args:{};
  return [
    tc.assistant_msg_idx!==undefined?`a:${tc.assistant_msg_idx}`:'',
    tc.name||'tool',
    JSON.stringify(args),
    String(tc.snippet||tc.preview||'').slice(0,160),
  ].join('|');
}

function _toolDisclosureIdentity(tc){
  if(!tc) return '';
  const tid=tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id||'';
  if(tid) return `id:${tid}`;
  const stable=[
    tc.assistant_msg_idx!==undefined?`a:${tc.assistant_msg_idx}`:'',
    tc.name||'tool',
  ].join('\x1f');
  return stable.trim()?`derived:${hashWorklogDetailKey(stable)}`:'';
}

export { _toolIdentity, _toolDisclosureIdentity };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _toolIdentity: { enumerable: true, get: () => _toolIdentity, set: (value) => { _toolIdentity = value; } },
  _toolDisclosureIdentity: { enumerable: true, get: () => _toolDisclosureIdentity, set: (value) => { _toolDisclosureIdentity = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
