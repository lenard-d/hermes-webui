import { loadCsvInline, loadDiffInline, loadExcalidrawInline, loadHtmlInline, loadPdfInline } from './artifact-postprocessing.js';
import { addCopyButtons, highlightCode, initTreeViews } from './code-postprocessing.js';
import { renderKatexBlocks, renderMermaidBlocks } from './markdown-postprocessing.js';
import { _suppressBrowserOverflowAnchor } from './navigation.js';
import { $ } from './state.js';

// The content post-processing transaction has one ordered interface. Each
// renderer behind it owns its own loading/cache lifecycle, while this module
// owns invocation order and the scroll-anchor suppression window around all
// DOM-height-changing work.
function postProcessRenderedMessages(container) {
  highlightCode(container);
  addCopyButtons(container);
  loadDiffInline(container);
  loadCsvInline(container);
  loadExcalidrawInline(container);
  loadPdfInline(container);
  loadHtmlInline(container);
  renderMermaidBlocks(container);
  renderKatexBlocks(container);
  initTreeViews(container);
}

function _postProcessWithAnchorSuppression(container){
  const scroller=$('messages');
  const release=(scroller&&typeof _suppressBrowserOverflowAnchor==='function')
    ? _suppressBrowserOverflowAnchor(scroller) : null;
  try{
    postProcessRenderedMessages(container);
  }finally{
    // Retain suppression for one additional frame so media measurement cannot
    // trigger the browser's native overflow-anchor after JS restored scroll.
    if(release){
      if(typeof requestAnimationFrame==='function') requestAnimationFrame(release);
      else release();
    }
  }
}

export { _postProcessWithAnchorSuppression, postProcessRenderedMessages };

const compatibilityBindings = {};
Object.defineProperties(compatibilityBindings, {
  _postProcessWithAnchorSuppression: { enumerable: true, get: () => _postProcessWithAnchorSuppression, set: (value) => { _postProcessWithAnchorSuppression = value; } },
  postProcessRenderedMessages: { enumerable: true, get: () => postProcessRenderedMessages, set: (value) => { postProcessRenderedMessages = value; } },
});
Object.freeze(compatibilityBindings);
export { compatibilityBindings };
