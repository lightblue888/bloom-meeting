// 当前会议 (docs/meeting-ui-spec.md §会议怎么开始): the page polls the 组会 backend's /api/current every 1.5 s and shows
// what it says. No backend: a notice with 启动 (the Rust command spawns `python -m zuhui serve`). No paper yet: the
// left column is a drop zone — the dropped file's path goes to POST /api/paper, and the paper's name leads the title.
// 开始会议 is Blurt's own recording (capture_start without the picker, the main window as the area); 散会 is its
// stop. The meeting's state is the backend's (idle / live / ended), and the buttons follow it.
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { Icon } from '@blurt/icons';
import { getCurrentWebview } from '@tauri-apps/api/webview';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { inTauri, listen } from '../api';
import { t } from '../i18n';
import { exportReason, meetingApi, POLL_MS, watchBrain, type Brain, type Current, type Line } from '../meetingApi';
import { MeetingLayout } from './MeetingLayout';
import { MeetingLobby } from './MeetingLobby';
import { PaperHistory } from './PaperHistory';
import type { LiveTools } from './MeetingTools';
import { keep } from './useDismiss';

type Backend = 'unknown' | 'up' | 'down' | 'starting';

/** WebView2's host bridge (Windows): a dropped file's path reaches the app this way (folder_drop.rs), a page can't read it */
const webview = () =>
  (window as unknown as { chrome?: { webview?: { postMessageWithAdditionalObjects?: (m: string, o: FileList) => void } } }).chrome?.webview;

const PAPER_EXT = /\.(pdf|md|markdown|tex|latex|docx?|pptx|txt)$/i;

/** an auto-save failure already said (this run, and the last one across restarts): true; else marks it said */
const AUTOSAVE_TOLD = 'bloom.autosave.told';
const toldNow = new Set<string>();
function autosaveTold(key: string): boolean {
  let last: string | null = null;
  try {
    last = localStorage.getItem(AUTOSAVE_TOLD);
  } catch {
    /* private window */
  }
  if (toldNow.has(key) || last === key) return true;
  toldNow.add(key);
  try {
    localStorage.setItem(AUTOSAVE_TOLD, key);
  } catch {
    /* this run only */
  }
  return false;
}
const NO_LINES: Line[] = [];

export function MeetingCurrent({ onToast, active = true }: { onToast: (msg: string) => void; active?: boolean }) {
  const [cur, setCurRaw] = useState<Current | null>(null);
  // the poll answers the same thing most of the time: keep what didn't change identical, so nothing re-renders for it
  const setCur = useCallback((c: Current) => setCurRaw((prev) => keep(prev, c)), []);
  const [backend, setBackend] = useState<Backend>('unknown');
  const [busy, setBusy] = useState<'start' | 'stop' | 'paper' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  /** the paper just imported, before the next poll says so */
  const [paperName, setPaperName] = useState<string | null>(null);
  const startedAt = useRef(0);

  // ---------------------------------------------------------------- the poll
  // every 1.5 s on screen; kept mounted behind 回顾 (for an instant switch back) it slows to every 5 s and asks at once
  // when it comes back
  const shown = useRef(active);
  shown.current = active;
  const kick = useRef<() => void>(() => {});
  // failures in a row: one slow answer is not a dead backend (it can be busy for seconds); only 3 in a row count
  const fails = useRef(0);
  useEffect(() => {
    let alive = true;
    let h: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      clearTimeout(h);
      try {
        const c = await meetingApi.current();
        if (!alive) return;
        fails.current = 0;
        setCur(c);
        setBackend('up');
        if (c.paper?.exists) setPaperName(null);
      } catch {
        if (!alive) return;
        fails.current += 1;
        // a backend just asked for gets a while to come up; a running one that missed an answer or two keeps its page
        setBackend((b) =>
          b === 'starting' && Date.now() - startedAt.current < 20_000
            ? 'starting'
            : b === 'up' && fails.current < 3
              ? 'up'
              : 'down',
        );
      }
      if (alive) h = setTimeout(tick, shown.current ? POLL_MS : 5000);
    };
    kick.current = () => void tick();
    void tick();
    return () => {
      alive = false;
      clearTimeout(h);
    };
  }, [setCur]);
  useEffect(() => {
    if (active) kick.current();
  }, [active]);
  // 散会后自动保存 failed (the backend says so for a few hours): "自动保存失败：原因" once, in a toast (nothing moves)
  const autosave = cur?.autosave ?? null;
  const autosaveKey = autosave && !autosave.ok ? `${autosave.meeting}:${autosave.at}` : null;
  useEffect(() => {
    if (!autosaveKey || !autosave || autosaveTold(autosaveKey)) return;
    onToast(t('meeting.autosave.failed', { a: exportReason({ code: autosave.error ?? '', detail: autosave.detail ?? '' }) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autosaveKey]);
  // "Claude 在干什么" is pushed by the backend the moment it changes (2026-11-13, the user: don't pull it; the page only
  // keeps time). The poll's copy is the fallback while the push channel is down.
  const isLive = cur?.meeting?.state === 'live';
  const [pushed, setPushed] = useState<Brain | null>(null);
  const [pushUp, setPushUp] = useState(false);
  useEffect(() => {
    if (!isLive) {
      setPushed(null);
      setPushUp(false);
      return;
    }
    return watchBrain(
      (b) => {
        setPushUp(true);
        setPushed(b);
      },
      () => setPushUp(false),
    );
  }, [isLive]);
  const polledBrain = cur?.brain ?? null;
  const brainNow = useMemo<Brain | null>(() => {
    if (pushUp && pushed) return pushed;
    if (!polledBrain) return null;
    return typeof polledBrain.now === 'number' ? { ...polledBrain, skew: polledBrain.now - Date.now() / 1000 } : polledBrain;
  }, [pushUp, pushed, polledBrain]);
  // stable callbacks: the page's columns are memoized and must not see "new" handlers on every poll
  const onRole = useCallback(
    (name: string, role: 'cohost' | 'participant' | 'dismiss') => {
      const done = role === 'dismiss' ? meetingApi.dismissRole(name) : meetingApi.setRole(name, role);
      done.then(
        () => meetingApi.current().then(setCur, () => undefined),
        (e) => setError(String((e as Error)?.message ?? e)),
      );
    },
    [setCur],
  );

  // ---------------------------------------------------------------- the paper
  const [importText, setImportText] = useState<string | null>(null);
  const importPaper = useCallback(async (path: string) => {
    if (!path) return;
    setBusy('paper');
    setError(null);
    setImportText(null);
    // MinerU takes a while on a PDF: show where it is, once a second
    let on = true;
    const watch = async () => {
      while (on) {
        try {
          const s = await meetingApi.paperStatus();
          if (on) setImportText(s.busy && s.text ? s.text : null);
        } catch {
          /* the next poll */
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    };
    void watch();
    try {
      const p = await meetingApi.paper(path);
      setPaperName(p?.name ?? null);
      const a = p?.name ?? path.split(/[\\/]/).pop() ?? path;
      // during a meeting the paper joins it (or, already open in it, just comes up) instead of replacing the one on screen
      onToast(t(p?.added === true ? 'meeting.paper.added' : p?.added === false ? 'meeting.paper.switched' : 'meeting.paper.imported', { a }));
      kick.current();
    } catch (e) {
      setError(t('meeting.paper.failed', { a: String((e as Error)?.message ?? e) }));
    } finally {
      on = false;
      setImportText(null);
      setBusy(null);
    }
  }, [onToast]);
  const latest = useRef(importPaper);
  latest.current = importPaper;
  const changePaper = async () => {
    try {
      const path = await meetingApi.pickPaper();
      if (path) await importPaper(path);
    } catch (e) {
      setError(t('meeting.paper.failed', { a: String((e as Error)?.message ?? e) }));
    }
  };
  useEffect(() => {
    // Windows: the path of a file handed over with postMessageWithAdditionalObjects (folder_drop.rs)
    const off = listen<{ paths: string[] }>('mainwin://paper-dropped', (p) => {
      const path = p.paths?.find((x) => PAPER_EXT.test(x)) ?? p.paths?.[0];
      if (path) void latest.current(path);
    });
    // Tauri's own drop events, should the window ever enable them (tauri.conf dragDropEnabled)
    let offDrag: (() => void) | null = null;
    let dead = false;
    if (inTauri) {
      try {
        getCurrentWebview()
          .onDragDropEvent((ev) => {
            if (ev.payload.type === 'drop') {
              const path = ev.payload.paths.find((x) => PAPER_EXT.test(x)) ?? ev.payload.paths[0];
              if (path) void latest.current(path);
            }
          })
          .then((u) => (dead ? u() : (offDrag = u)), () => {});
      } catch {
        /* not available here */
      }
    }
    return () => {
      off();
      dead = true;
      offDrag?.();
    };
  }, []);

  // during a meeting: switch to another of its papers (the title row's paper menu)
  const switchPaper = useCallback(
    async (id: string) => {
      setError(null);
      try {
        const p = await meetingApi.switchPaper(id);
        if (p?.name) onToast(t('meeting.paper.switched', { a: p.name }));
        kick.current();
      } catch (e) {
        setError(t('meeting.paper.switch_failed', { a: String((e as Error)?.message ?? e) }));
      }
    },
    [onToast],
  );
  const meeting = cur?.meeting ?? null;
  const state = meeting?.state ?? 'idle';
  const hasPaper = !!cur?.paper?.exists || !!paperName;

  // 改会议名, before the meeting only (2026-11-14, the user: once it starts the name is fixed, in 回顾 too). Before the
  // meeting there is no meeting to rename yet: the name is kept here and given to the meeting the moment it starts;
  // '' (the field emptied) = the paper's own name, as the backend gives it
  const [nextTitle, setNextTitle] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const renameNext = useCallback(async (title: string) => {
    setNextTitle(title.trim() ? title.trim() : null);
  }, []);
  const liveNow = state === 'live';
  // the lobby until a meeting is on; a meeting that started (here or already running when the app opened) brings up the
  // meeting page, which stays after it ends (its summary) until 新会议 goes back to the lobby
  const [view, setView] = useState<'lobby' | 'room'>('lobby');
  const [lobbyTitle, setLobbyTitle] = useState('');
  // 开始会议 was clicked and the recording started: the backend says "live" a moment later; the button stays busy till then
  const [waitLive, setWaitLive] = useState(0);
  useEffect(() => {
    if (liveNow) {
      setView('room');
      setWaitLive(0);
      setLobbyTitle('');
    }
  }, [liveNow]);
  useEffect(() => {
    if (!waitLive) return;
    const h = setTimeout(() => setWaitLive(0), Math.max(0, waitLive + 20_000 - Date.now()));
    return () => clearTimeout(h);
  }, [waitLive]);
  // a meeting that started while this page watched (not one already running when the app opened)
  const seenIdle = useRef<boolean | null>(null);
  useEffect(() => {
    if (!cur) return;
    if (!liveNow) {
      seenIdle.current = true;
      return;
    }
    const was = seenIdle.current;
    seenIdle.current = false;
    if (!was || !nextTitle || !meeting || meeting.title_auto === false) return;
    const want = nextTitle;
    const id = meeting.id;
    setApplying(true);
    meetingApi
      .renameMeeting(id, want)
      .then(
        async () => {
          try {
            setCur(await meetingApi.current());
          } catch {
            /* the next poll */
          }
        },
        (e) => setError(t('meeting.title.rename_failed', { a: String((e as Error)?.message ?? e) })),
      )
      .finally(() => {
        setNextTitle(null);
        setApplying(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveNow, meeting?.id, !!cur]);
  // the title before the meeting: the name you gave the next one; until you give one, what it always showed (the last
  // meeting's name next to its 已结束, or the paper on the desk's). While your name is being handed to the meeting that
  // just started, still yours (no flash of the paper's name)
  const paperTitle = paperName ?? (cur?.paper?.exists ? cur.paper.name : null);
  const titleOverride = !liveNow || applying ? nextTitle : null;
  // the papers on the desk for the 新增 menu: the backend's list, else the one paper on screen
  const papers = useMemo(() => {
    const ps = cur?.papers ?? [];
    if (ps.length) return ps;
    const name = paperName ?? (cur?.paper?.exists ? cur.paper.name : null);
    return name ? [{ id: cur?.active_paper || cur?.paper?.id || 'current', name }] : [];
  }, [cur?.papers, cur?.paper, cur?.active_paper, paperName]);
  // 第 6 批: the comments typed on the paper (the host writes, replies, resolves and deletes while the meeting is on;
  // after it they stay on the paper to read), and the paper on screen's 历史版本
  const comments = useMemo(
    () => ({ items: cur?.comments ?? [], canAdd: state === 'live', canDelete: state === 'live' }),
    [cur?.comments, state],
  );
  const [histOpen, setHistOpen] = useState(false);
  const histPaper = useMemo(() => {
    const id = cur?.active_paper ?? null;
    const p = id ? papers.find((x) => x.id === id) : null;
    return id ? { id, name: p?.name ?? paperTitle ?? '' } : null;
  }, [cur?.active_paper, papers, paperTitle]);
  useEffect(() => {
    if (!active) setHistOpen(false);
  }, [active]);
  // dropping a paper = 新增: it joins the papers on the desk (the backend keeps the earlier ones) and comes up
  const canDrop = backend === 'up' && busy !== 'paper';
  const isFileDrag = (e: DragEvent) => [...e.dataTransfer.items].some((i) => i.kind === 'file');
  const dropProps = {
    onDragOver: (e: DragEvent) => {
      if (!canDrop || !isFileDrag(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      setOver(true);
    },
    onDragLeave: (e: DragEvent) => {
      if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) setOver(false);
    },
    onDrop: (e: DragEvent) => {
      setOver(false);
      if (!canDrop || !isFileDrag(e)) return;
      e.preventDefault();
      const files = e.dataTransfer.files;
      if (!files.length) return;
      const bridge = webview();
      if (bridge?.postMessageWithAdditionalObjects) bridge.postMessageWithAdditionalObjects('zuhui-drop-paper', files);
      else if (!inTauri) setError(t('meeting.paper.browser_only'));
      // inside Tauri without the bridge (Linux): onDragDropEvent above is the only way, when the window allows it
    },
  };

  // ---------------------------------------------------------------- start / stop
  const startBackend = async () => {
    setError(null);
    try {
      const ok = await meetingApi.backendStart();
      if (!ok) return setError(t('meeting.backend.tauri_only'));
      startedAt.current = Date.now();
      setBackend('starting');
    } catch (e) {
      setError(t('meeting.backend.start_failed', { a: String((e as Error)?.message ?? e) }));
    }
  };
  // the backend is started without asking: a few seconds without an answer and the app spawns it, then keeps
  // polling; it tries again every so often for as long as it stays down
  const tries = useRef(0);
  useEffect(() => {
    if (backend === 'up') {
      tries.current = 0;
      return;
    }
    if (backend !== 'down') return;
    const h = setTimeout(() => {
      tries.current += 1;
      void startBackend();
    }, tries.current === 0 ? 2500 : 8000);
    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backend]);
  const windowRegion = async (): Promise<[number, number, number, number] | null> => {
    try {
      const w = getCurrentWindow();
      const [pos, size] = await Promise.all([w.outerPosition(), w.outerSize()]);
      if (size.width < 50 || size.height < 50) return null;
      return [pos.x, pos.y, size.width, size.height];
    } catch {
      return null;
    }
  };
  const startMeeting = async () => {
    if (!inTauri) return setError(t('meeting.backend.tauri_only'));
    setBusy('start');
    setError(null);
    try {
      await meetingApi.recordStart(await windowRegion());
      setWaitLive(Date.now());
    } catch (e) {
      const s = String(e);
      if (s !== 'cancelled') setError(t('meeting.start_failed', { a: s }));
    } finally {
      setBusy(null);
    }
  };
  const endMeeting = async () => {
    if (!inTauri) return setError(t('meeting.backend.tauri_only'));
    setBusy('stop');
    setError(null);
    try {
      await meetingApi.recordStop();
    } catch (e) {
      setError(t('meeting.stop_failed', { a: String(e) }));
    } finally {
      setBusy(null);
    }
  };

  // in the meeting (第 3 批): the box to type to the AI, 标记, the answer only you see, the passage selected in the
  // paper, the quiet reminder. Kept after the meeting ends (switched off): nothing moves then
  const onChanged = useCallback(() => kick.current(), []);
  const onToolError = useCallback((msg: string) => setError(msg), []);
  const endRef = useRef<() => void>(() => {});
  endRef.current = () => void endMeeting();
  const onQuietEnd = useCallback(() => endRef.current(), []);
  const tools = useMemo<LiveTools>(
    () => ({
      role: 'host',
      live: state === 'live',
      anchor: cur?.anchor ?? null,
      quietS: cur?.quiet_s ?? null,
      onError: onToolError,
      onEnd: onQuietEnd,
      onChanged,
    }),
    [state, cur?.anchor, cur?.quiet_s, onToolError, onQuietEnd, onChanged],
  );

  // no backend yet: nothing to click — the app starts it by itself (the effect above) and this page waits
  if (backend === 'down' || backend === 'starting' || (backend === 'unknown' && !cur)) {
    return (
      <div className="mt-root mt-down" data-testid="meeting-down">
        <div className="mt-down-card">
          <span className="mw-spinner" aria-hidden="true" />
          <h2>{t('meeting.backend.checking')}</h2>
        </div>
      </div>
    );
  }

  const live = state === 'live';
  const notice = error ? (
    <span className="mt-notice-err">
      <Icon name="warning" size={13} />
      {error}
      <button className="link" onClick={() => setError(null)}>{t('meeting.notice.dismiss')}</button>
    </span>
  ) : null; // the student's last reply is already in the transcript: no second copy up here

  if (view === 'lobby' && !live) {
    return (
      <>
      {/* 历史版本 opens from the lobby's paper too: between meetings is when you'd look back or restore */}
      {histOpen && histPaper && active && (
        <PaperHistory paper={histPaper.id} name={histPaper.name} onClose={() => setHistOpen(false)} onToast={onToast} />
      )}
      <MeetingLobby
        onHistory={histPaper ? () => setHistOpen(true) : null}
        cur={cur}
        paperName={paperName}
        busy={busy ?? (waitLive ? 'start' : null)}
        importText={importText}
        notice={notice}
        title={lobbyTitle}
        onTitle={(v) => {
          setLobbyTitle(v);
          void renameNext(v);
        }}
        onStart={() => void startMeeting()}
        onPickPaper={() => void (busy !== 'paper' && changePaper())}
        dropProps={dropProps}
        over={over}
        active={active}
      />
      </>
    );
  }
  const endBtn = (ghost: boolean) => (
    <button className="mt-btn is-end" onClick={ghost ? undefined : endMeeting} disabled={!ghost && busy === 'stop'} tabIndex={ghost ? -1 : undefined} data-testid={ghost ? undefined : 'meeting-end'}>
      <Icon name="stop-fill" size={13} />
      {t('meeting.action.end')}
    </button>
  );
  // 散会 during the meeting; after it, 新会议 goes back to the lobby (starting is the lobby's, where what a meeting needs
  // is checked), each at its own width at the bar's right end, 16 px after 邀请 like every other gap
  const newBtn = (
    <button className="mt-btn is-new" onClick={() => setView('lobby')} data-testid="meeting-new">
      <Icon name="plus" size={13} />
      {t('lobby.new_short')}
    </button>
  );
  const actions = <span className="mt-acts-slot">{live ? endBtn(false) : newBtn}</span>;

  const dropZone = (
    <div className={`mt-drop ${over ? 'is-over' : ''} ${busy === 'paper' ? 'is-busy' : ''}`} {...dropProps} data-testid="paper-drop">
      {busy === 'paper' ? <span className="mw-spinner" aria-hidden="true" /> : <Icon name="document" size={40} />}
      <h2>{t(busy === 'paper' ? 'meeting.paper.importing' : 'meeting.paper.drop')}</h2>
      {busy === 'paper' && importText && <p className="mt-drop-progress">{importText}</p>}
    </div>
  );

  return (
    <div className={`mt-host ${over && hasPaper ? 'is-drop-over' : ''}`} {...(hasPaper ? dropProps : {})}>
      <MeetingLayout
        meeting={meeting}
        titlePrefix={paperTitle}
        titleOverride={titleOverride}
        invite
        onRename={!live && hasPaper && !applying ? renameNext : null}
        papers={hasPaper ? papers : null}
        activePaper={cur?.active_paper ?? papers[0]?.id ?? null}
        onSwitchPaper={busy !== 'paper' && papers.length > 1 ? (id: string) => void switchPaper(id) : null}
        onAddPaper={hasPaper ? () => void (busy !== 'paper' && changePaper()) : null}
        transcript={cur?.transcript ?? NO_LINES}
        partial={cur?.partial ?? null}
        people={cur?.participants ?? null}
        suggest={cur?.role_suggest ?? null}
        onRole={onRole}
        board={cur?.board ?? null}
        brain={live ? brainNow : null}
        active={active}
        summary={cur?.summary ?? null}
        diffSrc={hasPaper ? meetingApi.liveDiffUrl() : ''}
        live={live}
        status={cur?.ready ?? null}
        actions={actions}
        notice={notice}
        left={hasPaper ? undefined : dropZone}
        tools={meeting ? tools : null}
        bookmarks={cur?.bookmarks ?? null}
        comments={hasPaper ? comments : null}
        onHistory={hasPaper && histPaper ? () => setHistOpen(true) : null}
      />
      {over && hasPaper && (
        <div className="mt-drop-veil" aria-hidden="true">
          <Icon name="document" size={28} />
          {t(live ? 'meeting.paper.drop_add' : 'meeting.paper.drop_new')}
        </div>
      )}
      {histOpen && histPaper && active && (
        <PaperHistory paper={histPaper.id} name={histPaper.name} onClose={() => setHistOpen(false)} onToast={onToast} />
      )}
    </div>
  );
}
