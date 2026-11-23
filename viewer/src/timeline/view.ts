/**
 * The shared time view: one zoom/pan window and one time cursor for every
 * track (engine timeline, request Gantt, …), so cause and effect line up
 * under the pointer. App owns the state; tracks get a `TimeView` and use
 * `usePanZoom` for identical pointer/keyboard behavior.
 */
import { useEffect, useRef } from 'react'
import type { KeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react'

/** Every track uses the same gutter and right margin so x positions align. */
export const GUTTER = 156
export const RIGHT = 12
export const MIN_VIEW_S = 0.5

export interface View {
  /** Seconds since trace start. */
  t0: number
  t1: number
}

export interface TimeView {
  view: View
  full: View
  setView: (v: View | ((v: View) => View)) => void
  /** Shared cursor time (seconds since trace start), null when off-plot. */
  hoverT: number | null
  setHoverT: (t: number | null) => void
}

export function clamp(v: View, full: View): View {
  const span = Math.min(v.t1 - v.t0, full.t1 - full.t0)
  const t0 = Math.min(Math.max(v.t0, full.t0), full.t1 - span)
  return { t0, t1: t0 + span }
}

export function zoom(v: View, full: View, at: number, factor: number): View {
  const span = Math.min(Math.max((v.t1 - v.t0) * factor, MIN_VIEW_S), full.t1 - full.t0)
  const frac = (at - v.t0) / (v.t1 - v.t0)
  return clamp({ t0: at - frac * span, t1: at - frac * span + span }, full)
}

/** A view framing [t0, t1] with some context on both sides. */
export function frame(t0: number, t1: number, full: View): View {
  const pad = Math.max((t1 - t0) * 0.25, MIN_VIEW_S)
  return clamp({ t0: t0 - pad, t1: t1 + pad }, full)
}

export const xOf = (t: number, view: View, plotW: number) =>
  GUTTER + ((t - view.t0) / (view.t1 - view.t0)) * plotW
export const tOf = (x: number, view: View, plotW: number) =>
  view.t0 + ((x - GUTTER) / plotW) * (view.t1 - view.t0)

/**
 * Scroll zooms around the pointer, drag pans, double-click resets; with the
 * track focused, arrows pan, +/- zoom, 0 resets. A drag shorter than a few
 * pixels is reported to `onClick` instead (so tracks can select marks).
 */
export function usePanZoom(
  canvas: RefObject<HTMLCanvasElement | null>,
  plotW: number,
  tv: TimeView,
  onClick?: (x: number, y: number) => void,
) {
  const drag = useRef<{ x: number; view: View; moved: boolean } | null>(null)
  const { full, setView, setHoverT } = tv

  // Wheel must be non-passive to stop the page scrolling while zooming.
  useEffect(() => {
    const el = canvas.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      const rect = el.getBoundingClientRect()
      const x = e.clientX - rect.left
      if (x < GUTTER || plotW <= 0) return
      e.preventDefault()
      setView((v) => {
        const at = tOf(x, v, plotW)
        const next = zoom(v, full, at, Math.exp(e.deltaY * 0.002))
        // Wheel doesn't fire pointermove; keep the cursor under the pointer.
        setHoverT(x <= GUTTER + plotW ? tOf(x, next, plotW) : null)
        return next
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [canvas, full, plotW, setView, setHoverT])

  const local = (e: ReactPointerEvent) => {
    const r = e.currentTarget.getBoundingClientRect()
    return [e.clientX - r.left, e.clientY - r.top] as const
  }

  return {
    dragging: drag.current?.moved ?? false,
    handlers: {
      onPointerMove(e: ReactPointerEvent<HTMLCanvasElement>) {
        const [x] = local(e)
        const d = drag.current
        if (d) {
          if (Math.abs(d.x - x) > 3) d.moved = true
          if (d.moved) {
            const dt = ((d.x - x) / plotW) * (d.view.t1 - d.view.t0)
            setView(clamp({ t0: d.view.t0 + dt, t1: d.view.t1 + dt }, full))
          }
        }
        // While dragging, the grabbed instant stays under the pointer.
        const t = d?.moved ? tOf(d.x, d.view, plotW) : tOf(x, tv.view, plotW)
        setHoverT(x >= GUTTER && x <= GUTTER + plotW ? t : null)
      },
      onPointerLeave() {
        setHoverT(null)
        drag.current = null
      },
      onPointerDown(e: ReactPointerEvent<HTMLCanvasElement>) {
        e.currentTarget.setPointerCapture(e.pointerId)
        drag.current = { x: local(e)[0], view: tv.view, moved: false }
      },
      onPointerUp(e: ReactPointerEvent<HTMLCanvasElement>) {
        const d = drag.current
        drag.current = null
        if (d && !d.moved && onClick) {
          const [x, y] = local(e)
          onClick(x, y)
        }
      },
      onDoubleClick() {
        setView(full)
      },
    },
    onKeyDown(e: KeyboardEvent) {
      const { view } = tv
      const span = view.t1 - view.t0
      const mid = (view.t0 + view.t1) / 2
      const pan = (d: number) => setView(clamp({ t0: view.t0 + d, t1: view.t1 + d }, full))
      if (e.key === 'ArrowLeft') pan(-span * 0.1)
      else if (e.key === 'ArrowRight') pan(span * 0.1)
      else if (e.key === '+' || e.key === '=') setView(zoom(view, full, mid, 0.8))
      else if (e.key === '-') setView(zoom(view, full, mid, 1.25))
      else if (e.key === '0') setView(full)
      else return
      e.preventDefault()
    },
  }
}
