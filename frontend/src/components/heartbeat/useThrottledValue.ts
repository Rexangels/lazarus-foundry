"use client";

import { useEffect, useRef, useState } from "react";

/**
 * `value`, updated at most once per `ms` (the latest value always lands). The terminal commits
 * new lines in batches with it, so the DOM and the polite live region are not flooded.
 */
export function useThrottledValue<T>(value: T, ms: number): T {
  const [shown, setShown] = useState(value);
  const last = useRef(0);
  useEffect(() => {
    const wait = Math.max(0, last.current + ms - Date.now());
    const timer = setTimeout(() => {
      last.current = Date.now();
      setShown(value);
    }, wait);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return shown;
}
