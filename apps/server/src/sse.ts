import type { Job } from "@pdf-converter/shared";
import type { JobStore } from "./db.ts";

export interface HubSnapshot {
  /** non-terminal jobs, keyed by id */
  active: Map<string, Job>;
  /** jobs that reached a terminal state since the previous tick */
  completed: Map<string, Job>;
}

type Subscriber = (snap: HubSnapshot) => void;

/**
 * Single in-process ticker: reads all active jobs ONCE per interval and fans the
 * snapshot out to every SSE subscriber — instead of each connection polling the
 * DB independently (adversarial-review M1). Detects just-completed jobs by diffing
 * against the previous tick so subscribers get a final event before the stream closes.
 */
export class ProgressHub {
  private readonly store: JobStore;
  private readonly intervalMs: number;
  private readonly subs = new Set<Subscriber>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private prevIds = new Set<string>();

  constructor(store: JobStore, intervalMs = 500) {
    this.store = store;
    this.intervalMs = intervalMs;
  }

  private tick(): void {
    const active = new Map<string, Job>();
    for (const j of this.store.activeJobs()) active.set(j.id, j);
    const completed = new Map<string, Job>();
    for (const id of this.prevIds) {
      if (!active.has(id)) {
        const j = this.store.get(id);
        if (j) completed.set(id, j);
      }
    }
    this.prevIds = new Set(active.keys());
    const snap: HubSnapshot = { active, completed };
    for (const s of this.subs) s(snap);
  }

  subscribe(cb: Subscriber): () => void {
    this.subs.add(cb);
    if (!this.timer) {
      this.timer = setInterval(() => this.tick(), this.intervalMs);
      if (typeof this.timer.unref === "function") this.timer.unref();
    }
    return () => {
      this.subs.delete(cb);
      if (this.subs.size === 0 && this.timer) {
        clearInterval(this.timer);
        this.timer = null;
        this.prevIds.clear();
      }
    };
  }

  get subscriberCount(): number {
    return this.subs.size;
  }
}
