import { useEffect, useRef, useState } from 'react'
import { loadTrace } from './trace/load'
import type { Trace } from './trace/types'

type ThemeChoice = 'system' | 'light' | 'dark'

function useTheme(): [ThemeChoice, () => void] {
  const [theme, setTheme] = useState<ThemeChoice>(
    () => (localStorage.getItem('il-theme') as ThemeChoice) ?? 'system',
  )
  useEffect(() => {
    if (theme === 'system') {
      delete document.documentElement.dataset.theme
      localStorage.removeItem('il-theme')
    } else {
      document.documentElement.dataset.theme = theme
      localStorage.setItem('il-theme', theme)
    }
  }, [theme])
  const cycle = () =>
    setTheme((t) => (t === 'system' ? 'dark' : t === 'dark' ? 'light' : 'system'))
  return [theme, cycle]
}

const THEME_ICON: Record<ThemeChoice, string> = {
  system: '◐',
  dark: '●',
  light: '○',
}

interface Loaded {
  name: string
  trace: Trace
}

export default function App() {
  const [theme, cycleTheme] = useTheme()
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  async function open(file: File) {
    setBusy(true)
    setError(null)
    try {
      setLoaded({ name: file.name, trace: await loadTrace(file) })
    } catch (e) {
      setLoaded(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="flex h-full flex-col"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault()
        const file = e.dataTransfer.files[0]
        if (file) void open(file)
      }}
    >
      <header className="flex items-center gap-3 border-b border-edge bg-surface px-3 py-1.5">
        <h1 className="font-data text-sm font-semibold tracking-tight">
          inferlens
        </h1>
        <span className="font-data text-xs text-ink-secondary">
          {busy ? 'loading…' : (loaded?.name ?? 'no trace loaded')}
        </span>
        {loaded && (
          <span className="font-data text-xs text-ink-muted">
            {loaded.trace.meta?.engine} {loaded.trace.meta?.engine_version} ·{' '}
            {loaded.trace.meta?.model} ·{' '}
            {formatDuration(loaded.trace.end - loaded.trace.start)}
          </span>
        )}
        <button
          type="button"
          onClick={cycleTheme}
          title={`Theme: ${theme}`}
          className="ml-auto rounded px-2 py-0.5 text-sm text-ink-secondary hover:bg-page"
        >
          {THEME_ICON[theme]} {theme}
        </button>
      </header>

      <main className="flex flex-1 flex-col gap-2 overflow-y-auto p-2">
        {error && (
          <p
            role="alert"
            className="rounded border border-status-critical/40 bg-surface px-3 py-2 text-sm"
          >
            <span className="font-medium text-status-critical">
              Could not open trace:
            </span>{' '}
            {error}
          </p>
        )}
        {loaded === null ? (
          <button
            type="button"
            onClick={() => fileInput.current?.click()}
            className="flex flex-1 cursor-pointer items-center justify-center rounded border border-dashed border-axis text-ink-muted hover:border-ink-muted"
          >
            <p className="text-sm">
              Drop a <span className="font-data">.ilens</span> /{' '}
              <span className="font-data">.ilens.gz</span> trace here, or click
              to browse
            </p>
          </button>
        ) : (
          <>
            <TraceSummary trace={loaded.trace} />
            <TrackPlaceholder title="Engine timeline" />
            <TrackPlaceholder title="Requests" />
            <TrackPlaceholder title="KV / prefix cache" />
          </>
        )}
        <input
          ref={fileInput}
          type="file"
          hidden
          accept=".ilens,.gz"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void open(file)
            e.target.value = ''
          }}
        />
      </main>
    </div>
  )
}

function formatDuration(seconds: number): string {
  if (seconds < 90) return `${seconds.toFixed(1)} s`
  const m = Math.floor(seconds / 60)
  return `${m} min ${Math.round(seconds - m * 60)} s`
}

function TraceSummary({ trace }: { trace: Trace }) {
  const counts: [string, number][] = [
    ['snapshots', trace.snapshots.length],
    ['requests', trace.requests.length],
    ['kv stored', trace.kvStored.length],
    ['kv evicted', trace.kvRemoved.length],
    ['cache clears', trace.kvCleared.length],
    ['gaps', trace.gaps.length],
  ]
  const skippedKinds = Object.entries(trace.warnings.unknownKinds)
  return (
    <section className="rounded border border-edge bg-surface px-3 py-2">
      <dl className="flex flex-wrap gap-x-6 gap-y-1">
        {counts.map(([label, n]) => (
          <div key={label} className="flex items-baseline gap-1.5">
            <dd className="font-data text-sm">{n.toLocaleString()}</dd>
            <dt className="text-xs text-ink-secondary">{label}</dt>
          </div>
        ))}
      </dl>
      {(trace.warnings.truncated ||
        trace.warnings.noMeta ||
        trace.warnings.malformedRecords > 0 ||
        skippedKinds.length > 0) && (
        <p className="mt-1 text-xs text-ink-secondary">
          <span className="font-medium text-status-serious">⚠︎</span>{' '}
          {[
            trace.warnings.truncated && 'recording ended mid-write (prefix loaded)',
            trace.warnings.noMeta && 'no clock anchor — times are unaligned',
            trace.warnings.malformedRecords > 0 &&
              `${trace.warnings.malformedRecords} unreadable records skipped`,
            ...skippedKinds.map(([k, n]) => `${n} '${k}' records skipped (unknown kind)`),
          ]
            .filter(Boolean)
            .join(' · ')}
        </p>
      )}
    </section>
  )
}

function TrackPlaceholder({ title }: { title: string }) {
  return (
    <section className="rounded border border-edge bg-surface">
      <h2 className="border-b border-grid px-2 py-1 text-xs font-medium text-ink-secondary">
        {title}
      </h2>
      <div className="flex h-40 items-center justify-center text-xs text-ink-muted">
        coming soon
      </div>
    </section>
  )
}
