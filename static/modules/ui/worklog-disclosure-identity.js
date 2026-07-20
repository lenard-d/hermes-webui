function hashWorklogDetailKey(value){
  const s=String(value||'');
  let hash=2166136261;
  for(let i=0;i<s.length;i++){
    hash^=s.charCodeAt(i);
    hash=Math.imul(hash,16777619)>>>0;
  }
  return hash.toString(36);
}

export { hashWorklogDetailKey };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  hashWorklogDetailKey: { enumerable: true, get: () => hashWorklogDetailKey, set: (value) => { hashWorklogDetailKey = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
