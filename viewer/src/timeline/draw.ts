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
