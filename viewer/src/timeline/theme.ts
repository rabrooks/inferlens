/**
 * Design tokens resolved for canvas drawing.
 *
 * Canvas can't use CSS variables, and `getPropertyValue('--il-kv')` returns
 * the *unresolved* `light-dark(...)` text. Assigning the token to a probe
 * element's `color` makes the browser resolve it for the active
 * `color-scheme`, so canvas colors always match the CSS ones
 * (viewer/DESIGN.md: never hard-code a hex in a render loop).
 */
import { useEffect, useState } from 'react'

const TOKENS = [
  'surface',
  'ink',
  'ink-secondary',
  'ink-muted',
  'grid',
  'axis',
  'running',
  'waiting',
  'kv',
  'preempt',
  'status-warning',
  'status-critical',
] as const

export type Token = (typeof TOKENS)[number]
export type Palette = Record<Token, [number, number, number]>

function resolve(): Palette {
  const probe = document.createElement('span')
  probe.style.display = 'none'
  document.body.appendChild(probe)
  const out = {} as Palette
  for (const name of TOKENS) {
    probe.style.color = `var(--il-${name})`
    out[name] = parseRgb(getComputedStyle(probe).color)
  }
  probe.remove()
  return out
}

/** Parse the computed `rgb()`/`rgba()`/`color(srgb …)` forms browsers return. */
export function parseRgb(css: string): [number, number, number] {
  const srgb = css.match(/color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/)
  if (srgb) return [0, 1, 2].map((i) => Math.round(Number(srgb[i + 1]) * 255)) as [number, number, number]
  const nums = css.match(/[\d.]+/g)?.map(Number) ?? [0, 0, 0]
  return [nums[0] ?? 0, nums[1] ?? 0, nums[2] ?? 0]
}

export function rgba([r, g, b]: [number, number, number], alpha = 1): string {
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** Resolved palette, re-read whenever the theme or OS scheme changes. */
export function usePalette(): Palette | null {
  const [palette, setPalette] = useState<Palette | null>(null)
  useEffect(() => {
    const update = () => setPalette(resolve())
    update()
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    media.addEventListener('change', update)
    return () => {
      observer.disconnect()
      media.removeEventListener('change', update)
    }
  }, [])
  return palette
}
