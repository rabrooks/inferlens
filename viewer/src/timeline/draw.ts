/** Canvas helpers shared by every track on the time axis. */
import { niceScale } from './series'
import { rgba, type Palette } from './theme'
import { GUTTER, type View } from './view'

export const DATA_FONT = '10px ui-monospace, SFMono-Regular, Menlo, monospace'

/** Hairline time gridlines from `top` to `bottom`, labels below `bottom`. */
export function timeAxis(
  ctx: CanvasRenderingContext2D,
  p: Palette,
  view: View,
  plotW: number,
  top: number,
  bottom: number,
) {
  const span = view.t1 - view.t0
  const { step } = niceScale(span, Math.max(2, Math.floor(plotW / 110)))
  ctx.font = DATA_FONT
  ctx.textAlign = 'center'
  ctx.textBaseline = 'top'
  const first = Math.ceil(view.t0 / step) * step
  for (let t = first; t <= view.t1 + 1e-9; t += step) {
    const x = Math.round(GUTTER + ((t - view.t0) / span) * plotW) + 0.5
    ctx.strokeStyle = rgba(p.grid)
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(x, top)
    ctx.lineTo(x, bottom)
    ctx.stroke()
    ctx.fillStyle = rgba(p['ink-muted'])
    const digits = step < 1 ? Math.min(3, Math.ceil(-Math.log10(step))) : 0
    ctx.fillText(`${t.toFixed(digits)} s`, x, bottom + 8)
  }
  ctx.textAlign = 'left'
}

export function maxOf(a: Float64Array): number {
  let m = 0
  for (const v of a) if (v > m) m = v
  return m
}

export function fmtRate(v: number): string {
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10_000 ? 0 : 1)}K`
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

export function axis(
  ctx: CanvasRenderingContext2D,
  p: Palette,
  top: number,
  h: number,
  s: { max: number; step: number },
  fmt: (v: number) => string,
) {
  ctx.font = DATA_FONT
  ctx.textAlign = 'right'
  ctx.textBaseline = 'middle'
  for (let v = 0; v <= s.max + 1e-9; v += s.step) {
    const y = Math.round(top + h - (v / s.max) * h) + 0.5
    ctx.strokeStyle = rgba(v === 0 ? p.axis : p.grid)
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.moveTo(GUTTER, y)
    ctx.lineTo(ctx.canvas.width, y)
    ctx.stroke()
    ctx.fillStyle = rgba(p['ink-muted'])
    ctx.fillText(fmt(v), GUTTER - 6, y)
  }
  ctx.textAlign = 'left'
}

/** 2px line over a ~10% wash, NaN columns left blank (no data yet). */
export function area(
  ctx: CanvasRenderingContext2D,
  values: Float64Array,
  max: number,
  top: number,
  h: number,
  color: [number, number, number],
) {
  const y = (v: number) => top + h - (Math.min(v, max) / max) * h
  let run: [number, number][] = []
  const flush = () => {
    if (run.length === 0) return
    ctx.beginPath()
    ctx.moveTo(run[0][0], top + h)
    for (const [px, py] of run) ctx.lineTo(px, py)
    ctx.lineTo(run.at(-1)![0], top + h)
    ctx.closePath()
    ctx.fillStyle = rgba(color, 0.1)
    ctx.fill()
    ctx.beginPath()
    run.forEach(([px, py], i) => (i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py)))
    ctx.strokeStyle = rgba(color)
    ctx.lineWidth = 2
    ctx.lineJoin = 'round'
    ctx.lineCap = 'round'
    ctx.stroke()
    run = []
  }
  for (let c = 0; c < values.length; c++) {
    const v = values[c]
    if (Number.isNaN(v)) flush()
    else run.push([GUTTER + c + 0.5, y(v)])
  }
  flush()
}

export function roundTop(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.min(r, w / 2, h)
  ctx.beginPath()
  ctx.moveTo(x, y + h)
  ctx.lineTo(x, y + rr)
  ctx.arcTo(x, y, x + rr, y, rr)
  ctx.lineTo(x + w - rr, y)
  ctx.arcTo(x + w, y, x + w, y + rr, rr)
  ctx.lineTo(x + w, y + h)
  ctx.closePath()
  ctx.fill()
}
