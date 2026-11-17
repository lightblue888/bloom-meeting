// The 纪要 tab's two faces and the 时间轴 tab (docs/meeting-ui-spec.md §当前会议). While the meeting is live the 纪要
// tab is the task board — 排队 / 改中 / 已改 in three columns, a card = the task's title, the teacher's words, the
// student's report. After 散会 it is the Summary, laid out as Tencent Meeting's AI summary: title, speakers, the
// abstract, 核心结论 in a blue-edged box, each topic's 01 / 02 cards, 后续工作安排. The timeline lists what happened
// in order: the teacher spoke → task N was noted → task N was done.
import { useEffect, useMemo, useState } from 'react';
import { Icon } from '@blurt/icons';
import { mi18n as i18n, mt as t } from '../i18n';
import { stepV } from './stepText';
import { fmtDuration, multiPaper, secSince, tagKey, type Board, type Line, type Meeting, type Participant, type Summary, type Task, type TaskStatus } from '../meetingApi';
import { Names, nameStyle } from './Highlight';
import { colorsByName } from './Flower';

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

function NoteCard({ task, multi }: { task: Task; multi: boolean }) {
  const where = whereOf(task, multi, task.section || task.focus?.section);
  return (
    <article className="mt-card is-note">
      <div className="mt-card-head">
        <span className="mt-card-flag" aria-hidden="true">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14V2.5M4 3h7.5l-1.8 2.7L11.5 8.5H4" /></svg>
        </span>
        <span className="mt-card-title">{task.title}</span>
      </div>
      {task.remark && <p className="mt-card-quote">{t('meeting.board.teacher_said', { a: task.remark })}</p>}
      {task.plan ? (
        <p className="mt-card-report">{t('meeting.todos.plan', { a: task.plan })}</p>
      ) : task.status !== 'done' ? (
        <p className="mt-card-report">{t('meeting.todos.writing')}</p>
      ) : null}
      {task.need && <p className="mt-card-report">{t('meeting.todos.need', { a: task.need })}</p>}
      {where && <span className="mt-card-section">{where}</span>}
    </article>
  );
}

function TaskCard({ task, multi }: { task: Task; multi: boolean }) {
  if (task.note) return <NoteCard task={task} multi={multi} />;
  const report = task.status === 'failed' ? task.error : task.result;
  const where = whereOf(task, multi, task.section);
  return (
    <article className={`mt-card is-${task.status}`}>
      <div className="mt-card-head">
        <span className="mt-card-title">{task.title}</span>
        {task.status === 'working' && <span className="mw-spinner sm" aria-hidden="true" />}
        {task.status === 'failed' && <span className="mt-card-bad">{t('meeting.board.failed')}</span>}
      </div>
      {task.remark && task.remark !== task.title && <p className="mt-card-quote">{t('meeting.board.teacher_said', { a: task.remark })}</p>}
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
  return m ? { who: m[1], text: remark.slice(m[0].length) } : { who: '', text: remark };
}
export function TodoList({ board, meetingId }: { board: Board | null; meetingId: string | null }) {
  const key = `bloom-todos-${meetingId ?? ''}`;
  const [ticked, setTicked] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(key) || '[]');
    } catch {
      return [];
    }
  });
  const [copied, setCopied] = useState(false);
  const tasks = board?.tasks ?? [];
  const notes = tasks.filter((x) => x.note);
  const edits = tasks.filter((x) => !x.note && x.status === 'done' && !x.rejected);
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
    const next = ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id];
    setTicked(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      /* private window: ticks just don't stick */
    }
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
        if (s.text) md.push(`  - ${s.who ? s.who + ' ' : ''}${s.text}`);
      }
    }
    if (edits.length) {
      md.push('', `## ${t('meeting.todos.done_in_meeting')}`);
      for (const x of edits) md.push(`- [x] ${multi && x.paper_name ? `${x.paper_name} · ` : ''}${x.title}`);
    }
    try {
      await navigator.clipboard.writeText(md.join('\n'));
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* no clipboard here */
    }
  };
  if (!notes.length && !edits.length)
    return (
      <div className="mt-empty">
        <p>{t('meeting.todos.empty')}</p>
      </div>
    );
  return (
    <div className="mt-todos" data-testid="todos">
      <div className="mt-todos-head">
        <h2>{t('meeting.todos.title')}</h2>
        <span className="mt-todos-n">{t('meeting.todos.count', { a: String(left), b: String(edits.length) })}</span>
        <button className="mt-btn mt-todos-copy" onClick={() => void copy()} data-testid="todos-copy">
          {copied ? t('meeting.todos.copied') : t('meeting.todos.copy')}
        </button>
      </div>
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
              </label>
            );
          })}
        </section>
      ))}
      {edits.length > 0 && (
        <section className="mt-todos-grp">
          <h3>{t('meeting.todos.done_in_meeting')}</h3>
          {edits.map((x) => (
            <label key={x.id} className="mt-todo is-done is-edit">
              <input type="checkbox" checked readOnly />
              <span className="mt-todo-t">{multi && x.paper_name ? `${x.paper_name} · ` : ''}{x.title}</span>
              <span className="mt-todo-tag is-edit">{t('meeting.todos.done_tag')}</span>
            </label>
          ))}
        </section>
      )}
    </div>
  );
}

export function SummaryView({ summary, people }: { summary: Summary; people?: Participant[] }) {
  const n2 = (i: number) => String(i + 1).padStart(2, '0');
  // the people in this 纪要: their names stand out as chips wherever the text mentions them
  const names = [...(summary.speakers ?? []), 'Claude'].filter((n) => n && !['老师', '学生', '主持人', '助手'].includes(n));
  const me = names.find((n) => n !== 'Claude') ?? null;
  // everyone in their own colour (the one their flower has); the first speaker stands in for the host when nobody is listed
  const colors = colorsByName(people ?? [], names, me);
  const N = (x: string) => <Names text={x} names={names} colors={colors} />;
  return (
    <div className="mt-summary">
      <h2 className="mt-sum-title">{summary.title}</h2>
      {summary.speakers?.length > 0 && (
        <p className="mt-sum-speakers">
          <Icon name="voice" size={13} />
          {summary.speakers.map((s, i) => (
            <span key={i} className="mt-name" style={nameStyle(colors[s])}>
              {s}
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
            {summary.conclusions.map((c, i) => <li key={i}>{N(c)}</li>)}
          </ol>
        </section>
      )}
      {summary.sections?.map((s, i) => (
        <section key={i} className="mt-sum-topic">
          <h4>
            <span className="mt-sum-num">{n2(i)}</span>
            {s.title}
          </h4>
          <div className="mt-sum-cards">
            {s.cards?.map((c, j) => (
              <article key={j} className="mt-sum-card">
                <h5>{c.title}</h5>
                <ul>
                  {c.bullets?.map((b, k) => <li key={k}>{N(b)}</li>)}
                </ul>
              </article>
            ))}
          </div>
        </section>
      ))}
      {summary.todos?.length > 0 && (
        <section className="mt-sum-block todos">
          <h4>{t('meeting.summary.todos')}</h4>
          <ul className="mt-sum-todos">
            {summary.todos.map((x, i) => (
              <li key={i}>
                <Icon name="check" size={13} />
                <span>{N(x)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

interface Event {
  sec: number;
  kind: 'teacher' | 'student' | 'noted' | 'done' | 'failed' | 'system';
  text: string;
  idx?: number;
}

/** The transcript and the board merged by time: a teacher line, "noted" / "done" with the edit's own title (no task numbers on screen). */
export function Timeline({ meeting, transcript, board }: { meeting: Meeting | null; transcript: Line[]; board: Board | null }) {
  const events = useMemo(() => {
    const out: Event[] = [];
    for (const l of transcript) {
      if (l.who === 'teacher') out.push({ sec: l.sec, kind: 'teacher', text: l.text });
      else if (l.who === 'student') out.push({ sec: l.sec, kind: 'student', text: l.text });
      else out.push({ sec: l.sec, kind: 'system', text: l.text });
    }
    const started = meeting?.started;
    for (const x of board?.tasks ?? []) {
      const c = secSince(started, x.created);
      if (c != null) out.push({ sec: c, kind: 'noted', text: x.title, idx: x.idx });
      const f = secSince(started, x.finished);
      if (f != null && (x.status === 'done' || x.status === 'failed')) out.push({ sec: f, kind: x.status, text: x.status === 'done' ? x.result || x.title : x.error || x.title, idx: x.idx });
    }
    return out.sort((a, b) => a.sec - b.sec);
  }, [transcript, board, meeting?.started]);
  if (!events.length)
    return (
      <div className="mt-empty">
        <Icon name="history" size={28} />
      </div>
    );
  const label = (e: Event) => {
    switch (e.kind) {
      case 'teacher': return t('meeting.who.teacher');
      case 'student': return t('meeting.who.student');
      case 'noted': return t('meeting.timeline.noted');
      case 'done': return t('meeting.timeline.done');
      case 'failed': return t('meeting.timeline.failed');
      default: return t('meeting.who.system');
    }
  };
  return (
    <ol className="mt-timeline">
      {events.map((e, i) => (
        <li key={i} className={`mt-tl is-${e.kind}`}>
          <span className="mt-tl-time">{fmtDuration(e.sec)}</span>
          <span className="mt-tl-dot" />
          <div className="mt-tl-body">
            <span className="mt-tl-label">{label(e)}</span>
            <span className="mt-tl-text">{e.text}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

/** the short label of a fast-model tag ("#edit" → 改稿), for the transcript's teacher rows */
export function TagChip({ tag }: { tag: string | undefined }) {
  const k = tagKey(tag);
  if (!k) return null;
  return <span className={`mt-tag is-${tag!.replace(/^#/, '').toLowerCase()}`}>{t(k)}</span>;
}
