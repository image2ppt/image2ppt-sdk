// Helpers shared by the test files.

import { vi } from "vitest";

export /** Run `fn`, capturing every delay handed to setTimeout instead of sleeping. */
async function withoutSleeping<T>(fn: () => Promise<T>): Promise<{ result: T; delays: number[] }> {
  const delays: number[] = [];
  const realSetTimeout = globalThis.setTimeout;
  const spy = vi
    .spyOn(globalThis, "setTimeout")
    .mockImplementation(((cb: () => void, ms?: number) => {
      delays.push(ms ?? 0);
      return realSetTimeout(cb, 0);
    }) as unknown as typeof setTimeout);
  try {
    return { result: await fn(), delays };
  } finally {
    spy.mockRestore();
  }
}
