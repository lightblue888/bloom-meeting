// Someone without a photo of their own: Bloom's flower in their colour, drawn as the guest page draws it
// (relay/index.html: flower(), guestColor()); their name is written in the same colour wherever it shows.
import type { Participant } from '../meetingApi';

export const HOST_COLOR = '#60a5fa';
export const CLAUDE_COLOR = '#e3906f';
/** a guest's colour when the backend sent none, by their order among the guests: bright, clear of the host's blue and
 * Claude's orange (the guest page's PALETTE) */
const PALETTE = ['#4ade80', '#facc15', '#f472b6', '#22d3ee', '#c084fc', '#f87171', '#2dd4bf', '#a3e635', '#e879f9', '#fbbf24'];

/** A name written in its person's colour: the colours are picked to read on the dark theme; the light theme mixes them
 *  with black (`--name-mix`, meeting.css) so a name keeps at least 3:1 against the light greys. Use it for `color:`
 *  wherever a name is drawn in its person's colour (the flower itself keeps the colour as picked). */
export const nameColor = (c: string | null | undefined): string | undefined =>
  c ? `color-mix(in srgb, ${c} var(--name-mix, 100%), #000)` : undefined;

const isClaude = (p: Participant) => p.id === 'claude';
/** the host: the one ranked so. Only a list without ranks (an older backend) falls back on "you" — on the guest page
 *  "you" is the guest, who was drawn in the host's blue next to the host (2026-11-20 review) */
const hostOf = (people: Participant[]) => people.find((p) => p.rank === 'host') ?? (people.some((p) => p.rank) ? undefined : people.find((p) => p.you));
const same = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/** a photo of their own — not the backend's stand-in for guests (/avatars/bloom.png) */
export const ownPhoto = (avatar: string | null | undefined): avatar is string => !!avatar && !/\/avatars\/bloom\.png(?:[?#]|$)/i.test(avatar);

/** each person's colour, by id: the host blue, Claude orange, a guest the backend's colour (else the palette's, in
 *  order). Nobody else gets the host's or the AI's colour: a guest whose colour is one of theirs, or a guest without one,
 *  takes the next palette colour nobody has yet */
export function colorsById(people: Participant[]): Record<string, string> {
  const out: Record<string, string> = Object.create(null);
  const host = hostOf(people);
  const taken: string[] = [];
  // the host's and the AI's are the ones picked in Settings → Profile (sent with the people); blue / orange otherwise
  for (const p of people) {
    if (p === host) out[p.id] = p.color || HOST_COLOR;
    else if (isClaude(p)) out[p.id] = p.color || CLAUDE_COLOR;
    else continue;
    taken.push(out[p.id]);
  }
  const reserved = [...taken];
  for (const p of people) if (p !== host && !isClaude(p) && p.color && !reserved.some((c) => same(c, p.color!))) taken.push(p.color);
  let k = 0;
  const next = () => {
    for (let i = 0; i < PALETTE.length; i++) {
      const c = PALETTE[(k + i) % PALETTE.length];
      if (!taken.some((x) => same(x, c))) {
        k = (k + i + 1) % PALETTE.length;
        taken.push(c);
        return c;
      }
    }
    // more people than colours: round the palette again
    return PALETTE[k++ % PALETTE.length];
  };
  for (const p of people) {
    if (p === host || isClaude(p)) continue;
    out[p.id] = p.color && !reserved.some((c) => same(c, p.color!)) ? p.color : next();
  }
  return out;
}

/** colours by name, for text that has only names (a guest's lines, the 纪要): the people's own first, then any other
 * name the next palette colour along. `me` stands in for the host when the list has none. */
export function colorsByName(people: Participant[], others: string[] = [], me?: string | null): Record<string, string> {
  const byId = colorsById(people);
  const out: Record<string, string> = Object.create(null);
  for (const p of people) if (p.name && out[p.name] === undefined) out[p.name] = byId[p.id];
  if (me && !hostOf(people) && out[me] === undefined) out[me] = HOST_COLOR;
  const ai = people.find(isClaude)?.name ?? 'Claude';            // the AI, whatever it is called
  if (out[ai] === undefined) out[ai] = CLAUDE_COLOR;
  // the others: the palette's colours nobody has yet, in order (then round again)
  const used = Object.values(out);
  const free = PALETTE.filter((c) => !used.some((u) => same(u, c)));
  const pool = free.length ? free : PALETTE;
  let k = 0;
  for (const raw of others) {
    const n = (raw ?? '').trim();
    if (n && out[n] === undefined) out[n] = pool[k++ % pool.length];
  }
  return out;
}

/** Bloom's five petals in one colour, the centre lighter (the guest page's avatar for anyone without a photo) */
export function Flower({ color }: { color: string }) {
  return (
    <svg viewBox="0 0 240 240" width="100%" height="100%" aria-hidden="true">
      <g transform="translate(120,120) scale(.82)">
        {[0, 1, 2, 3, 4].map((k) => (
          <ellipse key={k} cy="-46" rx="22" ry="40" fill={color} opacity={k % 2 === 0 ? 1 : 0.78} transform={`rotate(${k * 72})`} />
        ))}
        <circle r="26" fill={color} opacity={0.55} />
        <circle r="9" fill="#fff" opacity={0.9} />
      </g>
    </svg>
  );
}
