import { describe, expect, it } from 'vitest'
import { columns, lowerBound, niceScale, rates, stateAt, type SeriesData } from './series'

function data(points: [number, number, number, number, number][], evictions: [number, number][] = []): SeriesData {
  const f = (k: number) => Float64Array.from(points.map((p) => p[k]))
  return {
    snapT: f(0),
    running: f(1),
    waiting: f(2),
    kv: f(3),
    preempted: f(4),
    evictT: Float64Array.from(evictions.map((e) => e[0])),
    evictN: Float64Array.from(evictions.map((e) => e[1])),
    span: points.at(-1)?.[0] ?? 0,
  }
}

describe('columns', () => {
  it('takes the peak per column and sums events', () => {
    const d = data([
      [0.1, 2, 0, 0.2, 0],
      [0.4, 5, 1, 0.9, 2],
      [1.5, 3, 0, 0.5, 1],
    ])
    const c = columns(d, 0, 2, 2)
    expect(Array.from(c.running)).toEqual([5, 3])
    expect(Array.from(c.kv)).toEqual([0.9, 0.5])
    expect(Array.from(c.preempted)).toEqual([2, 1])
  })

  it('carries state through empty columns and from before the view', () => {
    const d = data([
      [0.5, 4, 1, 0.3, 0],
      [3.5, 6, 0, 0.6, 0],
    ])
    const c = columns(d, 1, 4, 3) // columns [1,2) [2,3) [3,4)
    expect(Array.from(c.running)).toEqual([4, 4, 6])
  })

  it('is NaN before the first snapshot', () => {
    const c = columns(data([[2.5, 1, 0, 0.1, 0]]), 0, 3, 3)
    expect(Number.isNaN(c.running[0])).toBe(true)
    expect(c.running[2]).toBe(1)
  })

  it('bins evictions in view only', () => {
    const d = data([[0, 1, 0, 0, 0]], [
      [0.5, 3],
      [1.2, 4],
      [5.0, 9],
    ])
    expect(Array.from(columns(d, 0, 2, 2).evicted)).toEqual([3, 4])
  })
})

describe('rates', () => {
  it('groups columns and converts to per-second', () => {
    // 4 columns of 0.5 s, groups of 2 => 1 s per group.
    expect(Array.from(rates(Float64Array.from([1, 3, 0, 2]), 2, 0.5))).toEqual([4, 2])
  })
})

describe('lookup helpers', () => {
  it('stateAt returns the last snapshot at or before t', () => {
    const d = data([
      [1, 2, 0, 0.1, 0],
      [2, 7, 3, 0.8, 0],
    ])
    expect(stateAt(d, 0.5)).toBeNull()
    expect(stateAt(d, 1.9)?.running).toBe(2)
    expect(stateAt(d, 2)?.running).toBe(7)
  })

  it('lowerBound and niceScale', () => {
    expect(lowerBound(Float64Array.from([1, 2, 2, 3]), 2)).toBe(1)
    expect(niceScale(53)).toEqual({ max: 60, step: 20 })
    expect(niceScale(0)).toEqual({ max: 1, step: 1 })
    expect(niceScale(1)).toEqual({ max: 1, step: 0.5 })
  })
})
