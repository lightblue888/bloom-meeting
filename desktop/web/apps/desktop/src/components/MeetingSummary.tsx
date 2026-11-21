// The 纪要 tab's two faces (docs/meeting-ui-spec.md §当前会议). While the meeting is live the 纪要 tab is the task
// board — 排队 / 改中 / 已改 in three columns, a card = the task's title, the teacher's words, the student's report.
// After 散会 it is the Summary, laid out as Tencent Meeting's AI summary: title, speakers, the abstract, 核心结论 in a
// blue-edged box, each topic's 01 / 02 cards, 后续工作安排.
import { useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { Icon } from '@blurt/icons';
import { mi18n as i18n, mt as t } from '../i18n';
import { stepV } from './stepText';
import {
  DOC_EDIT_TAG,
  fmtDuration,
  itemText,
  meetingApi,
  multiPaper,
  SUMMARY_TEMPLATES,
  type Board,
  type Line,
  type Participant,
  type SumItem,
  type Summary,
  type SummaryJob,
  type SummaryPrefs,
  type SummaryTemplate,
  type TalkTime,
  type Task,
  type TaskStatus,
  type Usage,
} from '../meetingApi';
import { fmtCosts, fmtTokens, usageDetail } from '../usageText';
import { Names, nameStyle } from './Highlight';
import { colorsByName, nameColor } from './Flower';
import { useMenu } from './Menu';
import { ticksToMove, todoKey } from '../todoTicks';
import { ownWords } from '../ownWords';

const COLUMNS: { status: TaskStatus[]; key: string; cls: string; note?: boolean }[] = [
  { status: ['queued'], key: 'meeting.board.queued', cls: 'queued' },
  { status: ['working'], key: 'meeting.board.working', cls: 'working' },
  { status: ['done', 'failed'], key: 'meeting.board.done', cls: 'done' },
  // notes: things said in the meeting that could not be done there (new data, new results); the text is unchanged
  { status: ['queued', 'working', 'done'], key: 'meeting.board.notes', cls: 'notes', note: true },
];

/** where a card sits: the section; with more than one paper in the meeting, the paper's name first */
const whereOf = (x: Task, multi: boolean, section: string | null | undefined) =>
  [multi ? x.paper_name : null, section].filter(Boolean).join(' · ');

export function TaskBoard({ board }: { board: Board | null }) {
  const tasks = board?.tasks ?? [];
  const multi = multiPaper(tasks);
  if (!tasks.length)
    return (
      <div className="mt-empty">
        <Icon name="list-box" size={28} />
      </div>
    );
  return (
    <div className="mt-board">
      {COLUMNS.filter((c) => !c.note || tasks.some((x) => x.note)).map((c) => {
        const rows = tasks.filter((x) => !!x.note === !!c.note && c.status.includes(x.status)).sort((a, b) => (c.cls === 'done' ? b.idx - a.idx : a.idx - b.idx));
        return (
          <section key={c.cls} className={`mt-col is-${c.cls}`} aria-label={t(c.key)}>
            <h3>
              <span className="mt-col-dot" />
              {t(c.key)}
              <span className="mt-col-n">{i18n().formatNumber(rows.length)}</span>
            </h3>
            <div className="mt-col-body">
              {rows.map((x) => <TaskCard key={x.id} task={x} multi={multi} />)}
            </div>
          </section>
        );
      })}
    </div>
  );
}

/** "已改 2 处 · 在 3.2 节改第 3 处 · 1 分 30 秒": what the editing worker is doing right now */
function Progress({ p }: { p: NonNullable<Task['progress']> }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const h = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(h);
  }, []);
  const secs = p.started ? Math.max(0, Math.floor(Date.now() / 1000 - p.started)) : null;
  const parts = [
    p.edits ? t('meeting.board.progress.edits', { n: p.edits }) : null,
    stepV(p.step ?? undefined) || null,
    secs != null ? fmtDuration(secs) : null,
  ].filter(Boolean);
  if (!parts.length) return null;
  return <p className="mt-card-progress">{parts.join(' · ')}</p>;
}

/** the kinds of work a note waits for (zuhui/board.py NOTE_TAGS), in their fixed order */
const NOTE_TAGS = ['rerun', 'data', 'figure', 'lit', 'discuss'];

/** a note's chips: its state (会后改 / 已处理, when `state`) and the kinds of work it waits for (要重新跑实验…) */
export function NoteTags({ task, state }: { task: Task; state?: boolean }) {
  const kinds = (task.tags ?? []).filter((k) => NOTE_TAGS.includes(k));
  if (!state && !kinds.length) return null;
  return (
    <span className="mt-ntags">
      {state && (
        <span className={`mt-ntag ${task.resolved ? 'is-ok' : 'is-st'}`}>
          {t(task.resolved ? 'meeting.tag.handled' : 'meeting.tag.after')}
        </span>
      )}
      {kinds.map((k) => (
        <span key={k} className="mt-ntag">
          {t(`meeting.tag.${k}`)}
        </span>
      ))}
    </span>
  );
}

function NoteCard({ task, multi }: { task: Task; multi: boolean }) {
  const where = whereOf(task, multi, task.section || task.focus?.section);
  const said = ownWords(task.remark);
  return (
    <article className="mt-card is-note">
      <div className="mt-card-head">
        <span className="mt-card-flag" aria-hidden="true">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14V2.5M4 3h7.5l-1.8 2.7L11.5 8.5H4" /></svg>
        </span>
        <span className="mt-card-title">{task.title}</span>
      </div>
      {said && <p className="mt-card-quote">{t('meeting.board.teacher_said', { a: said })}</p>}
      {task.plan ? (
        <p className="mt-card-report">{t('meeting.todos.plan', { a: task.plan })}</p>
      ) : task.status !== 'done' ? (
        <p className="mt-card-report">{t('meeting.todos.writing')}</p>
      ) : null}
      {task.need && <p className="mt-card-report">{t('meeting.todos.need', { a: task.need })}</p>}
      <NoteTags task={task} state />
      {where && <span className="mt-card-section">{where}</span>}
    </article>
  );
}

function TaskCard({ task, multi }: { task: Task; multi: boolean }) {
  if (task.note) return <NoteCard task={task} multi={multi} />;
  const report = task.status === 'failed' ? task.error : task.result;
  const where = whereOf(task, multi, task.section);
  const said = ownWords(task.remark);
  return (
    <article className={`mt-card is-${task.status}`}>
      <div className="mt-card-head">
        <span className="mt-card-title">{task.title}</span>
        {task.status === 'working' && <span className="mw-spinner sm" aria-hidden="true" />}
        {task.status === 'failed' && <span className="mt-card-bad">{t('meeting.board.failed')}</span>}
      </div>
      {said && said !== task.title && <p className="mt-card-quote">{t('meeting.board.teacher_said', { a: said })}</p>}
      {task.status === 'working' && task.progress && <Progress p={task.progress} />}
      {report && <p className="mt-card-report">{t('meeting.board.student_said', { a: report })}</p>}
      {where && <span className="mt-card-section">{where}</span>}
    </article>
  );
}

// 修改清单 (To-dos): the notes in paper order (abstract, then numbered sections, then the rest), each with what to do,
// who asked, how to change it and what it needs; the edits already made in the meeting at the bottom, ticked.
// Ticks are this viewer's own (kept in the browser); the list can be copied as Markdown for the advisor or a to-do app.
function secRank(s: string): number[] {
  if (!s) return [9999];
  if (/^(摘要|abstract|标题|title)/i.test(s)) return [0];
  const m = s.match(/^(\d+(?:\.\d+)*)/);
  if (m) return [1, ...m[1].split('.').map(Number)];
  if (/^(附录|appendix)/i.test(s)) return [8000];
  return [5000];
}
function cmpRank(a: number[], b: number[]) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? -1, y = b[i] ?? -1;
    if (x !== y) return x - y;
  }
  return 0;
}
function saidBy(remark: string): { who: string; text: string } {
  const m = remark.match(/^(?:参会的老师|参会者)(.+?)说[:：]\s*/);
  return m ? { who: m[1], text: ownWords(remark.slice(m[0].length)) } : { who: '', text: ownWords(remark) };
}

/** a change typed straight into the paper (the transcript's #doc_edit line): 改前「…」→ 改后「…」, for the row's tooltip */
const handDetail = (l: Line) =>
  [l.before ? t('meeting.todos.hand_before', { a: l.before }) : '', l.after ? t('meeting.todos.hand_after', { a: l.after }) : '']
    .filter(Boolean)
    .join('\n');

export function TodoList({ board, meetingId, done, hand }: {
  board: Board | null;
  meetingId: string | null;
  done?: string[] | null;
  /** the changes typed straight into the paper (#doc_edit lines): listed with 会上已改, after the AI's */
  hand?: Line[];
}) {
  const key = todoKey(meetingId);
  // a tick is saved with the meeting (carry.json, "note:<task id>"): ticked here, it is off the lobby's left-overs too.
  // What the server says, with the clicks still on their way laid over it
  const [mine, setMine] = useState<Record<string, boolean>>({});
  // another meeting opened in the same place: its own ticks only
  useEffect(() => setMine({}), [meetingId]);
  const server = useMemo(() => new Set((done ?? []).filter((k) => k.startsWith('note:')).map((k) => k.slice(5))), [done]);
  const ticked = useMemo(() => {
    const s = new Set(server);
    for (const [id, on] of Object.entries(mine)) {
      if (on) s.add(id);
      else s.delete(id);
    }
    return [...s];
  }, [server, mine]);
  // once: the ticks this browser kept before (localStorage) move to carry.json, then the old copy goes
  useEffect(() => {
    if (!meetingId || done == null) return;
    let local: unknown = null;
    try {
      local = JSON.parse(localStorage.getItem(key) || 'null');
    } catch {
      local = null;
    }
    if (local == null) return;
    const move = ticksToMove(local, done);
    setMine((m) => ({ ...Object.fromEntries(move.map((id) => [id, true])), ...m }));
    Promise.all(move.map((id) => meetingApi.tickCarry(`${meetingId}/note:${id}`, true))).then(
      () => {
        try {
          localStorage.removeItem(key);
        } catch {
          /* private window */
        }
      },
      () => {},
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId, done == null]);
  const [failed, setFailed] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const tasks = board?.tasks ?? [];
  const notes = tasks.filter((x) => x.note);
  const edits = tasks.filter((x) => !x.note && x.status === 'done' && !x.rejected);
  const byHand = (hand ?? []).filter((l) => l.tag === DOC_EDIT_TAG && l.text);
  const where = (x: Task) => x.section || x.focus?.section || '';
  // more than one paper in the meeting: grouped by paper (in the order they were opened), then by section
  const multi = multiPaper(tasks);
  const paperOrder = [...new Set(tasks.map((x) => x.paper_name || ''))];
  const SEP = '\u0001';
  // feedback about the app itself: one group of its own, after the paper's sections, never under a paper's name
  const APP = '\u0002app';
  const isApp = (x: Task) => !!x.app || x.kind === 'app';
  const keyOf = (x: Task) => (isApp(x) ? APP : multi ? `${x.paper_name || ''}${SEP}${where(x)}` : where(x));
  const partsOf = (k: string): [string, string] => (multi ? (k.split(SEP) as [string, string]) : ['', k]);
  const label = (k: string) => {
    if (k === APP) return t('meeting.todos.app');
    const [p, s] = partsOf(k);
    return [p, s || t('meeting.todos.other')].filter(Boolean).join(' · ');
  };
  const groups = new Map<string, Task[]>();
  for (const x of [...notes].sort((a, b) => a.idx - b.idx)) {
    const k = keyOf(x);
    groups.set(k, [...(groups.get(k) ?? []), x]);
  }
  const order = [...groups.keys()].sort((a, b) => {
    if (a === APP || b === APP) return (a === APP ? 1 : 0) - (b === APP ? 1 : 0);
    const [pa, sa] = partsOf(a);
    const [pb, sb] = partsOf(b);
    return paperOrder.indexOf(pa) - paperOrder.indexOf(pb) || cmpRank(secRank(sa), secRank(sb));
  });
  const toggle = (id: string) => {
    const on = !ticked.includes(id);
    setMine((m) => ({ ...m, [id]: on }));
    if (!meetingId) return;
    meetingApi.tickCarry(`${meetingId}/note:${id}`, on).then(
      () => setFailed(null),
      (e) => {
        setMine((m) => ({ ...m, [id]: !on }));
        setFailed(t('lobby.carry.failed', { a: String((e as Error)?.message ?? e) }));
      },
    );
  };
  const left = notes.filter((x) => !ticked.includes(x.id)).length;
  const copy = async () => {
    const md: string[] = [`# ${t('meeting.todos.title')}`];
    for (const k of order) {
      md.push('', `## ${label(k)}`);
      for (const x of groups.get(k)!) {
        const s = saidBy(x.remark || '');
        md.push(`- [${ticked.includes(x.id) ? 'x' : ' '}] ${x.title}${x.need ? `（${x.need}）` : ''}`);
        if (x.plan) md.push(`  - ${t('meeting.todos.plan', { a: x.plan })}`);
        const kinds = (x.tags ?? []).filter((k) => NOTE_TAGS.includes(k)).map((k) => t(`meeting.tag.${k}`));
        if (kinds.length) md.push(`  - ${kinds.join(i18n().lang.startsWith('en') ? ', ' : '、')}`);
        if (s.text) md.push(`  - ${s.who ? s.who + ' ' : ''}${s.text}`);
      }
    }
    if (edits.length || byHand.length) {
      md.push('', `## ${t('meeting.todos.done_in_meeting')}`);
      for (const x of edits) md.push(`- [x] ${multi && x.paper_name ? `${x.paper_name} · ` : ''}${x.title}`);
      for (const l of byHand) md.push(`- [x] ${l.text}`, ...handDetail(l).split('\n').filter(Boolean).map((s) => `  - ${s}`));
    }
    try {
      await navigator.clipboard.writeText(md.join('\n'));
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* no clipboard here */
    }
  };
  if (!notes.length && !edits.length && !byHand.length)
    return (
      <div className="mt-empty">
        <p>{t('meeting.todos.empty')}</p>
      </div>
    );
  return (
    <div className="mt-todos" data-testid="todos">
      <div className="mt-todos-head">
        <h2>{t('meeting.todos.title')}</h2>
        <span className="mt-todos-n">{t('meeting.todos.count', { a: String(left), b: String(edits.length + byHand.length) })}</span>
        <button className="mt-btn mt-todos-copy" onClick={() => void copy()} data-testid="todos-copy">
          {copied ? t('meeting.todos.copied') : t('meeting.todos.copy')}
        </button>
      </div>
      {/* a tick that didn't save: said over the list for a moment (nothing moves) */}
      {failed && <p className="mt-todos-err" role="alert">{failed}</p>}
      {order.map((k) => (
        <section key={k} className="mt-todos-grp">
          <h3>{label(k)}</h3>
          {groups.get(k)!.map((x) => {
            const s = saidBy(x.remark || '');
            const done = ticked.includes(x.id);
            return (
              <label key={x.id} className={`mt-todo ${done ? 'is-done' : ''}`}>
                <input type="checkbox" checked={done} onChange={() => toggle(x.id)} />
                <span className="mt-todo-t">{x.title}</span>
                {x.need && <span className="mt-todo-tag">{x.need}</span>}
                <span className="mt-todo-s">
                  {s.who && <b>{s.who} </b>}
                  {s.text}
                  {x.plan ? (
                    <>
                      <br />
                      {t('meeting.todos.plan', { a: x.plan })}
                    </>
                  ) : x.status !== 'done' ? (
                    <>
                      <br />
                      {t('meeting.todos.writing')}
                    </>
                  ) : null}
                </span>
                <NoteTags task={x} />
              </label>
            );
          })}
        </section>
      ))}
      {(edits.length > 0 || byHand.length > 0) && (
        <section className="mt-todos-grp">
          <h3>{t('meeting.todos.done_in_meeting')}</h3>
          {edits.map((x) => (
            <label key={x.id} className="mt-todo is-done is-edit">
              <input type="checkbox" checked readOnly />
              <span className="mt-todo-t">{multi && x.paper_name ? `${x.paper_name} · ` : ''}{x.title}</span>
              <span className="mt-todo-tag is-edit">{t('meeting.todos.done_tag')}</span>
            </label>
          ))}
          {/* typed straight into the paper by someone: the transcript's line, what changed on hover */}
          {byHand.map((l) => (
            <label key={`hand-${l.sec}-${l.text}`} className="mt-todo is-done is-edit" title={handDetail(l) || undefined} data-testid="todo-hand">
              <input type="checkbox" checked readOnly />
              <span className="mt-todo-t">{l.text}</span>
              <span className="mt-todo-tag is-edit">{t('meeting.todos.hand_tag')}</span>
            </label>
          ))}
        </section>
      )}
    </div>
  );
}

/** where in the meeting a 纪要 line was said: a small "12:34" after it; a click plays the recording from there and
 *  shows the transcript line (MeetingLayout's jump). Nothing when the summary doesn't say (older summaries). */
function At({ x, onJump }: { x: unknown; onJump?: (sec: number) => void }) {
  const o = x && typeof x === 'object' ? (x as { t?: string | null; sec?: number | null }) : null;
  if (!o || typeof o.sec !== 'number') return null;
  const sec = o.sec;
  const label = o.t || fmtDuration(sec);
  if (!onJump) return <span className="mt-sum-at is-static">{label}</span>;
  return (
    <button type="button" className="mt-sum-at" onClick={() => onJump(sec)} aria-label={t('meeting.summary.jump', { a: label })} data-tip={t('meeting.summary.jump', { a: label })} data-testid="summary-at">
      {label}
    </button>
  );
}

/** a feedback / ask line: who said it in front ("林晓：…") */
const byText = (x: SumItem) => {
  const s = itemText(x);
  const by = typeof x === 'object' ? x?.by : null;
  return by ? `${by}：${s}` : s;
};

const PERSON_KEYS = ['said', 'feedback', 'asks'] as const;

export function SummaryView({ summary, people, talk, usage, onJump }: {
  summary: Summary;
  people?: Participant[];
  /** each person's talk time (from the transcript), longest first */
  talk?: TalkTime[] | null;
  /** the meeting's API tokens and estimated cost (none with a Claude Code account) */
  usage?: Usage | null;
  /** a line's time clicked: play the recording from there, show the transcript line */
  onJump?: (sec: number) => void;
}) {
  const n2 = (i: number) => String(i + 1).padStart(2, '0');
  const crowd = people ?? [];
  // the people in this 纪要: their names stand out as chips wherever the text mentions them
  // the backend lists the host first and the AI second (whatever it is called)
  const ai = crowd.find((p) => p.id === 'claude' || p.role === 'student')?.name ?? summary.speakers?.[1] ?? 'AI';
  const host = crowd.find((p) => p.you)?.name ?? crowd.find((p) => p.role === 'teacher')?.name ?? summary.speakers?.[0] ?? t('meeting.rank.host');
  const persons = summary.people ?? [];
  const bys = persons.flatMap((p) => [...(p.feedback ?? []), ...(p.asks ?? [])].map((x) => (typeof x === 'object' ? x?.by ?? '' : '')));
  const talkName = (x: TalkTime) => (x.who === 'teacher' ? host : x.who === 'student' ? ai : x.name || t('meeting.who.guest'));
  const rows = talk ?? [];
  const names = [...new Set([...(summary.speakers ?? []), ai, ...persons.map((p) => p.name), ...bys, ...rows.map(talkName)])].filter(
    (n) => n && !['老师', '学生', '主持人', '助手'].includes(n),
  );
  const me = names.find((n) => n !== ai) ?? null;
  // everyone in their own colour (the one their flower has); the first speaker stands in for the host when nobody is listed
  const colors = colorsByName(crowd, names, me);
  const N = (x: string) => <Names text={x} names={names} colors={colors} />;
  const heads = summary.template === 'progress' ? 'meeting.summary.progress.' : 'meeting.summary.';
  const peopleBlock = persons.length > 0 && (
    <section className="mt-sum-block" data-testid="summary-people">
      <h4>{t('meeting.summary.people')}</h4>
      <div className="mt-sum-cards mt-sum-people">
        {persons.map((p, i) => (
          <article key={i} className="mt-sum-card">
            <h5>
              <span className="mt-name" style={nameStyle(colors[p.name])} title={p.name}>
                <span className="mt-name-t">{p.name}</span>
              </span>
            </h5>
            {PERSON_KEYS.map((k) =>
              p[k]?.length ? (
                <div key={k} className="mt-sum-pk">
                  <h6>{t(`${heads}${k}`)}</h6>
                  <ul>
                    {p[k]!.map((x, j) => (
                      <li key={j}>
                        {N(k === 'said' ? itemText(x) : byText(x))}
                        <At x={x} onJump={onJump} />
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null,
            )}
          </article>
        ))}
      </div>
    </section>
  );
  const topics = summary.sections?.map((s, i) => (
    <section key={i} className="mt-sum-topic">
      <h4>
        <span className="mt-sum-num">{n2(i)}</span>
        <span className="mt-sum-h">{s.title}</span>
        <At x={s} onJump={onJump} />
      </h4>
      <div className="mt-sum-cards">
        {s.cards?.map((c, j) => (
          <article key={j} className="mt-sum-card">
            <h5>{c.title}</h5>
            <ul>
              {c.bullets?.map((b, k) => (
                <li key={k}>
                  {N(itemText(b))}
                  <At x={b} onJump={onJump} />
                </li>
              ))}
            </ul>
          </article>
        ))}
      </div>
    </section>
  ));
  const top = Math.max(0.0001, ...rows.map((x) => x.share));
  return (
    <div className="mt-summary">
      <h2 className="mt-sum-title">{summary.title}</h2>
      {summary.speakers?.length > 0 && (
        <p className="mt-sum-speakers">
          <Icon name="voice" size={13} />
          {summary.speakers.map((s, i) => (
            <span key={i} className="mt-name" style={nameStyle(colors[s])} title={s}>
              <span className="mt-name-t">{s}</span>
            </span>
          ))}
        </p>
      )}
      {summary.abstract && (
        <section className="mt-sum-block">
          <h4>{t('meeting.summary.abstract')}</h4>
          <p>{N(summary.abstract)}</p>
        </section>
      )}
      {summary.conclusions?.length > 0 && (
        <section className="mt-sum-block">
          <h4>{t('meeting.summary.conclusions')}</h4>
          <ol className="mt-sum-conclusions">
            {summary.conclusions.map((c, i) => (
              <li key={i}>
                {N(itemText(c))}
                <At x={c} onJump={onJump} />
              </li>
            ))}
          </ol>
        </section>
      )}
      {/* 进展汇报: each person first; the other formats: the topics first */}
      {summary.template === 'progress' ? (
        <>
          {peopleBlock}
          {topics}
        </>
      ) : (
        <>
          {topics}
          {peopleBlock}
        </>
      )}
      {summary.todos?.length > 0 && (
        <section className="mt-sum-block todos">
          <h4>{t('meeting.summary.todos')}</h4>
          <ul className="mt-sum-todos">
            {summary.todos.map((x, i) => (
              <li key={i}>
                <Icon name="check" size={13} />
                <span>
                  {N(itemText(x))}
                  <At x={x} onJump={onJump} />
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {/* what earlier meetings left: done this time first, then what is still open */}
      {(summary.carried?.length ?? 0) > 0 && (
        <section className="mt-sum-block" data-testid="summary-carried">
          <h4>{t('meeting.summary.carried')}</h4>
          <ul className="mt-sum-carried">
            {[...summary.carried!].sort((a, b) => Number(b.done) - Number(a.done)).map((c) => (
              <li key={c.id} className={c.done ? 'is-done' : ''}>
                <span className={`mt-sum-cflag ${c.done ? 'is-done' : ''}`}>{t(c.done ? 'meeting.summary.carried_done' : 'meeting.summary.carried_open')}</span>
                <span className="mt-sum-cbody">
                  <span className="mt-sum-citem">{N(c.item)}</span>
                  {c.text ? <span className="mt-sum-cnote">{N(c.text)}</span> : null}
                  <At x={c} onJump={onJump} />
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {rows.length > 0 && (
        <section className="mt-sum-block" data-testid="summary-talk">
          <h4>{t('meeting.summary.talk')}</h4>
          <ul className="mt-sum-talk">
            {rows.map((x) => {
              const name = talkName(x);
              return (
                <li key={`${x.who}-${x.name ?? ''}`}>
                  <span className="mt-sum-talk-who" style={{ color: nameColor(colors[name]) }} title={name}>{name}</span>
                  <span className="mt-sum-talk-bar" aria-hidden="true">
                    <span style={{ width: `${Math.max(2, (x.share / top) * 100)}%`, background: colors[name] }} />
                  </span>
                  <span className="mt-sum-talk-t">{fmtDuration(x.sec)}</span>
                  <span className="mt-sum-talk-p">{`${Math.round(x.share * 100)}%`}</span>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {/* the meeting's API tokens and estimated cost, one model a row (an API source only) */}
      {(usage?.models.length ?? 0) > 0 && (
        <section className="mt-sum-block" data-testid="summary-usage">
          <h4>{t('meeting.summary.usage')}</h4>
          <ul className="mt-sum-usage">
            {usage!.models.map((m) => {
              const name = m.pretty || m.model;
              const cost = m.cost != null && m.currency ? fmtCosts({ [m.currency]: m.cost }) : '—';
              return (
                <li key={`${m.label}/${m.model}`}>
                  <span className="mt-sum-usage-m" title={m.label ? `${m.label} · ${name}` : name}>{name}</span>
                  <span className="mt-sum-usage-t" title={usageDetail(m)}>{fmtTokens(m.tokens)}</span>
                  <span className="mt-sum-usage-c" title={m.cost == null ? t('meeting.summary.usage_unpriced') : undefined}>{cost}</span>
                </li>
              );
            })}
            {usage!.models.length > 1 && (
              <li className="is-total">
                <span className="mt-sum-usage-m">{t('meeting.summary.usage_total')}</span>
                <span className="mt-sum-usage-t">{fmtTokens(usage!.tokens)}</span>
                <span className="mt-sum-usage-c">{fmtCosts(usage!.costs) || '—'}</span>
              </li>
            )}
          </ul>
        </section>
      )}
    </div>
  );
}

const REGEN_ERRORS = ['busy', 'custom_empty', 'template', 'not_found'];
const regenError = (e: unknown) => {
  const c = String((e as Error)?.message ?? e);
  return REGEN_ERRORS.includes(c) ? t(`meeting.summary.err.${c}`) : c;
};

/** failures already said (meeting:start), so reopening the meeting doesn't say it again */
const toldFailed = new Set<string>();

/** 纪要's tool in the bar above it (回顾): 重新生成 ▾, a menu of the formats (the current one ticked); picking one writes
 *  the 纪要 again in it — the model runs only then. While it runs the button says so in the same box; a failure is
 *  said once in a toast (nothing on the page moves). */
export function SummaryTools({ meetingId, summary, job, busy, onStarted, onToast }: {
  meetingId: string;
  summary: Summary | null;
  job?: SummaryJob | null;
  /** the meeting is on, or its post-meeting work still runs: nothing to write again yet */
  busy: boolean;
  /** the job was accepted: fetch the meeting again (its summary_job) */
  onStarted: () => void;
  onToast?: (msg: string) => void;
}) {
  const [prefs, setPrefs] = useState<SummaryPrefs | null>(null);
  useEffect(() => {
    let alive = true;
    meetingApi.summaryPrefs().then((p) => alive && setPrefs(p), () => {});
    return () => {
      alive = false;
    };
  }, []);
  // the format the 纪要 on screen was written in (older ones: the default, the only one then)
  const cur: SummaryTemplate = summary ? summary.template ?? 'default' : prefs?.template ?? 'default';
  // asked, and the meeting's next poll hasn't said "running" yet: already shown as running (no flicker back)
  const [asked, setAsked] = useState(false);
  useEffect(() => {
    if (job || !asked) return;
    const h = setTimeout(() => setAsked(false), 15_000);
    return () => clearTimeout(h);
  }, [job, asked]);
  useEffect(() => {
    if (job) setAsked(false);
  }, [job]);
  useEffect(() => setAsked(false), [summary?.generated]);
  const running = asked || job?.state === 'running';
  // the job failed in the background: said once
  const failKey = job?.state === 'failed' ? `${meetingId}:${job.started ?? ''}` : null;
  useEffect(() => {
    if (!failKey || toldFailed.has(failKey) || !job) return;
    toldFailed.add(failKey);
    onToast?.(t('meeting.summary.regen_failed', { a: regenError(job.error ?? '') }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failKey]);
  const go = async (k: SummaryTemplate) => {
    setAsked(true);
    try {
      await meetingApi.regenSummary(meetingId, k);
      onStarted();
    } catch (e) {
      setAsked(false);
      onToast?.(t('meeting.summary.regen_failed', { a: regenError(e) }));
    }
  };
  const menu = useMenu();
  // 自定义 only when its words are written (Settings › 纪要): a greyed-out item nobody can pick read as unfinished
  // (2026-11-21, the user: "自定义是什么")
  const shown = SUMMARY_TEMPLATES.filter((k) => k !== 'custom' || !!prefs?.custom.trim() || (!!summary && cur === 'custom'));
  const openMenu = (e: ReactMouseEvent<HTMLButtonElement>) =>
    menu.open(
      shown.map((k) => ({
        label: t(`meeting.summary.template.${k}`),
        checked: !!summary && k === cur,
        run: () => void go(k),
        testId: `summary-template-${k}`,
      })),
      { x: 0, y: 0, anchor: e.currentTarget, align: 'start', minWidth: 180 },
      t('meeting.summary.format'),
    );
  // the button says the format the 纪要 is in ("格式：论文讨论"); picking another writes it again in that one. It used to
  // say 重新生成, a menu of formats behind it ("重新生成这个也太奇怪了")
  const label = summary ? `fmt:${cur}` : 'gen';
  const words = (k: string) => (k === 'gen' ? t('meeting.summary.gen') : t('meeting.summary.format_btn', { a: t(`meeting.summary.template.${k.slice(4)}`) }));
  return (
    <div className="mt-ptools mt-sum-tools" data-testid="summary-tools">
      {/* the words stacked in one box as wide as the widest: 格式：论文讨论 → 生成中… never changes its size */}
      <button className="mt-pbtn mt-sum-regen" onClick={openMenu} disabled={running || busy} aria-haspopup="menu" aria-expanded={menu.isOpen}
        aria-busy={running || undefined} data-testid="summary-regen">
        <span className="mt-wstack">
          {['gen', ...SUMMARY_TEMPLATES.map((k) => `fmt:${k}`)].map((k) => (
            <span key={k} className={!running && k === label ? undefined : 'is-ghost'} aria-hidden={!running && k === label ? undefined : true}>
              {words(k)}
              <Icon name="chevron-down" size={12} />
            </span>
          ))}
          <span className={running ? undefined : 'is-ghost'} aria-hidden={running ? undefined : true}>
            <span className="mw-spinner sm" aria-hidden="true" />
            {t('meeting.summary.generating')}
          </span>
        </span>
      </button>
      {menu.node}
    </div>
  );
}
