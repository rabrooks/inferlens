/** Number formats shared with `inferlens explain` text output. */

/** Seconds with millisecond precision, as explain prints phase times. */
export function fmtS(s: number): string {
  return `${s.toFixed(3)} s`
}

/** explain's `_short`: long ids keep their head and distinctive tail. */
export function shortId(id: string): string {
  return id.length <= 26 ? id : `${id.slice(0, 12)}…${id.slice(-13)}`
}

/** Share of e2e as a whole percent (explain's `{:4.0f}%`). */
export function pct(part: number, whole: number): string {
  return whole > 0 ? `${Math.round((part / whole) * 100)}%` : '–'
}
