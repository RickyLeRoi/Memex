import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { JobState } from "./types";

const POLL_MS = 1000;

export function useJob(onFinished?: () => void) {
  const [job, setJob] = useState<JobState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const track = useCallback((id: string) => {
    setError(null);
    const timer = window.setInterval(async () => {
      try {
        const state = await api.job(id);
        setJob(state);
        if (state.status !== "running") {
          window.clearInterval(timer);
          finishedRef.current?.();
        }
      } catch (e) {
        window.clearInterval(timer);
        setError((e as Error).message);
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, []);

  const start = useCallback(
    async (launch: () => Promise<{ job: string }>) => {
      setError(null);
      try {
        const { job: id } = await launch();
        setJob({ id, sources: [], status: "running", exit_code: null, started_at: "", finished_at: null, log: [] });
        track(id);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [track],
  );

  useEffect(() => {
    let stop: (() => void) | undefined;
    api.runningJob().then(({ running }) => {
      if (running) {
        setJob(running);
        stop = track(running.id);
      }
    }).catch(() => undefined);
    return () => stop?.();
  }, [track]);

  return { job, error, start, busy: job?.status === "running" };
}
