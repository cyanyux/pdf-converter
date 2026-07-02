import { isTerminal, type Job, type Locale, type Mode } from "@pdf-ocr/shared";
import { useCallback, useEffect, useRef, useState } from "react";
import { cancelJob, createJobs, deleteJob, getJob } from "./api.ts";

const STORAGE_KEY = "pdfOcrJobs";

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

export type ToastKind = "done" | "error";

/**
 * Job store: submit, poll non-terminal jobs once per second, persist ids across
 * reloads (rehydrating from the durable server store), and fire a toast on
 * terminal transitions. Mirrors the legacy polling behavior in idiomatic React.
 */
export function useJobStore(locale: Locale, onToast: (kind: ToastKind, job: Job) => void) {
  const [byId, setById] = useState<Record<string, Job>>({});
  const [order, setOrder] = useState<string[]>([]);
  const byIdRef = useRef(byId);
  byIdRef.current = byId;
  const lastStatus = useRef<Record<string, string>>({});
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

  // Poll non-terminal jobs.
  useEffect(() => {
    const tick = async () => {
      const active = Object.values(byIdRef.current).filter((j) => !isTerminal(j.status));
      if (active.length === 0) return;
      const hidden = document.hidden;
      await Promise.all(
        active.map(async (j) => {
          try {
            const fresh = await getJob(j.id);
            if (fresh === null) {
              setById((p) => {
                const n = { ...p };
                delete n[j.id];
                return n;
              });
              setOrder((p) => p.filter((x) => x !== j.id));
              return;
            }
            setById((p) => ({ ...p, [j.id]: fresh }));
            if (isTerminal(fresh.status) && lastStatus.current[j.id] !== fresh.status) {
              lastStatus.current[j.id] = fresh.status;
              if (fresh.status === "done") onToastRef.current("done", fresh);
              else if (fresh.status === "error") onToastRef.current("error", fresh);
            }
          } catch {
            /* transient network error; retry next tick */
          }
        }),
      );
      void hidden;
    };
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
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
      return resp;
    },
    [locale],
  );

  const cancel = useCallback(async (id: string) => {
    await cancelJob(id);
    setById((p) => (p[id] ? { ...p, [id]: { ...p[id], status: "cancelled" } } : p));
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
  return { jobs, submit, cancel, remove };
}
