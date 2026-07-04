import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'
import { loadTrace, TraceSchemaError } from './load'

const META = {
  kind: 'trace_meta',
  engine: 'vllm',
  engine_version: '0.24.0',
  model: 'test-model',
  wall_time_unix: 1_000_000.0,
  monotonic_time: 500.0,
  schema_version: '0.3',
  extra: {},
}

const SNAPSHOT = {
  kind: 'engine_snapshot',
  ts: 510.0,
  num_running_reqs: 3,
  num_waiting_reqs: 1,
  kv_cache_usage: 0.5,
}

const KV_STORED = {
  kind: 'kv_block_stored',
  ts: 512.0,
  seq: 7,
  wall_time_unix: 1_000_011.5,
  block_hashes: [123, 456],
  parent_block_hash: null,
  num_tokens: 128,
  block_size: 128,
}

function jsonl(...records: unknown[]): string {
  return records.map((r) => JSON.stringify(r) + '\n').join('')
}

function blobOf(text: string): Blob {
  return new Blob([text])
}

function gzBlobOf(text: string): Blob {
  return new Blob([new Uint8Array(gzipSync(Buffer.from(text)))])
}

describe('loadTrace', () => {
  it('parses a plain JSONL trace and places events on the wall clock', async () => {
    const trace = await loadTrace(blobOf(jsonl(META, SNAPSHOT, KV_STORED)))
    expect(trace.eventCount).toBe(3)
    expect(trace.meta?.model).toBe('test-model')
    // Monotonic ts rebased through the anchor: 510 + (1_000_000 - 500).
    expect(trace.snapshots[0].t).toBeCloseTo(1_000_010.0, 6)
    // Defaults filled for fields the record omitted.
    expect(trace.snapshots[0].num_preempted_reqs).toBe(0)
    expect(trace.snapshots[0].ttft_mean_s).toBeNull()
    // kv_* place by their own wall_time_unix, never by receive-time ts.
    expect(trace.kvStored[0].t).toBeCloseTo(1_000_011.5, 6)
    expect(trace.start).toBeCloseTo(1_000_010.0, 6)
    expect(trace.end).toBeCloseTo(1_000_011.5, 6)
    expect(trace.warnings.truncated).toBe(false)
  })

  it('reads gzip-compressed traces (sniffed by magic bytes, not filename)', async () => {
    const trace = await loadTrace(gzBlobOf(jsonl(META, SNAPSHOT)))
    expect(trace.eventCount).toBe(2)
    expect(trace.snapshots).toHaveLength(1)
  })

  it('keeps the readable prefix of a truncated gzip stream', async () => {
    const full = gzipSync(Buffer.from(jsonl(META, SNAPSHOT, KV_STORED)))
    const cut = new Blob([new Uint8Array(full.subarray(0, full.length - 20))])
    const trace = await loadTrace(cut)
    expect(trace.warnings.truncated).toBe(true)
    expect(trace.meta).not.toBeNull()
    expect(trace.eventCount).toBeGreaterThanOrEqual(1)
  })

  it('treats an undecodable line as end-of-trace, not an error', async () => {
    const text = jsonl(META, SNAPSHOT) + '{"kind":"engine_snapshot","ts":5'
    const trace = await loadTrace(blobOf(text))
    expect(trace.snapshots).toHaveLength(1)
    expect(trace.warnings.truncated).toBe(true)
  })

  it('skips unknown kinds and ignores unknown fields (minor-version compat)', async () => {
    const trace = await loadTrace(
      blobOf(
        jsonl(
          META,
          { kind: 'from_the_future', ts: 511.0, payload: 1 },
          { ...SNAPSHOT, some_new_field: 'ignored' },
        ),
      ),
    )
    expect(trace.warnings.unknownKinds).toEqual({ from_the_future: 1 })
    expect(trace.snapshots).toHaveLength(1)
  })

  it('skips records missing required fields for their kind', async () => {
    const trace = await loadTrace(
      blobOf(jsonl(META, { kind: 'engine_snapshot', ts: 511.0 }, SNAPSHOT)),
    )
    expect(trace.warnings.malformedRecords).toBe(1)
    expect(trace.snapshots).toHaveLength(1)
  })

  it('rejects an unsupported schema major version', async () => {
    const bad = { ...META, schema_version: '1.0' }
    await expect(loadTrace(blobOf(jsonl(bad)))).rejects.toThrow(TraceSchemaError)
  })

  it('ignores mid-stream trace_meta records, keeping the first anchor', async () => {
    const later = { ...META, model: 'other-model' }
    const trace = await loadTrace(blobOf(jsonl(META, SNAPSHOT, later)))
    expect(trace.meta?.model).toBe('test-model')
    expect(trace.warnings.extraMetas).toBe(1)
  })

  it('falls back to raw ts placement when there is no anchor', async () => {
    const trace = await loadTrace(blobOf(jsonl(SNAPSHOT)))
    expect(trace.warnings.noMeta).toBe(true)
    expect(trace.snapshots[0].t).toBeCloseTo(510.0, 6)
  })

  it('preserves 64-bit block hashes beyond Number.MAX_SAFE_INTEGER', async () => {
    const big = '11346928661648350203'
    const text =
      JSON.stringify(META) +
      '\n' +
      // Hand-built line: JSON.stringify can't produce the unsafe integer.
      `{"kind":"kv_block_removed","ts":512.0,"seq":1,"wall_time_unix":1000011.0,` +
      `"block_hashes":[${big},1]}\n`
    const trace = await loadTrace(blobOf(text))
    expect(trace.kvRemoved[0].block_hashes[0]).toBe(big)
    expect(trace.kvRemoved[0].block_hashes[1]).toBe(1)
  })
})

describe('gpu-incident.ilens.gz (real trace, skipped when absent)', () => {
  const path = join(homedir(), 'Projects', 'inferlens-traces', 'gpu-incident.ilens.gz')

  it.skipIf(!existsSync(path))(
    'matches the Python reader event-for-event',
    async () => {
      const trace = await loadTrace(new Blob([new Uint8Array(readFileSync(path))]))
      // Ground truth from `inferlens.trace_io.read_trace` over the same file.
      expect(trace.eventCount).toBe(88_743)
      expect(trace.snapshots).toHaveLength(29_227)
      expect(trace.requests).toHaveLength(1_104)
      expect(trace.kvStored).toHaveLength(18_155)
      expect(trace.kvRemoved).toHaveLength(40_256)
      expect(trace.meta?.model).toBe('Qwen/Qwen2.5-1.5B-Instruct')
      const preempted = trace.snapshots.reduce((s, e) => s + e.num_preempted_reqs, 0)
      expect(preempted).toBe(665)
      expect(Math.max(...trace.snapshots.map((e) => e.kv_cache_usage))).toBe(1.0)
      expect(trace.warnings.truncated).toBe(false)
      expect(trace.warnings.malformedRecords).toBe(0)
      expect(trace.warnings.unknownKinds).toEqual({})
      // The whole point of wall placement: every event lands inside a sane,
      // shared timeline.
      expect(trace.end - trace.start).toBeGreaterThan(60)
      expect(trace.end - trace.start).toBeLessThan(3600)
    },
  )
})
