/** Balances of other AI services, checked by the runner (see runner/riesz_runner/balances.py). */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";

export interface Balance {
  amount: number;
  unit: string;
}

export interface ServiceBalance {
  id: string;
  label: string;
  balances: Balance[];
  error: string | null;
}

export interface Balances {
  providers: ServiceBalance[];
  fetchedAt: number | null;
}

const SYMBOLS: Record<string, string> = { USD: "$", CNY: "¥" };

/** "$58.33", "-¥1.28", "19,826 Anlas" */
export function formatBalance(b: Balance): string {
  const symbol = SYMBOLS[b.unit];
  if (symbol) return `${b.amount < 0 ? "-" : ""}${symbol}${Math.abs(b.amount).toFixed(2)}`;
  return `${Math.round(b.amount).toLocaleString("en-US")} ${b.unit}`;
}

export function useBalances() {
  const [balances, setBalances] = useState<Balances | null>(null);
  const inflight = useRef(false);

  const load = useCallback(async (refresh = false) => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      setBalances(await api<Balances>("balances", { query: refresh ? { refresh: 1 } : {} }));
    } catch {
      /* keep the last value */
    } finally {
      inflight.current = false;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { balances, reload: load };
}
