import type { HealthResponse } from "@pdf-ocr/shared";
import { useQuery } from "@tanstack/react-query";

export function App() {
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: async (): Promise<HealthResponse> => {
      const res = await fetch("/api/v1/health");
      if (!res.ok) throw new Error(`health ${res.status}`);
      return (await res.json()) as HealthResponse;
    },
    refetchInterval: 5000,
  });

  const worker = isError ? "unreachable" : data ? (data.worker.alive ? "alive" : "down") : "…";

  return (
    <main className="app">
      <h1>PDF OCR</h1>
      <p>Vite+ · React · TypeScript scaffold.</p>
      <p>
        Worker: <strong>{worker}</strong>
        {data ? ` · queue depth ${data.queueDepth}` : ""}
      </p>
    </main>
  );
}
