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

const isHost = (p: Participant) => !!p.you || p.rank === 'host';
const isClaude = (p: Participant) => p.id === 'claude';

/** a photo of their own — not the backend's stand-in for guests (/avatars/bloom.png) */
export const ownPhoto = (avatar: string | null | undefined): avatar is string => !!avatar && !/\/avatars\/bloom\.png(?:[?#]|$)/i.test(avatar);

/** each person's colour, by id: the host blue, Claude orange, a guest the backend's colour (else the palette's, in order) */
export function colorsById(people: Participant[]): Record<string, string> {
  const out: Record<string, string> = Object.create(null);
  let k = 0;
  for (const p of people) {
    // the host's and the AI's are the ones picked in Settings → Profile (sent with the people); blue / orange otherwise
    if (isHost(p)) out[p.id] = p.color || HOST_COLOR;
    else if (isClaude(p)) out[p.id] = p.color || CLAUDE_COLOR;
    else {
      out[p.id] = p.color || PALETTE[k % PALETTE.length];
      k++;
    }
  }
  return out;
}

/** colours by name, for text that has only names (a guest's lines, the 纪要): the people's own first, then any other
 * name the next palette colour along. `me` stands in for the host when the list has none. */
export function colorsByName(people: Participant[], others: string[] = [], me?: string | null): Record<string, string> {
  const byId = colorsById(people);
  const out: Record<string, string> = Object.create(null);
  for (const p of people) if (p.name && out[p.name] === undefined) out[p.name] = byId[p.id];
  if (me && !people.some(isHost) && out[me] === undefined) out[me] = HOST_COLOR;
  const ai = people.find(isClaude)?.name ?? 'Claude';            // the AI, whatever it is called
  if (out[ai] === undefined) out[ai] = CLAUDE_COLOR;
  let k = people.filter((p) => !isHost(p) && !isClaude(p)).length;
  for (const raw of others) {
    const n = (raw ?? '').trim();
    if (n && out[n] === undefined) out[n] = PALETTE[k++ % PALETTE.length];
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
