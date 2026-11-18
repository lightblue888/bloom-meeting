// 复制（带格式）: the clipboard gets the same text twice — as HTML (headings, lists; Word, 飞书, 腾讯文档 keep them)
// and as plain text (a chat box, Notepad). The async clipboard first; where it can't (no ClipboardItem, refused), a
// hidden copy of the HTML is selected and copied the old way, which also puts both kinds on the clipboard.

export async function copyRich(html: string, text: string): Promise<void> {
  try {
    if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([text], { type: 'text/plain' }),
        }),
      ]);
      return;
    }
  } catch {
    /* the old way below */
  }
  const box = document.createElement('div');
  box.setAttribute('contenteditable', 'true');
  box.setAttribute('aria-hidden', 'true');
  // off screen, never seen; still laid out so the selection holds the formatting
  box.style.cssText = 'position:fixed;left:-10000px;top:0;width:600px;opacity:0;pointer-events:none;white-space:normal';
  box.innerHTML = html;
  document.body.appendChild(box);
  const sel = window.getSelection();
  const keep = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
  try {
    const range = document.createRange();
    range.selectNodeContents(box);
    sel?.removeAllRanges();
    sel?.addRange(range);
    if (!document.execCommand('copy')) throw new Error('clipboard');
  } finally {
    sel?.removeAllRanges();
    if (keep) sel?.addRange(keep);
    box.remove();
  }
}
