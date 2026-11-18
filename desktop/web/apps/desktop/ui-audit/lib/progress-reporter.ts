// Reports the run to the owner's progress window (~/.claude/progress/ui_audit_run.json, the same format as
// claude_progress.py): one tick per finished test. Never throws into the run.
import { mkdirSync, renameSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type { FullResult, Reporter, Suite, TestCase, TestResult } from '@playwright/test/reporter';

const DIR = join(homedir(), '.claude', 'progress');
const TASK = process.env.UI_AUDIT_TASK || 'ui_audit_run';

export default class ProgressReporter implements Reporter {
  private total = 0;
  private current = 0;
  private started = Date.now() / 1000;
  private note = '';
  private last = 0;

  constructor() {
    // at once, before Vite and the warm-up start: a watchdog must never read the last run's "done"
    this.note = 'starting the dev server';
    this.write('running', true);
  }

  private write(status: string, force = false) {
    const now = Date.now() / 1000;
    if (!force && now - this.last < 2) return;
    this.last = now;
    try {
      mkdirSync(DIR, { recursive: true });
      const path = join(DIR, `${TASK}.json`);
      const data = { task: TASK, current: this.current, total: this.total, status, note: this.note, started_at: this.started, updated_at: now, pid: process.pid };
      writeFileSync(path + '.tmp', JSON.stringify(data), 'utf8');
      renameSync(path + '.tmp', path);
    } catch {
      /* the progress window is a nicety */
    }
  }
  onBegin(_config: unknown, suite: Suite) {
    this.total = suite.allTests().length;
    this.note = 'starting';
    this.write('running', true);
  }
  onTestBegin(test: TestCase) {
    this.note = test.title;
    this.write('running');
  }
  onTestEnd(test: TestCase, result: TestResult) {
    this.current += 1;
    this.note = `${test.title}: ${result.status}`;
    this.write('running');
  }
  onEnd(result: FullResult) {
    this.note = `finished: ${result.status}`;
    this.write(result.status === 'interrupted' || result.status === 'timedout' ? 'error' : 'done', true);
  }
  printsToStdio() {
    return false;
  }
}
