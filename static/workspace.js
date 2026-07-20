async function api(path,opts={}){
  // Strip leading slash so URL resolves relative to location.href (supports subpath mounts)
  const rel = path.startsWith('/') ? path.slice(1) : path;
  const url=new URL(rel,document.baseURI||location.href);
  const timeoutMs=Object.prototype.hasOwnProperty.call(opts,'timeoutMs')?opts.timeoutMs:30000;
  const timeoutToast=opts.timeoutToast!==false;
  const redirect401=opts.redirect401!==false;
  const maxAttempts=Object.prototype.hasOwnProperty.call(opts,'retries')?Math.max(0,Number(opts.retries)||0)+1:3;
  const retryTimeouts=opts.retryTimeouts===true;
  const retryStatuses=Array.isArray(opts.retryStatuses)?opts.retryStatuses.map(Number).filter(Number.isFinite):[];
  const retryDelayMs=Object.prototype.hasOwnProperty.call(opts,'retryDelayMs')?Math.max(0,Number(opts.retryDelayMs)||0):350;
  // Retry up to 2 times on network errors (e.g. stale keep-alive after long idle).
  // Callers may opt into retrying timeouts / transient server statuses for idempotent GETs.
  let lastErr;
  for(let attempt=0;attempt<maxAttempts;attempt++){
    let controller=null;
    let timeoutId=null;
    let didTimeout=false;
    let upstreamSignal=null;
    let upstreamAbort=null;
    try{
      const fetchOpts={...opts};
      delete fetchOpts.timeoutMs;
      delete fetchOpts.timeoutToast;
      delete fetchOpts.redirect401;
      delete fetchOpts.retries;
      delete fetchOpts.retryTimeouts;
      delete fetchOpts.retryStatuses;
      delete fetchOpts.retryDelayMs;

      const useTimeout=Number.isFinite(Number(timeoutMs))&&Number(timeoutMs)>0;
      if(useTimeout&&typeof AbortController!=='undefined'){
        controller=new AbortController();
        upstreamSignal=fetchOpts.signal||null;
        if(upstreamSignal){
          upstreamAbort=()=>controller.abort(upstreamSignal.reason);
          if(upstreamSignal.aborted) upstreamAbort();
          else upstreamSignal.addEventListener('abort',upstreamAbort,{once:true});
        }
        fetchOpts.signal=controller.signal;
      }
      const requestPromise=(async()=>{
        const res=await fetch(url.href,{credentials:'include',headers:{'Content-Type':'application/json'},...fetchOpts});
        if(!res.ok){
          // 401 means the auth session expired. Redirect to login so the user can
          // re-authenticate. This is especially important for iOS PWA (standalone mode)
          // and for subpath mounts like /hermes/, where /login escapes to the site root.
          if(res.status===401){
            // #5578: if we're ALREADY on the login page, appending
            // window.location.pathname+search (which contains ?next=…) into a
            // fresh next= wraps the login URL into itself and re-encodes it —
            // exponential URL growth on each expired-auth bounce until the tab
            // breaks. On the login page, just reload login WITHOUT a next (the
            // page preserves its own inner next); elsewhere, capture the path.
            if(redirect401){
              // Already on the login page? Reload login WITHOUT a next.
              const _p=(window.location.pathname||'').replace(/\/+$/,'');
              if(/(?:^|\/)login$/.test(_p)){
                window.location.href='login';
              }else{
                window.location.href='login?next='+encodeURIComponent(window.location.pathname+window.location.search);
              }
            }
            // Callers can opt out of navigation and handle the unauthenticated state themselves.
            return;
          }
          const text=await res.text();
          // Parse JSON error body and surface the human-readable message,
          // rather than showing raw JSON like {"error":"Profile 'x' does not exist."}
          let message=text;
          try{const j=JSON.parse(text);message=j.error||j.message||text;}catch(e){}
          // Attach the raw HTTP context so callers can branch on status (404 stale-session
          // cleanup, 401 redirect, 503 retry, etc.) without re-parsing the message string.
          const err=new Error(message);
          err.status=res.status;
          err.statusText=res.statusText;
          err.body=text;
          throw err;
        }
        const ct=res.headers.get('content-type')||'';
        return ct.includes('application/json')?await res.json():await res.text();
      })();
      return useTimeout?await Promise.race([
        requestPromise,
        new Promise((_,reject)=>{
          timeoutId=setTimeout(()=>{
            didTimeout=true;
            if(controller) controller.abort();
            const err=new Error('Request timed out. Please try again.');
            err.name='TimeoutError';
            err.timeout=true;
            reject(err);
          },Number(timeoutMs));
        })
      ]):await requestPromise;
    }catch(e){
      lastErr=e;
      const isTimeout=didTimeout||(e&&(e.timeout===true||e.name==='TimeoutError'));
      if(isTimeout){
        if(retryTimeouts&&attempt<2&&attempt<maxAttempts-1){
          if(retryDelayMs) await new Promise(resolve=>setTimeout(resolve,retryDelayMs*Math.pow(2,attempt)));
          continue;
        }
        const err=(e&&e.name==='TimeoutError')?e:new Error('Request timed out. Please try again.');
        err.name='TimeoutError';
        err.timeout=true;
        if(timeoutToast&&typeof showToast==='function') showToast('Request timed out. Please try again.',5000,'error');
        throw err;
      }
      // Only retry on network errors (TypeError from fetch), not on HTTP errors
      // that were already thrown above. Re-throw 401 redirects immediately.
      if(e.message&&/401/.test(e.message)) throw e;
      if(attempt<2&&attempt<maxAttempts-1 && (e instanceof TypeError || retryStatuses.includes(Number(e.status)))){
        if(retryDelayMs) await new Promise(resolve=>setTimeout(resolve,retryDelayMs*Math.pow(2,attempt)));
        continue;
      }
      throw e;
    }finally{
      if(timeoutId) clearTimeout(timeoutId);
      if(upstreamSignal&&upstreamAbort) upstreamSignal.removeEventListener('abort',upstreamAbort);
    }
  }
  throw lastErr;
}

function recordClientSSEError(source, details={}){
  try{
    const payload={
      event:'sse_error',
      source:String(source||'unknown'),
      ready_state:details.ready_state,
      session_id:details.session_id||null,
      stream_id:details.stream_id||null,
      visibility_state:(typeof document!=='undefined'&&document.visibilityState)||'unknown',
      online:(typeof navigator!=='undefined'&&typeof navigator.onLine==='boolean')?navigator.onLine:null,
      url_path:(typeof location!=='undefined'&&location.pathname)||'/',
      reason:details.reason||'EventSource.onerror',
    };
    void api('/api/client-events/log',{method:'POST',body:JSON.stringify(payload),timeoutMs:3000,timeoutToast:false}).catch(()=>{});
  }catch(_){}
}

// Persist/restore expanded directory state per workspace in localStorage

(function installWorkspaceFacade(root){
  const workspace=root.HermesWorkspace||{};
  workspace.parts=workspace.parts||Object.create(null);
  workspace.loadOrder=Object.freeze([
    '001-navigation.js',
    '002-preview-editor.js',
    '003-upload.js',
  ]);
  workspace.transport=Object.freeze({api,recordClientSSEError});
  root.HermesWorkspace=workspace;
})(globalThis);
