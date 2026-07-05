import { createContext, type ReactNode, useCallback, useContext, useRef, useState } from "react";

interface ToastItem {
  id: number;
  message: string;
}

const ToastContext = createContext<(message: string) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);
  const show = useCallback((message: string) => {
    const id = ++seq.current;
    setToasts((t) => [...t, { id, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3000);
  }, []);
  return (
    <ToastContext value={show}>
      {children}
      <div className="toast-container">
        {toasts.map((t) => (
          <div key={t.id} className="toast show">
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext>
  );
}

export function useToast(): (message: string) => void {
  return useContext(ToastContext);
}
