// A task's remark is what was said, then what the AI said back on the spot ("（当场复述的改法：…）", older tasks
// "（Claude 当场复述…）"), added for the worker. Cards show the words said only (zuhui/view.py _drop_said_back does
// the same on the paper's cards). The said-back can hold brackets of its own ("第（2）段"): its end is found by pairing
// them; cut short (no closing bracket), it goes to the end. Other notes after the words ("（又补充了：…）") stay.
const SAID_BACK = /（(?:[^（）\s]{1,24} )?当场复述/g;

export function ownWords(remark: string | null | undefined): string {
  const s = remark ?? '';
  let out = '';
  let i = 0;
  SAID_BACK.lastIndex = 0;
  for (let m = SAID_BACK.exec(s); m; m = SAID_BACK.exec(s)) {
    out += s.slice(i, m.index);
    let depth = 0;
    let j = m.index;
    while (j < s.length) {
      const ch = s[j++];
      depth += ch === '（' ? 1 : ch === '）' ? -1 : 0;
      if (depth === 0) break;
    }
    i = j;
    SAID_BACK.lastIndex = j;
  }
  return (out + s.slice(i)).trim();
}
