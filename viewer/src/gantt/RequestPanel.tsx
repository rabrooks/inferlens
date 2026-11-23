/**
 * Request sidebar: the selected request's breakdown (the UI twin of
 * `inferlens explain --request ID`) above a sortable request table (the
 * Gantt's tooltip-free twin, and how keyboard users select a request).
 */
import { useMemo, useState } from 'react'
import type { Incident } from '../analysis/incidents'
import type { RequestAnalysis, RequestAttr } from '../analysis/requests'
import { phaseSegments } from '../analysis/requests'
import { fmtS, pct } from './format'

export function RequestPanel({
  analysis,
  incidents,
  selected,
  onSelect,
  onFrame,
}: {
  analysis: RequestAnalysis
  incidents: Incident[]
  selected: RequestAttr | null
  onSelect: (r: RequestAttr | null) => void
  /** Zoom the shared view to [t0, t1] (seconds since trace start). */
  onFrame: (t0: number, t1: number) => void
}) {
  return (
    <aside className="flex flex-col gap-2">
      {selected ? (
        <RequestDetail
          r={selected}
          analysis={analysis}
          incidents={incidents}
          onClose={() => onSelect(null)}
          onFrame={onFrame}
        />
      ) : (
        <section className="rounded border border-edge bg-surface px-3 py-2 text-xs text-ink-secondary">
          Select a request in the Gantt or the table below to see where its time went.
        </section>
      )}
      <RequestTable analysis={analysis} selected={selected} onSelect={onSelect} />
    </aside>
  )
}

// Labels inside fills: white or near-black by the fill's luminance, which
// for queued/decode flips with the theme (the ramp is selected per theme).
const PHASE_INK = {
  unattributed: 'var(--il-ink)',
  queued: 'light-dark(#0b0b0b, #ffffff)',
  prefill: '#0b0b0b',
  decode: 'light-dark(#ffffff, #0b0b0b)',
} as const

const PHASE_COLOR = {
  unattributed: 'var(--il-ink-muted)',
  queued: 'var(--il-phase-queued)',
  prefill: 'var(--il-phase-prefill)',
  decode: 'var(--il-phase-decode)',
} as const

function RequestDetail({
  r,
  analysis,
  incidents,
  onClose,
  onFrame,
}: {
  r: RequestAttr
  analysis: RequestAnalysis
  incidents: Incident[]
  onClose: () => void
  onFrame: (t0: number, t1: number) => void
}) {
  // Time order, as the Gantt draws them: remainder at the head.
  const parts = [
    { name: 'unattributed', v: r.unattributedS },
    { name: 'queued', v: r.queuedS },
    { name: 'prefill', v: r.prefillS },
    { name: 'decode', v: r.decodeS },
  ] as const
  const segs = phaseSegments(r)
  const span = r.finishS - r.arrivalS
  const overlapped = r.incidents.map((id) => incidents[id - 1]).filter(Boolean)

  return (
    <section className="rounded border border-edge bg-surface" aria-label={`Request ${r.requestId}`}>
      <header className="flex items-start gap-2 border-b border-grid px-3 py-1.5">
        <div className="min-w-0">
          <h2 className="text-xs font-medium text-ink-secondary">Request</h2>
          <p className="font-data text-xs break-all text-ink">{r.requestId}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Clear selection"
          className="ml-auto rounded px-1.5 text-xs text-ink-secondary hover:bg-page"
        >
          ✕
        </button>
      </header>
      <div className="flex flex-col gap-2 px-3 py-2">
        <p className="flex items-baseline gap-2">
          <span className="text-2xl font-semibold text-ink">{r.e2eS.toFixed(3)} s</span>
          <span className="text-xs text-ink-secondary">end-to-end · {r.finishReason}</span>
        </p>
        <p className="font-data text-xs text-ink-secondary">
          arrived t={r.arrivalS.toFixed(2)} s · finished t={r.finishS.toFixed(2)} s
        </p>

        <div>
          <h3 className="mb-1 text-xs font-medium text-ink-secondary">Where the time went (as reported)</h3>
          {/* Tall segmented bar in time order: one whole (e2e), its parts. */}
          <div
            className="flex h-7 gap-0.5"
            role="img"
            aria-label={parts.map((p) => `${p.name} ${fmtS(p.v)} (${pct(p.v, r.e2eS)})`).join(', ')}
          >
            {segs.map((s) => {
              const w = span > 0 ? ((s.t1 - s.t0) / span) * 100 : 0
              const share = pct(s.t1 - s.t0, r.e2eS)
              return (
                <div
                  key={s.phase}
                  title={`${s.phase} ${fmtS(s.t1 - s.t0)}`}
                  className={`flex items-center rounded font-data text-[11px] font-semibold ${w >= 14 ? 'px-1' : ''}`}
                  style={{
                    width: `${w}%`,
                    minWidth: 2,
                    background: PHASE_COLOR[s.phase],
                    opacity: s.phase === 'unattributed' ? 0.5 : 1,
                    color: PHASE_INK[s.phase],
                  }}
                >
                  {/* Only print the share where it fits (~4ch at 11px). */}
                  {w >= 14 ? share : ''}
                </div>
              )
            })}
          </div>
          <dl className="mt-1.5 grid grid-cols-[1fr_auto_auto] items-baseline gap-x-3 gap-y-0.5 font-data text-xs tabular-nums">
            {parts.map((p) => (
              <div key={p.name} className="contents">
                <dt className="flex items-center gap-1.5 font-ui text-ink-secondary">
                  <span
                    className="inline-block h-2 w-3 rounded-sm"
                    style={{ background: PHASE_COLOR[p.name], opacity: p.name === 'unattributed' ? 0.5 : 1 }}
                  />
                  {p.name}
                </dt>
                <dd className="text-right font-semibold text-ink">{fmtS(p.v)}</dd>
                <dd className="text-right text-ink-secondary">{pct(p.v, r.e2eS)}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-1 text-[11px] leading-snug text-ink-muted">
            Unattributed = e2e − phases: frontend time and anything the engine doesn't assign to a phase.
          </p>
        </div>

        <ul className="flex flex-col gap-0.5 text-xs text-ink-secondary">
          <li>
            <span className="font-data text-ink">{r.promptTokens.toLocaleString()}</span> prompt tokens (
            <span className="font-data text-ink">{r.cachedTokens.toLocaleString()}</span> from prefix cache),{' '}
            <span className="font-data text-ink">{r.generationTokens.toLocaleString()}</span> generated
          </li>
          {r.numPreemptions !== null && (
            <li>
              preempted <span className="font-data text-ink">{r.numPreemptions}</span> time(s)
            </li>
          )}
          {r.queuedVsCalm !== null ? (
            <li>
              queue time <span className="font-data text-ink">{r.queuedVsCalm.toFixed(1)}×</span> the calm median
            </li>
          ) : (
            analysis.calmQueuedP50 !== null && (
              <li>
                queue time <span className="font-data text-ink">{fmtS(r.queuedS)}</span> vs a calm median of{' '}
                <span className="font-data text-ink">{fmtS(analysis.calmQueuedP50)}</span>
              </li>
            )
          )}
          {r.tpotVsCalm !== null && (
            <li>
              per-token decode time <span className="font-data text-ink">{r.tpotVsCalm.toFixed(1)}×</span> the calm
              median
            </li>
          )}
        </ul>

        <div>
          <h3 className="mb-1 text-xs font-medium text-ink-secondary">Engine during this request</h3>
          {overlapped.length === 0 ? (
            <p className="text-xs text-ink-secondary">Overlapped no incident.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {overlapped.map((inc) => (
                <li key={inc.id}>
                  <button
                    type="button"
                    onClick={() => onFrame(Math.min(inc.startS, r.arrivalS), Math.max(inc.endS, r.finishS))}
                    className="w-full rounded border border-edge px-2 py-1 text-left text-xs text-ink-secondary hover:bg-page"
                  >
                    <span className="font-medium text-ink">
                      Incident #{inc.id} · {inc.kind === 'kv_bound' ? 'KV-bound' : 'queue-bound'}
                    </span>
                    <span className="block font-data">
                      t={inc.startS.toFixed(0)}–{inc.endS.toFixed(0)} s · KV peak {Math.round(inc.kvUsageMax * 100)}% ·{' '}
                      {inc.preemptions} preemptions · waiting ≤{inc.waitingMax}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-1 text-[11px] leading-snug text-ink-muted">
            Linked by time overlap: the engine doesn't report which requests ran in which step.
          </p>
        </div>

        <button
          type="button"
          onClick={() => onFrame(r.arrivalS, r.finishS)}
          className="self-start rounded border border-edge px-2 py-0.5 text-xs text-ink-secondary hover:bg-page"
        >
          Zoom to request
        </button>
      </div>
    </section>
  )
}

type SortKey = 'e2eS' | 'queuedS' | 'prefillS' | 'decodeS' | 'unattributedS'

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: 'e2eS', label: 'e2e' },
  { key: 'queuedS', label: 'queued' },
  { key: 'prefillS', label: 'prefill' },
  { key: 'decodeS', label: 'decode' },
  { key: 'unattributedS', label: 'unattr.' },
]

const PAGE = 50

function RequestTable({
  analysis,
  selected,
  onSelect,
}: {
  analysis: RequestAnalysis
  selected: RequestAttr | null
  onSelect: (r: RequestAttr) => void
}) {
  const [sort, setSort] = useState<SortKey>('e2eS')
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const rows = useMemo(() => {
    const q = query.trim()
    const list = q ? analysis.requests.filter((r) => r.requestId.includes(q)) : analysis.requests
    return [...list].sort((a, b) => b[sort] - a[sort])
  }, [analysis, sort, query])

  return (
    <section className="rounded border border-edge bg-surface">
      <header className="flex items-center gap-2 border-b border-grid px-3 py-1.5">
        <h2 className="text-xs font-medium text-ink-secondary">All requests</h2>
        <input
          type="search"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setLimit(PAGE)
          }}
          placeholder="find request id"
          aria-label="Find request by id"
          className="ml-auto w-40 rounded border border-edge bg-page px-1.5 py-0.5 font-data text-xs text-ink placeholder:text-ink-muted"
        />
      </header>
      <div className="max-h-[28rem] overflow-y-auto">
        <table className="w-full font-data text-xs tabular-nums">
          <caption className="sr-only">Finished requests, seconds, phases as reported, slowest first</caption>
          <thead className="sticky top-0 bg-surface text-ink-secondary">
            <tr>
              <th scope="col" className="px-2 py-1 text-left font-medium">
                request
              </th>
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className="px-1 py-1 text-right font-medium"
                  aria-sort={sort === c.key ? 'descending' : 'none'}
                >
                  <button
                    type="button"
                    onClick={() => setSort(c.key)}
                    className={`rounded px-0.5 hover:bg-page ${sort === c.key ? 'text-ink' : ''}`}
                  >
                    {c.label}
                    {sort === c.key ? ' ↓' : ''}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, limit).map((r) => {
              const isSel = r.index === selected?.index
              return (
                <tr
                  key={r.index}
                  aria-selected={isSel}
                  className={`border-t border-grid text-ink ${isSel ? 'bg-page font-semibold' : 'hover:bg-page'}`}
                >
                  <td className="px-2 py-0.5 whitespace-nowrap">
                    <button
                      type="button"
                      onClick={() => onSelect(r)}
                      title={r.requestId}
                      className="text-left underline decoration-axis underline-offset-2 hover:decoration-ink-muted"
                    >
                      {r.requestId.length > 16 ? `…${r.requestId.slice(-14)}` : r.requestId}
                    </button>
                  </td>
                  {COLUMNS.map((c) => (
                    <td key={c.key} className="px-1 py-0.5 text-right">
                      {r[c.key].toFixed(2)}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
        {rows.length > limit && (
          <button
            type="button"
            onClick={() => setLimit((l) => l + PAGE * 4)}
            className="w-full border-t border-grid py-1 text-xs text-ink-secondary hover:bg-page"
          >
            Show more ({(rows.length - limit).toLocaleString()} left)
          </button>
        )}
        {rows.length === 0 && <p className="px-3 py-2 text-xs text-ink-muted">No request matches.</p>}
      </div>
    </section>
  )
}

