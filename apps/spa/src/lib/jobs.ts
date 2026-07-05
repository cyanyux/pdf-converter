import { isTerminal, type Job, type Locale, type Mode } from "@pdf-converter/shared";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, cancelJob, createJobs, deleteJob, getJob } from "./api.ts";

const STORAGE_KEY = "pdfConverterJobs";

interface Persisted {
  id: string;
  mode: Mode;
  filename: string;
  locale: Locale;
}

function stub(p: Persisted): Job {
  const now = Date.now() / 1000;
  return {
    id: p.id,
    groupId: null,
    mode: p.mode,
    filename: p.filename,
    locale: p.locale,
    // Optimistic placeholder overwritten by the first real GET; the requested engine
    // isn't persisted client-side, so 'auto' stands in for this transient stub.
    engine: "auto",
    status: "queued",
    attempts: 0,
    createdAt: now,
    updatedAt: now,
    heartbeatAt: null,
    progress: null,
    result: null,
    error: null,
  };
}

export type ToastKind = "done" | "error" | "cancelled";

// Base poll interval, and the exponential-backoff ceiling applied after
// consecutive tick failures (1s → 2s → 4s … → 30s), reset on the first success.
const POLL_MS = 1000;
const MAX_POLL_MS = 30_000;
// Consecutive failed ticks before we surface a "connection lost" banner.
const RECONNECT_THRESHOLD = 3;
// A 401/403 is an auth problem, not an outage: the connection is fine, so we
// don't spin the exponential backoff to its ceiling — we retry on a slow fixed
// cadence (key rotation needs a page reload anyway) and show a distinct banner.
const AUTH_RETRY_MS = 30_000;

/**
 * Job store: submit, poll non-terminal jobs once per second, persist ids across
 * reloads (rehydrating from the durable server store), and fire a toast on
 * terminal transitions. Mirrors the legacy polling behavior in idiomatic React.
 *
 * On a sustained polling failure the loop backs off exponentially (capped at
 * MAX_POLL_MS) and, after RECONNECT_THRESHOLD consecutive failures, exposes a
 * `reconnecting` flag so the UI can tell the user the server is unreachable; both
 * reset the moment a tick succeeds.
 */
export function useJobStore(locale: Locale, onToast: (kind: ToastKind, job: Job) => void) {
  const [byId, setById] = useState<Record<string, Job>>({});
  const [order, setOrder] = useState<string[]>([]);
  const [reconnecting, setReconnecting] = useState(false);
  // Distinct from `reconnecting`: the server is reachable but rejecting us with
  // 401/403 (API_KEY set or rotated mid-session). Surfaced as its own banner.
  const [authError, setAuthError] = useState(false);
  const byIdRef = useRef(byId);
  byIdRef.current = byId;
  const lastStatus = useRef<Record<string, string>>({});
  // Ids rehydrated from localStorage this session. Their first poll observation must
  // NOT fire a toast (they may already be terminal on the server from a prior
  // session) — only genuine in-session transitions should toast.
  const rehydrated = useRef<Set<string>>(new Set());
  const onToastRef = useRef(onToast);
  onToastRef.current = onToast;

  // Load persisted job ids once and rehydrate via polling.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const arr = JSON.parse(raw) as Persisted[];
      const rec: Record<string, Job> = {};
      const ord: string[] = [];
      for (const p of arr) {
        if (!p?.id) continue;
        rec[p.id] = stub(p);
        ord.push(p.id);
      }
      setById(rec);
      setOrder(ord);
      rehydrated.current = new Set(ord);
    } catch {
      /* ignore corrupt storage */
    }
  }, []);

  // Persist minimal job info whenever the set changes.
  useEffect(() => {
    const arr: Persisted[] = order
      .map((id) => byId[id])
      .filter((j): j is Job => Boolean(j))
      .map((j) => ({ id: j.id, mode: j.mode, filename: j.filename, locale: j.locale }));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
  }, [byId, order]);

  // Poll non-terminal jobs on a self-scheduling timer so the delay can grow with
  // backoff. `fails` counts consecutive failed ticks; a tick "fails" only when it
  // ran real fetches and every one of them threw (a persistent server/network
  // outage), so a single flaky job among healthy ones doesn't trip backoff.
  const inFlight = useRef(false);
  const fails = useRef(0);
  // True when the last real tick failed *only* with auth errors (401/403). Drives
  // a slow fixed retry instead of the outage backoff, and the distinct banner.
  const authFail = useRef(false);
  // Set by the polling effect; lets submit() force an immediate poll of a freshly
  // created job instead of waiting out a stale backoff timer.
  const kickRef = useRef<() => void>(() => {});
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;

    const tick = async () => {
      // Skip polling while backgrounded — the tab isn't visible, so there's no UI
      // to update and we avoid needless requests. Don't count this as a failure.
      if (document.hidden) return;
      // In-flight guard: if the previous tick's fetches haven't resolved, skip this
      // one so overlapping ticks can't race (e.g. a late getJob resurrecting a job
      // that was removed in the meantime).
      if (inFlight.current) return;
      const active = Object.values(byIdRef.current).filter((j) => !isTerminal(j.status));
      if (active.length === 0) {
        // Nothing to poll: an earlier outage may have left the streak elevated and
        // the banner lit, so clear both here — otherwise a fresh submit would start
        // at max backoff behind a stale "connection lost"/auth banner.
        fails.current = 0;
        authFail.current = false;
        setReconnecting(false);
        setAuthError(false);
        return;
      }
      inFlight.current = true;
      let ok = 0;
      let failed = 0;
      let authFailed = 0;
      try {
        await Promise.all(
          active.map(async (j) => {
            try {
              const fresh = await getJob(j.id);
              ok++;
              if (fresh === null) {
                setById((p) => {
                  const n = { ...p };
                  delete n[j.id];
                  return n;
                });
                setOrder((p) => p.filter((x) => x !== j.id));
                return;
              }
              // The job may have been removed while this fetch was in flight; don't
              // re-insert it.
              if (!(j.id in byIdRef.current)) return;
              setById((p) => (j.id in p ? { ...p, [j.id]: fresh } : p));
              const prev = lastStatus.current[j.id];
              lastStatus.current[j.id] = fresh.status;
              // First sight of a rehydrated job: seed lastStatus without toasting, so a
              // job that finished in a previous session doesn't re-toast on reload.
              const firstSightOfRehydrated = prev === undefined && rehydrated.current.has(j.id);
              if (isTerminal(fresh.status) && prev !== fresh.status && !firstSightOfRehydrated) {
                if (fresh.status === "done") onToastRef.current("done", fresh);
                else if (fresh.status === "error") onToastRef.current("error", fresh);
                else if (fresh.status === "cancelled") onToastRef.current("cancelled", fresh);
              }
            } catch (e) {
              // getJob threw. A 401/403 is an auth rejection (connection is fine),
              // tracked separately from a network/server outage; the job is left
              // untouched and retried on the next tick.
              if (e instanceof ApiError && (e.status === 401 || e.status === 403)) authFailed++;
              else failed++;
            }
          }),
        );
      } finally {
        inFlight.current = false;
      }
      // A tick counts as failed only when every fetch it issued threw. If all the
      // failures were auth rejections, surface the distinct auth banner and retry
      // slowly rather than treating it as an outage. Any success clears everything.
      if (ok === 0 && authFailed > 0 && failed === 0) {
        authFail.current = true;
        fails.current = 0;
        setAuthError(true);
        setReconnecting(false);
      } else if (ok === 0 && failed > 0) {
        authFail.current = false;
        fails.current += 1;
        setAuthError(false);
        if (fails.current >= RECONNECT_THRESHOLD) setReconnecting(true);
      } else {
        authFail.current = false;
        fails.current = 0;
        setReconnecting(false);
        setAuthError(false);
      }
    };

    const loop = async () => {
      if (stopped) return;
      await tick();
      if (stopped) return;
      // Auth failures retry on a slow fixed cadence; network failures back off
      // exponentially (1s, 2s, 4s … capped at 30s); success polls at the base rate.
      const delay = authFail.current
        ? AUTH_RETRY_MS
        : Math.min(POLL_MS * 2 ** fails.current, MAX_POLL_MS);
      timer = setTimeout(loop, delay);
    };

    // A recovered network/refocused tab (or a fresh submit) shouldn't wait out a
    // backed-off delay: reset the streak and poll immediately. If a tick is already
    // in flight, that tick will reschedule on its own — kicking here would leak an
    // orphaned timer racing loop()'s, so we just reset the streak and let it run.
    const kick = () => {
      if (stopped || document.hidden) return;
      fails.current = 0;
      authFail.current = false;
      if (inFlight.current) return;
      clearTimeout(timer);
      void loop();
    };
    const onVisibility = () => {
      if (!document.hidden) kick();
    };
    kickRef.current = kick;
    window.addEventListener("online", kick);
    window.addEventListener("focus", kick);
    document.addEventListener("visibilitychange", onVisibility);

    timer = setTimeout(loop, POLL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
      window.removeEventListener("online", kick);
      window.removeEventListener("focus", kick);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const submit = useCallback(
    async (files: File[], modes: Mode[]) => {
      const resp = await createJobs(files, modes, locale);
      const created = resp.jobs.map((j) =>
        stub({ id: j.id, mode: j.mode, filename: j.filename, locale }),
      );
      setById((p) => {
        const n = { ...p };
        for (const j of created) n[j.id] = j;
        return n;
      });
      setOrder((p) => [
        ...created.map((j) => j.id),
        ...p.filter((id) => !created.some((c) => c.id === id)),
      ]);
      // createJobs just succeeded, so the connection is healthy: clear any stale
      // outage/auth banners and poll the new job immediately rather than letting it
      // sit un-polled behind a backed-off timer.
      kickRef.current();
      return resp;
    },
    [locale],
  );

  const cancel = useCallback(async (id: string) => {
    await cancelJob(id);
    // Reflect the request optimistically but keep it NON-terminal so polling keeps
    // reconciling: the server sets 'cancel_requested' for a running job (only a
    // still-queued one becomes 'cancelled' outright), and a running job may still
    // finish 'done' past its last cancel checkpoint.
    setById((p) =>
      p[id] && !isTerminal(p[id].status)
        ? { ...p, [id]: { ...p[id], status: "cancel_requested" } }
        : p,
    );
  }, []);

  const remove = useCallback(async (id: string) => {
    setById((p) => {
      const n = { ...p };
      delete n[id];
      return n;
    });
    setOrder((p) => p.filter((x) => x !== id));
    try {
      await deleteJob(id);
    } catch {
      /* best-effort server cleanup */
    }
  }, []);

  const jobs = order.map((id) => byId[id]).filter((j): j is Job => Boolean(j));
  return { jobs, submit, cancel, remove, reconnecting, authError };
}
