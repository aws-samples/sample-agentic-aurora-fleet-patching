// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState, useEffect, useCallback, useRef } from 'react'

const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')
const API = `${BASE}/api`

// ─── API auth token ──────────────────────────────────────────────────────────
// The backend is fail-closed: mutating requests (POST/PUT/PATCH/DELETE) require
// an API token unless the deployment explicitly opts out (ALLOW_UNAUTHENTICATED).
// Whoever adopts this code supplies the token to the UI one of two ways, no code
// change required:
//   1. Build-time: set VITE_API_AUTH_TOKEN before `npm run build` (baked in).
//   2. Runtime: set it in the browser once —
//        localStorage.setItem('apiAuthToken', '<token>')
//      (survives reloads; handy when the same build is reused across envs).
// If neither is set, requests go out without a token — fine for a deployment
// that set ALLOW_UNAUTHENTICATED=true, and otherwise the backend returns 503
// with a message explaining how to enable auth.
function getApiAuthToken() {
  try {
    const runtime = window.localStorage.getItem('apiAuthToken')
    if (runtime) return runtime
  } catch (e) { /* localStorage unavailable (e.g. SSR) — fall through */ }
  return import.meta.env.VITE_API_AUTH_TOKEN || ''
}

// Standard headers for a JSON request, including the auth token when present.
// Use this instead of hand-writing { 'Content-Type': 'application/json' } so the
// token is attached consistently on every call.
function authHeaders(extra = {}) {
  const headers = { 'Content-Type': 'application/json', ...extra }
  const token = getApiAuthToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
    headers['x-api-key'] = token
  }
  return headers
}

// Some endpoints (analyze, version-drift) run slow Bedrock work as async jobs to
// avoid API Gateway's 29s timeout. They return {job_id, status}; we poll
// /api/jobs/{id} until it completes. If a legacy synchronous result comes back
// (no job_id), we just return it as-is.
async function dispatchAndPoll(url, body, { intervalMs = 2000, timeoutMs = 480000 } = {}) {
  const res = await fetch(url, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await res.text())
  const started = await res.json()
  if (!started.job_id) return started // synchronous fallback
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, intervalMs))
    const jr = await fetch(`${API}/jobs/${started.job_id}`)
    if (!jr.ok) throw new Error(`Job status check failed: ${jr.status}`)
    const job = await jr.json()
    if (job.status === 'complete') return job.result
    if (job.status === 'failed') throw new Error(job.error || 'Job failed')
  }
  throw new Error('Timed out waiting for analysis to finish')
}

const STYLES = {
  app: { fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif', maxWidth: 1400, margin: '0 auto', padding: '24px 32px', background: '#0d1117', minHeight: '100vh', color: '#c9d1d9' },
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid #30363d', paddingBottom: 16, marginBottom: 24 },
  title: { fontSize: 22, fontWeight: 700, color: '#f0f6fc', letterSpacing: '-0.5px' },
  subtitle: { fontSize: 13, color: '#8b949e', marginTop: 4 },
  tabs: { display: 'flex', gap: 2, marginBottom: 24, background: '#161b22', borderRadius: 8, padding: 4, border: '1px solid #30363d' },
  tab: (active) => ({ padding: '10px 20px', borderRadius: 6, border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: 500, background: active ? '#1f6feb' : 'transparent', color: active ? '#fff' : '#8b949e', transition: 'all 0.15s' }),
  card: { background: '#161b22', border: '1px solid #30363d', borderRadius: 10, padding: 20, marginBottom: 16 },
  badge: (color) => ({ display: 'inline-block', padding: '3px 10px', borderRadius: 12, fontSize: 11, fontWeight: 600, letterSpacing: '0.3px', background: color === 'green' ? '#1a7f3722' : color === 'yellow' ? '#9e6a0322' : color === 'red' ? '#da363322' : color === 'blue' ? '#1f6feb22' : '#30363d', color: color === 'green' ? '#3fb950' : color === 'yellow' ? '#d29922' : color === 'red' ? '#f85149' : color === 'blue' ? '#58a6ff' : '#8b949e', border: `1px solid ${color === 'green' ? '#3fb95040' : color === 'yellow' ? '#d2992240' : color === 'red' ? '#f8514940' : color === 'blue' ? '#58a6ff40' : '#30363d'}` }),
  btn: (variant) => ({ padding: '8px 16px', borderRadius: 6, border: '1px solid transparent', cursor: 'pointer', fontSize: 13, fontWeight: 500, background: variant === 'primary' ? '#238636' : variant === 'danger' ? '#da3633' : variant === 'warning' ? '#9e6a03' : '#21262d', color: '#fff', transition: 'all 0.15s', borderColor: variant === 'primary' ? '#238636' : variant === 'danger' ? '#da3633' : variant === 'warning' ? '#9e6a03' : '#30363d' }),
  btnDisabled: { opacity: 0.5, cursor: 'not-allowed' },
  btnSm: { padding: '5px 12px', fontSize: 12 },
  table: { width: '100%', borderCollapse: 'separate', borderSpacing: 0, fontSize: 13 },
  th: { textAlign: 'left', padding: '12px 14px', borderBottom: '2px solid #30363d', color: '#8b949e', fontWeight: 600, fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.5px' },
  td: { padding: '12px 14px', borderBottom: '1px solid #21262d', verticalAlign: 'middle' },
  input: { background: '#0d1117', border: '1px solid #30363d', borderRadius: 6, padding: '8px 12px', color: '#c9d1d9', fontSize: 13, width: 60 },
  modal: { position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.75)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 },
  modalContent: { background: '#161b22', border: '1px solid #30363d', borderRadius: 12, padding: 28, width: 720, maxHeight: '85vh', overflow: 'auto' },
  toast: (type) => ({ position: 'fixed', bottom: 24, right: 24, padding: '12px 20px', borderRadius: 8, fontSize: 13, fontWeight: 500, zIndex: 9999, animation: 'slideUp 0.3s ease', background: type === 'success' ? '#1a7f37' : type === 'error' ? '#da3633' : '#1f6feb', color: '#fff', boxShadow: '0 4px 12px rgba(0,0,0,0.4)' }),
  sectionTitle: { fontSize: 11, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12 },
  progressBar: { width: '100%', height: 6, background: '#21262d', borderRadius: 3, overflow: 'hidden' },
  progressFill: (pct, color) => ({ width: `${pct}%`, height: '100%', background: color || '#238636', borderRadius: 3, transition: 'width 0.5s' }),
  timeline: { borderLeft: '2px solid #30363d', marginLeft: 12, paddingLeft: 16 },
  timelineItem: { marginBottom: 12, position: 'relative' },
  timelineDot: (color) => ({ width: 10, height: 10, borderRadius: '50%', background: color, position: 'absolute', left: -21, top: 4 }),
  checkRow: { display: 'flex', alignItems: 'flex-start', padding: '10px 0', borderBottom: '1px solid #21262d' },
  checkIcon: (passed, running) => ({ width: 20, height: 20, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, marginRight: 12, marginTop: 2, flexShrink: 0, background: running ? '#30363d' : passed ? '#1a7f37' : '#da3633', color: '#fff', border: running ? '2px solid #8b949e' : 'none' }),
}

const GUARDRAIL_LABELS = {
  cluster_status: { label: 'Cluster Health', desc: 'Cluster is available with no in-progress modifications' },
  concurrency_lock: { label: 'Concurrency Lock', desc: 'No other upgrade is running on this cluster' },
  replication_lag: { label: 'Replication Lag', desc: 'All replicas are caught up (lag < 200ms)' },
  long_running_txns: { label: 'Active Transactions', desc: 'No long-running or blocking transactions' },
  backup_current: { label: 'Backup Status', desc: 'Automated backups exist and latest restorable time is recent' },
  change_window: { label: 'Change Window', desc: 'Current time is within the approved maintenance window' },
  pre_upgrade_snapshot: { label: 'Pre-Upgrade Snapshot', desc: 'Manual snapshot taken as rollback anchor' },
}

// Toast notification component
function Toast({ message, type, onClose }) {
  useEffect(() => {
    const timer = setTimeout(onClose, 4000)
    return () => clearTimeout(timer)
  }, [onClose])

  return <div style={STYLES.toast(type)}>{message}</div>
}

function StatusBadge({ status }) {
  const map = {
    available: 'green', complete: 'green', ready: 'blue',
    provisioning: 'yellow', switching: 'yellow', validating: 'yellow', preflight: 'yellow', awaiting_approval: 'yellow',
    failed: 'red', preflight_failed: 'red', cancelled: 'red', validation_failed: 'red', rolled_back: 'red',
    dev: 'green', staging: 'yellow', production: 'red',
  }
  return <span style={STYLES.badge(map[status] || 'gray')}>{status}</span>
}

// Spinner for loading states
function Spinner({ size = 14 }) {
  return (
    <span style={{ display: 'inline-block', width: size, height: size, border: '2px solid #30363d', borderTopColor: '#58a6ff', borderRadius: '50%', animation: 'spin 0.6s linear infinite', marginRight: 6, verticalAlign: 'middle' }} />
  )
}

// ─────────────────────────────────────────────────────────────────
// Guardrails Modal
// ─────────────────────────────────────────────────────────────────
function GuardrailsModal({ cluster, onClose, onProceed }) {
  const [state, setState] = useState('idle')
  const [assessment, setAssessment] = useState(null)
  const [preflight, setPreflight] = useState(null)
  const [readyToUpgrade, setReadyToUpgrade] = useState(false)
  const [skipWindow, setSkipWindow] = useState(false)
  const [overrideConfirmed, setOverrideConfirmed] = useState(false)

  const runChecks = async () => {
    setState('running')
    setAssessment(null)
    setPreflight(null)
    try {
      const res = await fetch(`${API}/upgrades/assess`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          cluster_id: cluster.cluster_id,
          target_version: cluster.target_version,
          region: cluster.region,
          environment: cluster.environment,
          skip_window_check: skipWindow,
        }),
      })
      const data = await res.json()
      setAssessment(data.assessment)
      setPreflight(data.preflight)
      setReadyToUpgrade(data.ready_to_upgrade)
      setState('done')
    } catch (e) {
      setState('done')
    }
  }

  useEffect(() => { runChecks() }, [skipWindow])

  const allPreflightChecks = preflight?.checks || {}
  const riskFactors = assessment?.risk_factors || []
  const riskScore = assessment?.risk_score ?? null
  const failedChecks = Object.entries(allPreflightChecks).filter(([, c]) => !c.passed)
  const canOverride = state === 'done' && !readyToUpgrade && failedChecks.length > 0

  return (
    <div style={STYLES.modal} onClick={onClose}>
      <div style={STYLES.modalContent} onClick={e => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
          <div>
            <h2 style={{ margin: 0, color: '#f0f6fc', fontSize: 18 }}>Pre-Upgrade Guardrails</h2>
            <p style={{ margin: '4px 0 0', color: '#8b949e', fontSize: 13 }}>
              {cluster.cluster_id} &middot; {cluster.engine_version} &rarr; {cluster.target_version}
              <span style={{ ...STYLES.badge(cluster.strategy === 'in_place' ? 'yellow' : 'blue'), marginLeft: 8 }}>
                {cluster.strategy === 'in_place' ? 'In-Place (downtime)' : 'Blue/Green (zero-downtime)'}
              </span>
            </p>
          </div>
          <button style={{ ...STYLES.btn(), fontSize: 18, padding: '4px 12px', lineHeight: 1 }} onClick={onClose}>&times;</button>
        </div>

        {/* Risk Assessment */}
        <div style={{ marginBottom: 20 }}>
          <div style={STYLES.sectionTitle}>Risk Assessment</div>
          <div style={STYLES.card}>
            {state === 'running' && !assessment && (
              <div style={{ color: '#8b949e', fontSize: 13, display: 'flex', alignItems: 'center' }}><Spinner /> Analyzing cluster risk...</div>
            )}
            {assessment && (
              <>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                  <div style={{ fontSize: 13 }}>
                    Risk Score: <strong style={{ color: riskScore < 25 ? '#3fb950' : riskScore < 50 ? '#d29922' : '#f85149' }}>{riskScore}/100</strong>
                    <span style={{ marginLeft: 12 }}><StatusBadge status={assessment.risk_level} /></span>
                  </div>
                  <span style={{ fontSize: 12, color: '#8b949e' }}>{assessment.recommendation}</span>
                </div>
                {['Extension', 'Parameter', 'Major'].map(key => {
                  const found = riskFactors.find(f => f.includes(key))
                  return (
                    <div key={key} style={STYLES.checkRow}>
                      <div style={STYLES.checkIcon(!found, false)}>{found ? '!' : '✓'}</div>
                      <div>
                        <div style={{ fontSize: 13, fontWeight: 600 }}>{key} Compatibility</div>
                        <div style={{ fontSize: 12, color: '#8b949e' }}>{found || `No ${key.toLowerCase()} issues detected`}</div>
                      </div>
                    </div>
                  )
                })}
              </>
            )}
          </div>
        </div>

        {/* Operational Preflight */}
        <div style={{ marginBottom: 20 }}>
          <div style={STYLES.sectionTitle}>Operational Pre-Flight Checks</div>
          <div style={STYLES.card}>
            {state === 'running' && Object.keys(allPreflightChecks).length === 0 && (
              <div style={{ color: '#8b949e', fontSize: 13, display: 'flex', alignItems: 'center' }}><Spinner /> Running pre-flight checks...</div>
            )}
            {Object.entries(allPreflightChecks).map(([key, check]) => (
              <div key={key} style={STYLES.checkRow}>
                <div style={STYLES.checkIcon(check.passed, false)}>{check.passed ? '✓' : '✗'}</div>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{GUARDRAIL_LABELS[key]?.label || key}</span>
                    <span style={STYLES.badge(check.passed ? 'green' : 'red')}>{check.passed ? 'PASS' : 'FAIL'}</span>
                  </div>
                  <div style={{ fontSize: 12, color: '#8b949e', marginTop: 2 }}>{GUARDRAIL_LABELS[key]?.desc}</div>
                  <div style={{ fontSize: 12, marginTop: 4, color: check.passed ? '#3fb950' : '#f85149' }}>{check.reason}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Actions */}
        <div style={{ borderTop: '1px solid #30363d', paddingTop: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
              <label style={{ fontSize: 12, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 6 }}>
                <input type="checkbox" checked={skipWindow} onChange={e => { setSkipWindow(e.target.checked); setOverrideConfirmed(false) }} />
                Skip window check
              </label>
              {state === 'done' && (
                <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={() => { setOverrideConfirmed(false); runChecks() }}>Re-run</button>
              )}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button style={STYLES.btn()} onClick={onClose}>Cancel</button>
              {readyToUpgrade && state === 'done' && (
                <button style={STYLES.btn('primary')} onClick={() => onProceed(cluster, skipWindow)}>Proceed with Upgrade</button>
              )}
            </div>
          </div>

          {canOverride && (
            <div style={{ marginTop: 12, padding: 12, background: '#1c1208', border: '1px solid #9e6a03', borderRadius: 6 }}>
              <div style={{ fontSize: 13, color: '#d29922', marginBottom: 8 }}>
                {failedChecks.length} check(s) failed. Override at your own risk.
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <label style={{ fontSize: 12, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 6 }}>
                  <input type="checkbox" checked={overrideConfirmed} onChange={e => setOverrideConfirmed(e.target.checked)} />
                  I understand the risks
                </label>
                <button
                  style={{ ...STYLES.btn('warning'), ...(overrideConfirmed ? {} : STYLES.btnDisabled) }}
                  disabled={!overrideConfirmed}
                  onClick={() => onProceed(cluster, skipWindow)}
                >Override &amp; Proceed</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Cluster Detail Panel
// ─────────────────────────────────────────────────────────────────
function ClusterDetailPanel({ cluster, onClose }) {
  const [analysis, setAnalysis] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedTarget, setSelectedTarget] = useState(cluster.target_version || '')
  const [stepResults, setStepResults] = useState({})
  const [runningStep, setRunningStep] = useState(null)
  const [baseline, setBaseline] = useState(null)
  const [baselineLoading, setBaselineLoading] = useState(false)
  const [comparison, setComparison] = useState(null)
  const [dbValidation, setDbValidation] = useState(null)
  const [versionDrift, setVersionDrift] = useState(null)
  const [driftLoading, setDriftLoading] = useState(false)

  const captureBaseline = async () => {
    setBaselineLoading(true)
    try {
      const res = await fetch(`${API}/agent/baseline/capture`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ cluster_id: cluster.cluster_id, region: cluster.region }) })
      setBaseline(await res.json())
    } catch (e) { setBaseline({ error: e.message }) }
    setBaselineLoading(false)
  }

  const compareBaseline = async () => {
    setBaselineLoading(true)
    try {
      const res = await fetch(`${API}/agent/baseline/compare`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ cluster_id: cluster.cluster_id, region: cluster.region }) })
      setComparison(await res.json())
    } catch (e) { setComparison({ error: e.message }) }
    setBaselineLoading(false)
  }

  const runDbValidation = async () => {
    setBaselineLoading(true)
    try {
      const res = await fetch(`${API}/agent/validate-db`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ cluster_id: cluster.cluster_id, region: cluster.region }) })
      setDbValidation(await res.json())
    } catch (e) { setDbValidation({ error: e.message }) }
    setBaselineLoading(false)
  }

  const executeStep = async (action, params, stepType, stepKey) => {
    setRunningStep(stepKey)
    try {
      const res = await fetch(`${API}/agent/execute-step`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ action, params, cluster_id: cluster.cluster_id, step_type: stepType, region: cluster.region }),
      })
      const result = await res.json()
      setStepResults(prev => ({ ...prev, [stepKey]: result }))
    } catch (e) {
      setStepResults(prev => ({ ...prev, [stepKey]: { status: 'error', message: e.message } }))
    }
    setRunningStep(null)
  }

  const runAnalysis = async (targetVersion) => {
    if (!targetVersion) return
    setLoading(true)
    setError(null)
    try {
      const result = await dispatchAndPoll(`${API}/agent/analyze`, {
        cluster_id: cluster.cluster_id, target_version: targetVersion, region: cluster.region,
      })
      setAnalysis(result)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const runVersionDrift = async (targetVersion) => {
    if (!targetVersion) return
    setDriftLoading(true)
    setVersionDrift(null)
    try {
      const result = await dispatchAndPoll(`${API}/agent/version-drift`, {
        current_version: cluster.engine_version, target_version: targetVersion, cluster_id: cluster.cluster_id,
      })
      setVersionDrift(result)
    } catch (e) {
      setVersionDrift({ error: e.message })
    }
    setDriftLoading(false)
  }

  useEffect(() => { if (selectedTarget) runAnalysis(selectedTarget) }, [])

  return (
    <div style={{ ...STYLES.card, borderColor: '#58a6ff', marginTop: 0, borderTop: 'none', borderTopLeftRadius: 0, borderTopRightRadius: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h3 style={{ margin: 0, color: '#f0f6fc', fontSize: 15 }}>
          Agent Analysis: <code style={{ color: '#58a6ff' }}>{cluster.cluster_id}</code>
        </h3>
        <button onClick={onClose} style={{ ...STYLES.btn(), ...STYLES.btnSm }}>Close</button>
      </div>

      {/* Version selector */}
      {cluster.available_versions && cluster.available_versions.length > 0 && (
        <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 13, color: '#8b949e' }}>Target:</span>
          <select
            value={selectedTarget}
            onChange={(e) => { setSelectedTarget(e.target.value); runAnalysis(e.target.value); setVersionDrift(null) }}
            style={{ ...STYLES.input, width: 'auto', padding: '6px 10px' }}
          >
            {cluster.available_versions.map(v => {
              const isLts = ['13.16','14.12','15.8','16.4','17.4','2.11.6','3.04.3'].includes(v) ||
                            (v.includes('mysql_aurora') && ['3.04.3'].some(lts => v.includes(lts)))
              return (
                <option key={v} value={v}>
                  {v} {isLts ? '★ LTS' : ''}{v === cluster.target_version ? ' (recommended)' : ''}
                </option>
              )
            })}
          </select>
          <span style={{ fontSize: 12, color: '#8b949e' }}>Current: <code>{cluster.engine_version}</code></span>
          <button
            style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }}
            onClick={() => runVersionDrift(selectedTarget)}
            disabled={driftLoading}
          >
            {driftLoading ? <><Spinner size={10} /> Analyzing...</> : 'Version Changes'}
          </button>
        </div>
      )}

      {/* Version Drift Analysis */}
      {versionDrift && !versionDrift.error && (
        <div style={{ marginBottom: 20, padding: 16, background: '#0d1117', borderRadius: 8, border: '1px solid #1f6feb30' }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: '#58a6ff', marginBottom: 10 }}>
            Version Changes: {cluster.engine_version} &rarr; {selectedTarget}
          </div>
          {versionDrift.summary && (
            <div style={{ fontSize: 12, color: '#c9d1d9', marginBottom: 14, lineHeight: 1.6 }}>{versionDrift.summary}</div>
          )}

          {/* Parameter Default Changes */}
          {versionDrift.parameter_changes?.length > 0 && (
            <details open style={{ marginBottom: 12 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: '#d29922', cursor: 'pointer', marginBottom: 6 }}>
                Parameter Default Changes ({versionDrift.parameter_changes.length})
              </summary>
              <table style={{ ...STYLES.table }}>
                <thead><tr>
                  <th style={{ ...STYLES.th, fontSize: 10 }}>Parameter</th>
                  <th style={{ ...STYLES.th, fontSize: 10 }}>Old Default</th>
                  <th style={{ ...STYLES.th, fontSize: 10 }}>New Default</th>
                  <th style={{ ...STYLES.th, fontSize: 10 }}>Impact</th>
                  <th style={{ ...STYLES.th, fontSize: 10 }}>Note</th>
                </tr></thead>
                <tbody>
                  {versionDrift.parameter_changes.map((p, i) => (
                    <tr key={i}>
                      <td style={{ ...STYLES.td, fontSize: 11 }}><code>{p.parameter}</code></td>
                      <td style={{ ...STYLES.td, fontSize: 11 }}><code>{p.old_default}</code></td>
                      <td style={{ ...STYLES.td, fontSize: 11, color: '#3fb950' }}><code>{p.new_default}</code></td>
                      <td style={STYLES.td}><span style={STYLES.badge(p.impact === 'high' ? 'red' : p.impact === 'medium' ? 'yellow' : 'green')}>{p.impact}</span></td>
                      <td style={{ ...STYLES.td, fontSize: 11 }}>{p.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}

          {/* Behavioral Changes */}
          {versionDrift.behavioral_changes?.length > 0 && (
            <details open style={{ marginBottom: 12 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: '#f85149', cursor: 'pointer', marginBottom: 6 }}>
                Behavioral Changes ({versionDrift.behavioral_changes.length})
              </summary>
              {versionDrift.behavioral_changes.map((b, i) => (
                <div key={i} style={{ padding: '8px 12px', borderLeft: `3px solid ${b.impact === 'high' ? '#f85149' : b.impact === 'medium' ? '#d29922' : '#3fb950'}`, marginBottom: 6, background: '#161b22', borderRadius: '0 4px 4px 0' }}>
                  <div style={{ fontSize: 12, color: '#f0f6fc' }}>{b.change}</div>
                  {b.mitigation && <div style={{ fontSize: 11, color: '#8b949e', marginTop: 4 }}>Mitigation: {b.mitigation}</div>}
                </div>
              ))}
            </details>
          )}

          {/* Deprecated Features */}
          {versionDrift.deprecated_features?.length > 0 && (
            <details style={{ marginBottom: 12 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: '#f85149', cursor: 'pointer', marginBottom: 6 }}>
                Deprecated/Removed ({versionDrift.deprecated_features.length})
              </summary>
              {versionDrift.deprecated_features.map((d, i) => (
                <div key={i} style={{ fontSize: 12, color: '#f0f6fc', padding: '4px 12px', borderLeft: '2px solid #da3633', marginBottom: 4 }}>
                  <strong>{d.feature}</strong>: {d.impact}
                </div>
              ))}
            </details>
          )}

          {/* New Features */}
          {versionDrift.new_features?.length > 0 && (
            <details style={{ marginBottom: 12 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: '#3fb950', cursor: 'pointer', marginBottom: 6 }}>
                New Features ({versionDrift.new_features.length})
              </summary>
              {versionDrift.new_features.map((f, i) => (
                <div key={i} style={{ fontSize: 12, color: '#f0f6fc', padding: '4px 12px', borderLeft: '2px solid #238636', marginBottom: 4 }}>
                  <strong>{f.feature}</strong>: {f.benefit}
                </div>
              ))}
            </details>
          )}

          {/* Performance Changes */}
          {versionDrift.performance_changes?.length > 0 && (
            <details style={{ marginBottom: 12 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: '#79c0ff', cursor: 'pointer', marginBottom: 6 }}>
                Performance Impact ({versionDrift.performance_changes.length})
              </summary>
              {versionDrift.performance_changes.map((p, i) => (
                <div key={i} style={{ fontSize: 12, color: '#c9d1d9', padding: '4px 12px', borderLeft: `2px solid ${p.expected_impact === 'positive' ? '#238636' : p.expected_impact === 'negative' ? '#da3633' : '#8b949e'}`, marginBottom: 4 }}>
                  <span style={{ color: p.expected_impact === 'positive' ? '#3fb950' : p.expected_impact === 'negative' ? '#f85149' : '#8b949e' }}>{p.expected_impact === 'positive' ? '+' : p.expected_impact === 'negative' ? '-' : '~'}</span> {p.change}
                </div>
              ))}
            </details>
          )}

          {/* Recommendations */}
          {versionDrift.recommendations?.length > 0 && (
            <details open style={{ marginBottom: 4 }}>
              <summary style={{ fontSize: 12, fontWeight: 600, color: '#58a6ff', cursor: 'pointer', marginBottom: 6 }}>
                Recommendations ({versionDrift.recommendations.length})
              </summary>
              {versionDrift.recommendations.map((r, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '6px 0', borderBottom: '1px solid #21262d' }}>
                  <span style={STYLES.badge(r.priority === 'critical' ? 'red' : r.priority === 'high' ? 'yellow' : r.priority === 'medium' ? 'blue' : 'green')}>{r.priority}</span>
                  <div>
                    <div style={{ fontSize: 12, color: '#f0f6fc' }}>{r.action}</div>
                    <div style={{ fontSize: 11, color: '#8b949e' }}>{r.reason}</div>
                  </div>
                </div>
              ))}
            </details>
          )}
        </div>
      )}
      {versionDrift?.error && (
        <div style={{ marginBottom: 16, padding: 10, background: '#da363315', borderRadius: 6, fontSize: 12, color: '#f85149' }}>Version drift analysis failed: {versionDrift.error}</div>
      )}

      {loading && (
        <div style={{ textAlign: 'center', padding: 40, color: '#8b949e' }}>
          <Spinner size={20} />
          <div style={{ fontSize: 14, marginTop: 8 }}>Agent analyzing cluster configuration...</div>
          <div style={{ fontSize: 12, marginTop: 4 }}>Parameters, extensions, CVE exposure, utilization</div>
        </div>
      )}

      {error && <div style={{ padding: 12, background: '#da363322', borderRadius: 6, color: '#f85149', fontSize: 13, border: '1px solid #da363340' }}>{error}</div>}

      {analysis && !loading && (
        <div>
          {/* Summary */}
          <div style={{ display: 'flex', gap: 10, marginBottom: 16, flexWrap: 'wrap' }}>
            <span style={{ padding: '6px 14px', borderRadius: 6, background: analysis.ready_to_upgrade ? '#23863622' : '#da363322', color: analysis.ready_to_upgrade ? '#3fb950' : '#f85149', fontSize: 13, fontWeight: 600, border: `1px solid ${analysis.ready_to_upgrade ? '#23863640' : '#da363340'}` }}>
              {analysis.ready_to_upgrade ? '✓ Ready to Upgrade' : '✗ Not Ready'}
            </span>
            <span style={{ padding: '6px 14px', borderRadius: 6, background: '#21262d', color: '#c9d1d9', fontSize: 13 }}>
              {analysis.is_major_upgrade ? 'Major' : 'Minor'}: {analysis.current_version} → {analysis.target_version}
            </span>
            {analysis.recommended_strategy && (
              <span style={{ padding: '6px 14px', borderRadius: 6, background: '#1f6feb22', color: '#58a6ff', fontSize: 13, border: '1px solid #1f6feb40' }}>
                Strategy: {analysis.recommended_strategy.replace('_', ' ')}
              </span>
            )}
            {analysis.estimated_duration && (
              <span style={{ padding: '6px 14px', borderRadius: 6, background: '#21262d', color: '#c9d1d9', fontSize: 13 }}>
                ~{analysis.estimated_duration}
              </span>
            )}
          </div>

          {/* Blocking issues */}
          {analysis.blocking_issues && analysis.blocking_issues.length > 0 && (
            <div style={{ marginBottom: 16, padding: 12, background: '#da363315', borderRadius: 6, border: '1px solid #da363330' }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: '#f85149', marginBottom: 6 }}>Blocking Issues</div>
              {analysis.blocking_issues.map((issue, i) => (
                <div key={i} style={{ fontSize: 12, color: '#f0f6fc', padding: '4px 0' }}>&#x2716; {issue}</div>
              ))}
            </div>
          )}

          {/* Parameter Analysis */}
          {analysis.parameter_analysis && analysis.parameter_analysis.status === 'analyzed' && (
            <div style={{ marginBottom: 16 }}>
              <div style={STYLES.sectionTitle}>Parameters ({analysis.parameter_analysis.custom_param_count} custom)</div>
              {analysis.parameter_analysis.issues.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  {analysis.parameter_analysis.issues.map((issue, i) => (
                    <div key={i} style={{ fontSize: 12, color: '#f0f6fc', padding: '4px 0 4px 12px', borderLeft: '2px solid #da3633', marginBottom: 4 }}>
                      <strong>{issue.parameter}</strong>: {issue.message}
                    </div>
                  ))}
                </div>
              )}
              {analysis.parameter_analysis.warnings.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  {analysis.parameter_analysis.warnings.map((w, i) => (
                    <div key={i} style={{ fontSize: 12, color: '#c9d1d9', padding: '4px 0 4px 12px', borderLeft: '2px solid #d29922', marginBottom: 4 }}>
                      <code>{w.parameter}</code> default changes to <code>{w.new_default}</code>
                    </div>
                  ))}
                </div>
              )}
              {analysis.parameter_analysis.custom_params.length > 0 && (
                <details style={{ marginTop: 8 }}>
                  <summary style={{ fontSize: 12, color: '#58a6ff', cursor: 'pointer' }}>View all custom parameters</summary>
                  <table style={{ ...STYLES.table, marginTop: 8 }}>
                    <thead><tr><th style={{ ...STYLES.th, fontSize: 10 }}>Parameter</th><th style={{ ...STYLES.th, fontSize: 10 }}>Value</th><th style={{ ...STYLES.th, fontSize: 10 }}>Apply</th></tr></thead>
                    <tbody>
                      {analysis.parameter_analysis.custom_params.map((p, i) => (
                        <tr key={i}><td style={{ ...STYLES.td, fontSize: 11 }}><code>{p.name}</code></td><td style={{ ...STYLES.td, fontSize: 11 }}><code>{p.value}</code></td><td style={{ ...STYLES.td, fontSize: 11 }}>{p.apply_type}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}
            </div>
          )}

          {/* Extension Analysis */}
          {analysis.extension_analysis && analysis.extension_analysis.compatible_extensions.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <div style={STYLES.sectionTitle}>Extensions</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {analysis.extension_analysis.compatible_extensions.map((ext, i) => (
                  <span key={i} style={{ fontSize: 11, padding: '4px 10px', background: '#0d419d20', border: '1px solid #1f6feb30', borderRadius: 12, color: '#79c0ff' }}>
                    {ext.extension} (min: {ext.min_version_required})
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Size & Utilization */}
          <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
            {analysis.size_analysis && !analysis.size_analysis.error && (
              <div style={{ flex: 1, minWidth: 200, padding: 12, background: '#21262d', borderRadius: 6 }}>
                <div style={STYLES.sectionTitle}>Cluster Size</div>
                <div style={{ fontSize: 12, color: '#c9d1d9', lineHeight: 1.8 }}>
                  Storage: {analysis.size_analysis.storage_gb} GB<br/>
                  Instances: {analysis.size_analysis.instance_count}<br/>
                  Sync: {analysis.size_analysis.sync_estimate}
                </div>
              </div>
            )}
            {analysis.utilization && !analysis.utilization.error && (
              <div style={{ flex: 1, minWidth: 200, padding: 12, background: '#21262d', borderRadius: 6 }}>
                <div style={STYLES.sectionTitle}>Utilization (7-day)</div>
                <div style={{ fontSize: 12, color: '#c9d1d9', lineHeight: 1.8 }}>
                  {analysis.utilization.avg_cpu_7d !== undefined && <>Avg CPU: {analysis.utilization.avg_cpu_7d}% (max: {analysis.utilization.max_cpu_7d}%)<br/></>}
                  {analysis.utilization.avg_write_iops_7d !== undefined && <>Write IOPS: {analysis.utilization.avg_write_iops_7d}<br/></>}
                  {analysis.utilization.avg_connections_24h !== undefined && <>Connections: {analysis.utilization.avg_connections_24h}<br/></>}
                  {analysis.utilization.lowest_utilization_hours_utc && (
                    <span style={{ color: '#3fb950' }}>Best window: {analysis.utilization.lowest_utilization_hours_utc.map(h => `${h}:00`).join(', ')} UTC</span>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Database Objects */}
          {analysis.database_metadata && !analysis.database_metadata.error && analysis.database_metadata.databases && (
            <div style={{ marginBottom: 16 }}>
              <div style={STYLES.sectionTitle}>Database Objects (Live)</div>
              {analysis.database_metadata.databases.map((db, di) => (
                <details key={di} style={{ marginBottom: 8 }}>
                  <summary style={{ fontSize: 13, color: '#58a6ff', cursor: 'pointer', padding: '6px 0' }}>
                    {db.name} <span style={{ color: '#8b949e', fontWeight: 400 }}>({db.size})</span>
                    {db.tables && <span style={{ color: '#8b949e', marginLeft: 8, fontSize: 11 }}>{db.tables.length} tables, {db.total_rows?.toLocaleString()} rows</span>}
                  </summary>
                  <div style={{ padding: '8px 12px', background: '#21262d', borderRadius: 6, marginTop: 4 }}>
                    {db.extensions && db.extensions.length > 0 && (
                      <div style={{ marginBottom: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        {db.extensions.map((ext, i) => (
                          <span key={i} style={{ fontSize: 10, padding: '2px 8px', background: '#0d419d20', borderRadius: 12, color: '#79c0ff' }}>{ext.name} v{ext.version}</span>
                        ))}
                      </div>
                    )}
                    {db.functions && db.functions.length > 0 && (
                      <div style={{ marginBottom: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        {db.functions.map((fn, i) => (
                          <span key={i} style={{ fontSize: 10, padding: '2px 8px', background: '#23863620', borderRadius: 12, color: '#7ee787' }}>{fn.name} ({fn.language})</span>
                        ))}
                      </div>
                    )}
                    {db.tables && db.tables.length > 0 && (
                      <table style={{ ...STYLES.table, marginTop: 4 }}>
                        <thead><tr><th style={{ ...STYLES.th, fontSize: 10, padding: '6px 8px' }}>Table</th><th style={{ ...STYLES.th, fontSize: 10, padding: '6px 8px' }}>Rows</th><th style={{ ...STYLES.th, fontSize: 10, padding: '6px 8px' }}>Size</th></tr></thead>
                        <tbody>
                          {db.tables.map((t, i) => (
                            <tr key={i}><td style={{ ...STYLES.td, fontSize: 10, padding: '4px 8px' }}><code>{t.schema}.{t.name}</code></td><td style={{ ...STYLES.td, fontSize: 10, padding: '4px 8px' }}>{t.rows?.toLocaleString()}</td><td style={{ ...STYLES.td, fontSize: 10, padding: '4px 8px' }}>{t.size}</td></tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                </details>
              ))}
            </div>
          )}

          {/* Change Plan */}
          {analysis.change_plan && (
            <div style={{ marginBottom: 16 }}>
              <div style={STYLES.sectionTitle}>Agent Change Plan</div>
              {analysis.change_plan.strategy_reason && (
                <div style={{ fontSize: 12, color: '#c9d1d9', marginBottom: 10, padding: 10, background: '#1f6feb10', borderRadius: 6, borderLeft: '3px solid #1f6feb' }}>
                  <strong>Strategy:</strong> {analysis.change_plan.strategy?.replace('_', ' ')} — {analysis.change_plan.strategy_reason}
                </div>
              )}

              {/* Steps grouped */}
              {[
                { key: 'pre_upgrade_steps', label: 'Pre-Upgrade', color: '#d29922', prefix: 'pre' },
                { key: 'upgrade_steps', label: 'Upgrade Execution', color: '#1f6feb', prefix: 'upgrade' },
                { key: 'post_upgrade_steps', label: 'Post-Upgrade Validation', color: '#238636', prefix: 'post' },
              ].map(({ key, label, color, prefix }) => {
                const steps = analysis.change_plan[key]
                if (!steps || steps.length === 0) return null
                return (
                  <div key={key} style={{ marginBottom: 12 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color }}>{label}</div>
                      {prefix !== 'upgrade' && (
                        <button
                          style={{ ...STYLES.btn(prefix === 'pre' ? 'warning' : 'primary'), ...STYLES.btnSm }}
                          onClick={async () => {
                            for (let i = 0; i < steps.length; i++) {
                              const step = steps[i]
                              if (step.exec_action && !stepResults[`${prefix}_${i}`]?.status) {
                                await executeStep(step.exec_action, step.exec_params || {}, prefix, `${prefix}_${i}`)
                              }
                            }
                          }}
                        >Run All</button>
                      )}
                    </div>
                    {steps.map((step, i) => {
                      const stepKey = `${prefix}_${i}`
                      const result = stepResults[stepKey]
                      const displayText = step.command || step.validation
                      const canRun = Boolean(step.exec_action)
                      return (
                        <div key={i} style={{ fontSize: 12, padding: '6px 0 6px 12px', borderLeft: `2px solid ${result?.status === 'success' ? '#238636' : result?.status === 'failed' ? '#da3633' : '#30363d'}`, marginBottom: 6 }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                            <div style={{ color: '#f0f6fc', flex: 1 }}><strong>{i+1}.</strong> {step.action}</div>
                            {canRun && (
                              <button
                                style={{ ...STYLES.btn(result?.status === 'success' ? 'primary' : undefined), ...STYLES.btnSm, marginLeft: 8, opacity: runningStep === stepKey ? 0.6 : 1 }}
                                onClick={() => executeStep(step.exec_action, step.exec_params || {}, prefix, stepKey)}
                                disabled={runningStep === stepKey}
                              >{runningStep === stepKey ? <><Spinner size={10} />Running</> : result?.status === 'success' ? '✓ Done' : 'Run'}</button>
                            )}
                          </div>
                          {displayText && <code style={{ display: 'block', marginTop: 4, fontSize: 10, color: '#79c0ff', background: '#0d1117', padding: '3px 6px', borderRadius: 3, wordBreak: 'break-all' }}>{displayText}</code>}
                          {result && (
                            <div style={{ marginTop: 4, padding: 6, background: result.status === 'success' ? '#0d1f0d' : '#1c0e0e', borderRadius: 4, fontSize: 11 }}>
                              <span style={{ color: result.status === 'success' ? '#7ee787' : '#f85149' }}>{result.status === 'success' ? 'Completed' : 'Failed'}</span>
                              {result.stdout && <pre style={{ marginTop: 4, color: '#8b949e', whiteSpace: 'pre-wrap', maxHeight: 80, overflow: 'auto', fontSize: 10 }}>{result.stdout.slice(0, 800)}</pre>}
                              {result.stderr && <pre style={{ marginTop: 2, color: '#f85149', whiteSpace: 'pre-wrap', maxHeight: 60, overflow: 'auto', fontSize: 10 }}>{result.stderr.slice(0, 400)}</pre>}
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )
              })}

              {analysis.change_plan.rollback_plan && (
                <div style={{ fontSize: 12, color: '#8b949e', padding: 8, background: '#da363310', borderRadius: 4, border: '1px solid #da363320' }}>
                  <strong style={{ color: '#f85149' }}>Rollback:</strong> {analysis.change_plan.rollback_plan}
                </div>
              )}
            </div>
          )}

          {/* Performance Baseline */}
          <div style={{ padding: 14, background: '#21262d', borderRadius: 8 }}>
            <div style={STYLES.sectionTitle}>Performance Baseline & Validation</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
              <button style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }} onClick={captureBaseline} disabled={baselineLoading}>
                {baselineLoading ? <><Spinner size={10} /> Working...</> : 'Capture Baseline'}
              </button>
              <button style={{ ...STYLES.btn('warning'), ...STYLES.btnSm }} onClick={compareBaseline} disabled={baselineLoading}>
                Compare After Upgrade
              </button>
              <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={runDbValidation} disabled={baselineLoading}>
                Run DB Validation
              </button>
            </div>

            {baseline && !baseline.error && (
              <div style={{ fontSize: 11, color: '#7ee787', marginBottom: 6 }}>
                Baseline: {baseline.query_count} queries ({Math.round(baseline.total_tracked_time_ms)}ms total) at {new Date(baseline.captured_at).toLocaleString()}
              </div>
            )}
            {baseline?.error && <div style={{ fontSize: 11, color: '#f85149' }}>Error: {baseline.error}</div>}

            {comparison && !comparison.error && (
              <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: comparison.overall_verdict === 'pass' ? '#7ee787' : comparison.overall_verdict === 'warning' ? '#d29922' : '#f85149', marginBottom: 4 }}>
                  {comparison.overall_verdict.toUpperCase()} — {comparison.summary?.regressions_found || 0} regressions
                </div>
                {comparison.regressions && comparison.regressions.slice(0, 3).map((r, i) => (
                  <div key={i} style={{ fontSize: 10, padding: '3px 6px', background: '#1c0e0e', borderRadius: 3, marginBottom: 2 }}>
                    <code>{r.query?.slice(0, 60)}...</code>
                    <span style={{ color: '#f85149', marginLeft: 6 }}>+{r.pct_change}%</span>
                  </div>
                ))}
              </div>
            )}

            {dbValidation && !dbValidation.error && (
              <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: dbValidation.passed ? '#7ee787' : '#f85149', marginBottom: 4 }}>
                  DB Validation: {dbValidation.passed ? 'ALL PASSED' : 'ISSUES FOUND'}
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {dbValidation.checks?.map((c, i) => (
                    <span key={i} style={STYLES.badge(c.status === 'pass' ? 'green' : c.status === 'warning' ? 'yellow' : 'red')}>
                      {c.status === 'pass' ? '✓' : '!'} {c.name}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Fleet View
// ─────────────────────────────────────────────────────────────────
function FleetView({ clusters, onScan, onStartUpgrade, scanning, onDeleteBG, onDeleteOldCluster, secScan }) {
  const [selectedCluster, setSelectedCluster] = useState(null)
  const [filterEngine, setFilterEngine] = useState('all')
  const [filterEnv, setFilterEnv] = useState('all')
  const [filterStatus, setFilterStatus] = useState('all')

  // Apply filters
  const filteredClusters = clusters.filter(c => {
    if (filterEngine !== 'all') {
      if (filterEngine === 'postgresql' && c.engine_type === 'aurora-mysql') return false
      if (filterEngine === 'mysql' && c.engine_type !== 'aurora-mysql') return false
    }
    if (filterEnv !== 'all' && c.environment !== filterEnv) return false
    if (filterStatus === 'needs_upgrade' && !c.upgrade_available) return false
    if (filterStatus === 'up_to_date' && c.upgrade_available) return false
    if (filterStatus === 'old' && c.cluster_type !== 'old_primary') return false
    return true
  })

  const envCounts = clusters.reduce((acc, c) => {
    acc[c.environment] = (acc[c.environment] || 0) + 1
    return acc
  }, {})

  const upgradeCount = clusters.filter(c => c.upgrade_available).length

  return (
    <div>
      {/* Summary cards */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 20 }}>
        <div style={{ ...STYLES.card, flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 24, fontWeight: 700, color: '#f0f6fc' }}>{clusters.length}</div>
            <div style={{ fontSize: 12, color: '#8b949e' }}>Total Clusters</div>
            <div style={{ fontSize: 11, color: '#8b949e', marginTop: 2 }}>
              {clusters.filter(c => c.engine_type !== 'aurora-mysql').length} PostgreSQL / {clusters.filter(c => c.engine_type === 'aurora-mysql').length} MySQL
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            {Object.entries(envCounts).map(([env, count]) => (
              <span key={env} style={STYLES.badge(env === 'production' ? 'red' : env === 'staging' ? 'yellow' : 'green')}>{env}: {count}</span>
            ))}
          </div>
        </div>
        <div style={{ ...STYLES.card, flex: '0 0 auto', display: 'flex', alignItems: 'center', gap: 16 }}>
          <div>
            <div style={{ fontSize: 24, fontWeight: 700, color: upgradeCount > 0 ? '#d29922' : '#3fb950' }}>{upgradeCount}</div>
            <div style={{ fontSize: 12, color: '#8b949e' }}>Upgrades Available</div>
          </div>
          <button style={STYLES.btn('primary')} onClick={onScan} disabled={scanning}>
            {scanning ? <><Spinner /> Scanning...</> : 'Scan Fleet'}
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'center' }}>
        <span style={{ fontSize: 11, color: '#8b949e', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Filter:</span>
        <select
          value={filterEngine}
          onChange={(e) => setFilterEngine(e.target.value)}
          style={{ ...STYLES.input, width: 'auto', padding: '6px 10px', cursor: 'pointer' }}
        >
          <option value="all">All Engines</option>
          <option value="postgresql">PostgreSQL</option>
          <option value="mysql">MySQL</option>
        </select>
        <select
          value={filterEnv}
          onChange={(e) => setFilterEnv(e.target.value)}
          style={{ ...STYLES.input, width: 'auto', padding: '6px 10px', cursor: 'pointer' }}
        >
          <option value="all">All Environments</option>
          <option value="production">Production</option>
          <option value="staging">Staging</option>
          <option value="dev">Dev</option>
        </select>
        <select
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
          style={{ ...STYLES.input, width: 'auto', padding: '6px 10px', cursor: 'pointer' }}
        >
          <option value="all">All Status</option>
          <option value="needs_upgrade">Needs Upgrade</option>
          <option value="up_to_date">Up to Date</option>
          <option value="old">Old (post-switchover)</option>
        </select>
        {(filterEngine !== 'all' || filterEnv !== 'all' || filterStatus !== 'all') && (
          <span style={{ fontSize: 11, color: '#58a6ff' }}>
            Showing {filteredClusters.length} of {clusters.length}
            <span
              style={{ marginLeft: 8, cursor: 'pointer', color: '#8b949e', textDecoration: 'underline' }}
              onClick={() => { setFilterEngine('all'); setFilterEnv('all'); setFilterStatus('all') }}
            >Clear filters</span>
          </span>
        )}
      </div>

      {/* Table */}
      <div style={STYLES.card}>
        <table style={STYLES.table}>
          <thead>
            <tr>
              <th style={STYLES.th}>Cluster</th>
              <th style={STYLES.th}>Type</th>
              <th style={STYLES.th}>Engine</th>
              <th style={STYLES.th}>Version</th>
              <th style={STYLES.th}>Environment</th>
              <th style={STYLES.th}>Region</th>
              <th style={STYLES.th}>Instances</th>
              <th style={STYLES.th}>CVE Risk</th>
              <th style={STYLES.th}>Upgrade</th>
              <th style={STYLES.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredClusters.map(c => (
              <React.Fragment key={c.cluster_id}>
              <tr
                style={{ cursor: 'pointer', transition: 'background 0.1s', ...(selectedCluster?.cluster_id === c.cluster_id ? { background: '#1f6feb15' } : {}), ...(c.cluster_type === 'old_primary' ? { opacity: 0.7 } : {}) }}
                onClick={() => setSelectedCluster(selectedCluster?.cluster_id === c.cluster_id ? null : c)}
              >
                <td style={STYLES.td}>
                  <strong style={{ color: '#f0f6fc' }}>{c.cluster_id}</strong>
                  {c.source_cluster && <div style={{ fontSize: 11, color: '#8b949e', marginTop: 2 }}>{c.cluster_type === 'old_primary' ? 'Replaced by' : c.cluster_type === 'global_secondary' ? 'Primary' : 'Source'}: {c.source_cluster}</div>}
                </td>
                <td style={STYLES.td}>
                  {c.cluster_type === 'blue_green'
                    ? <span style={STYLES.badge('blue')}>Green</span>
                    : c.cluster_type === 'old_primary'
                    ? <span style={STYLES.badge('red')}>Old</span>
                    : c.cluster_type === 'global_secondary'
                    ? <span style={STYLES.badge('yellow')}>Global Reader</span>
                    : <span style={STYLES.badge('green')}>Primary</span>}
                </td>
                <td style={STYLES.td}>
                  <span style={STYLES.badge(c.engine_type === 'aurora-mysql' ? 'yellow' : 'blue')}>
                    {c.engine_type === 'aurora-mysql' ? 'MySQL' : 'PostgreSQL'}
                  </span>
                </td>
                <td style={STYLES.td}>
                  <code style={{ fontSize: 12 }}>{c.engine_version}</code>
                  {c.is_lts && <span style={{ ...STYLES.badge('green'), marginLeft: 6, fontSize: 9 }}>LTS</span>}
                </td>
                <td style={STYLES.td}><StatusBadge status={c.environment} /></td>
                <td style={STYLES.td}><span style={{ fontSize: 12 }}>{c.region}</span></td>
                <td style={STYLES.td}>{c.instance_count}</td>
                <td style={STYLES.td}>
                  {(() => {
                    const scanCluster = secScan?.clusters?.find(sc => sc.cluster_id === c.cluster_id)
                    if (!scanCluster || !scanCluster.cve_exposure?.length) return <span style={{ fontSize: 11, color: '#484f58' }}>—</span>
                    const level = scanCluster.urgency_level
                    return (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                        <span style={STYLES.badge(level === 'critical' ? 'red' : level === 'high' ? 'yellow' : 'blue')}>
                          {scanCluster.cve_exposure.length} CVE{scanCluster.cve_exposure.length > 1 ? 's' : ''}
                        </span>
                      </div>
                    )
                  })()}
                </td>
                <td style={STYLES.td}>
                  {c.upgrade_available
                    ? <>
                        <span style={STYLES.badge('yellow')}>{c.target_version}</span>
                        {c.target_is_lts && <span style={{ ...STYLES.badge('green'), marginLeft: 4, fontSize: 9 }}>LTS</span>}
                        {!c.target_is_lts && c.recommended_lts && (
                          <div style={{ fontSize: 10, color: '#58a6ff', marginTop: 3 }}>LTS target: {c.recommended_lts.version}</div>
                        )}
                      </>
                    : <span style={STYLES.badge('green')}>Latest</span>}
                </td>
                <td style={STYLES.td}>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {c.upgrade_available && c.cluster_type === 'primary' && (
                      <button style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }} onClick={(e) => { e.stopPropagation(); onStartUpgrade(c) }}>Upgrade</button>
                    )}
                    {c.upgrade_available && c.cluster_type === 'primary' && (
                      <button style={{ ...STYLES.btn('warning'), ...STYLES.btnSm }} onClick={(e) => { e.stopPropagation(); onStartUpgrade({ ...c, strategy: 'in_place' }) }}>In-Place</button>
                    )}
                    {c.upgrade_available && c.cluster_type === 'primary' && secScan?.clusters?.find(sc => sc.cluster_id === c.cluster_id)?.cve_exposure?.length > 0 && (
                      <button style={{ ...STYLES.btn('danger'), ...STYLES.btnSm }} onClick={(e) => {
                        e.stopPropagation()
                        const scanData = secScan.clusters.find(sc => sc.cluster_id === c.cluster_id)
                        const cveIds = (scanData?.cve_exposure || []).map(cve => cve.cve_id || cve.id || cve).filter(Boolean)
                        onStartUpgrade({ ...c, strategy: 'in_place', reason: 'cve_patch', cve_ids: cveIds })
                      }}>Patch CVE</button>
                    )}
                    {c.cluster_type === 'blue_green' && (
                      <button style={{ ...STYLES.btn('danger'), ...STYLES.btnSm }} onClick={(e) => { e.stopPropagation(); onDeleteBG(c) }}>Delete Green</button>
                    )}
                    {c.cluster_type === 'old_primary' && (
                      <button style={{ ...STYLES.btn('danger'), ...STYLES.btnSm }} onClick={(e) => { e.stopPropagation(); onDeleteOldCluster(c) }}>Delete Old</button>
                    )}
                  </div>
                </td>
              </tr>
              {selectedCluster?.cluster_id === c.cluster_id && (
                <tr><td colSpan={10} style={{ padding: 0 }}>
                  <ClusterDetailPanel cluster={c} onClose={() => setSelectedCluster(null)} />
                </td></tr>
              )}
              </React.Fragment>
            ))}
            {filteredClusters.length === 0 && clusters.length > 0 && (
              <tr><td style={{ ...STYLES.td, textAlign: 'center', color: '#8b949e', padding: 40 }} colSpan={10}>No clusters match the current filters.</td></tr>
            )}
            {clusters.length === 0 && (
              <tr><td style={{ ...STYLES.td, textAlign: 'center', color: '#8b949e', padding: 40 }} colSpan={10}>No clusters found. Click "Scan Fleet" to discover your Aurora clusters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Upgrade Progress
// ─────────────────────────────────────────────────────────────────
function UpgradeProgress({ upgrades, history, onSwitchover, onCancel, onRollback, onDeleteDeployment, onDiscover, onRefresh, onRetry, onDismiss, discoverLoading, refreshLoading, actionErrors = {}, cleanupStatus = {} }) {
  const [diagnosing, setDiagnosing] = useState({})
  const [diagnoses, setDiagnoses] = useState({})
  const [applying, setApplying] = useState({})
  const [applyResults, setApplyResults] = useState({})
  const [expandedHistory, setExpandedHistory] = useState({})

  const handleDiagnose = async (stepKey, error, clusterId, stepName) => {
    setDiagnosing(prev => ({ ...prev, [stepKey]: true }))
    try {
      const res = await fetch(`${API}/diagnose`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ error, cluster_id: clusterId, step: stepName }),
      })
      const text = await res.text()
      let data
      try { data = JSON.parse(text) } catch { data = { diagnosis: text.slice(0, 200), remediation: 'Could not parse response from server.', auto_fixable: false } }
      setDiagnoses(prev => ({ ...prev, [stepKey]: data }))
    } catch (e) {
      setDiagnoses(prev => ({ ...prev, [stepKey]: { diagnosis: 'Diagnosis request failed', remediation: e.message, auto_fixable: false } }))
    }
    setDiagnosing(prev => ({ ...prev, [stepKey]: false }))
  }

  const handleApplyFix = async (stepKey, error, clusterId, fixAction) => {
    setApplying(prev => ({ ...prev, [stepKey]: true }))
    try {
      const diag = diagnoses[stepKey] || {}
      const res = await fetch(`${API}/diagnose/apply-fix`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ error, cluster_id: clusterId, context: { fix_action: fixAction, diagnosis: diag.diagnosis || '' } }),
      })
      const data = await res.json()
      setApplyResults(prev => ({ ...prev, [stepKey]: data }))
    } catch (e) {
      setApplyResults(prev => ({ ...prev, [stepKey]: { status: 'error', message: e.message } }))
    }
    setApplying(prev => ({ ...prev, [stepKey]: false }))
  }

  const entries = Object.values(upgrades)

  const getProgress = (status) => {
    const map = { preflight: 15, provisioning: 35, ready: 60, awaiting_approval: 60, switching: 80, validating: 90, complete: 100, failed: 0, cancelled: 0 }
    return map[status] || 0
  }

  const getColor = (status) => {
    if (status === 'complete') return '#238636'
    if (status === 'failed' || status === 'cancelled') return '#da3633'
    return '#1f6feb'
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div>
          <h2 style={{ fontSize: 18, color: '#f0f6fc', margin: 0 }}>Upgrade Progress</h2>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: '#8b949e' }}>
            {entries.length} active upgrade{entries.length !== 1 ? 's' : ''} &middot; {history.length} in history
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button style={STYLES.btn()} onClick={onDiscover} disabled={discoverLoading}>
            {discoverLoading ? <><Spinner /> Discovering...</> : 'Discover from AWS'}
          </button>
          <button style={STYLES.btn('primary')} onClick={onRefresh} disabled={refreshLoading}>
            {refreshLoading ? <><Spinner /> Refreshing...</> : 'Refresh'}
          </button>
        </div>
      </div>

      {entries.length === 0 && (
        <div style={{ ...STYLES.card, textAlign: 'center', padding: 40 }}>
          <div style={{ fontSize: 14, color: '#8b949e', marginBottom: 8 }}>No active upgrades</div>
          <div style={{ fontSize: 12, color: '#484f58' }}>Start an upgrade from the Fleet tab, or click "Discover from AWS" to find existing Blue/Green deployments.</div>
        </div>
      )}

      {entries.map(u => (
        <div key={u.cluster_id} style={STYLES.card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <div>
              <strong style={{ color: '#f0f6fc', fontSize: 15 }}>{u.cluster_id}</strong>
              <span style={{ marginLeft: 12, fontSize: 12, color: '#8b949e' }}>&rarr; {u.target_version}</span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <StatusBadge status={u.status} />
              {(u.status === 'ready' || u.status === 'awaiting_approval') && (
                <button style={{ ...STYLES.btn('warning'), ...STYLES.btnSm }} onClick={() => onSwitchover(u.deployment_id, u.region)}>Switchover</button>
              )}
              {u.status === 'complete' && u.deployment_id && (
                <button style={{ ...STYLES.btn('danger'), ...STYLES.btnSm }} onClick={() => onRollback(u.deployment_id, u.region, u.cluster_id)}>Rollback</button>
              )}
              {u.deployment_id && !['cancelled', 'switching'].includes(u.status) && (
                <button style={{ ...STYLES.btn('danger'), ...STYLES.btnSm }} onClick={() => onDeleteDeployment(u.deployment_id, u.region, u.cluster_id)}>Delete</button>
              )}
              {!['complete', 'cancelled', 'failed', 'preflight_failed'].includes(u.status) && !u.deployment_id && (
                <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={() => onCancel(u.cluster_id)}>Cancel</button>
              )}
              {(u.status === 'failed' || u.status === 'preflight_failed') && (
                <button style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }} onClick={() => onRetry(u)}>Retry</button>
              )}
              {(u.status === 'failed' || u.status === 'preflight_failed') && (
                <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={() => onDismiss(u.cluster_id)}>Dismiss</button>
              )}
            </div>
          </div>

          <div style={STYLES.progressBar}>
            <div style={STYLES.progressFill(getProgress(u.status), getColor(u.status))} />
          </div>

          <div style={{ marginTop: 10, fontSize: 12, color: '#8b949e' }}>
            Started: {new Date(u.started_at).toLocaleString()}
            {u.error && <span style={{ color: '#f85149', marginLeft: 12 }}>Error: {u.error}</span>}
          </div>

          {u.steps && u.steps.length > 0 && (
            <details style={{ marginTop: 12 }} open={u.status === 'failed' || u.status === 'preflight_failed'}>
              <summary style={{ cursor: 'pointer', color: '#58a6ff', fontSize: 12 }}>
                {u.steps.length} steps {u.steps.filter(s => s.status === 'failed').length > 0 && <span style={{ color: '#f85149' }}>({u.steps.filter(s => s.status === 'failed').length} failed)</span>}
              </summary>
              <div style={{ ...STYLES.timeline, marginTop: 8 }}>
                {u.steps.map((step, i) => (
                  <div key={i} style={STYLES.timelineItem}>
                    <div style={STYLES.timelineDot(step.status === 'failed' ? '#da3633' : step.status === 'skipped' ? '#8b949e' : '#238636')} />
                    <div style={{ fontSize: 12 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span>
                          <strong>{step.step}</strong>
                          <span style={{ marginLeft: 8, ...STYLES.badge(step.status === 'failed' ? 'red' : step.status === 'skipped' ? 'gray' : 'green'), fontSize: 10, padding: '1px 6px' }}>{step.status}</span>
                        </span>
                        <span style={{ color: '#8b949e', fontSize: 11 }}>
                          {step.time && new Date(step.time).toLocaleTimeString()}
                          {step.duration_sec != null && ` (${step.duration_sec}s)`}
                        </span>
                      </div>
                      {step.error && (
                        <div style={{ marginTop: 6, padding: 8, background: '#1c0e0e', border: '1px solid #da363340', borderRadius: 4 }}>
                          <div style={{ fontSize: 11, color: '#f85149', wordBreak: 'break-word' }}>{step.error}</div>
                          {step.diagnosis && (
                            <div style={{ marginTop: 6, fontSize: 11 }}>
                              <div style={{ color: '#d29922' }}><strong>Diagnosis:</strong> {step.diagnosis.diagnosis}</div>
                              <div style={{ color: '#58a6ff', marginTop: 2 }}><strong>Fix:</strong> {step.diagnosis.remediation}</div>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      ))}

      {/* History */}
      {history.length > 0 && (
        <div style={{ marginTop: 32 }}>
          <div style={STYLES.sectionTitle}>Upgrade History</div>
          <div style={STYLES.card}>
            <table style={STYLES.table}>
              <thead>
                <tr>
                  <th style={STYLES.th}>Cluster</th>
                  <th style={STYLES.th}>Type</th>
                  <th style={STYLES.th}>Target</th>
                  <th style={STYLES.th}>Status</th>
                  <th style={STYLES.th}>Started</th>
                  <th style={STYLES.th}>Steps</th>
                  <th style={STYLES.th}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {history.slice().reverse().map((h, i) => (
                  <React.Fragment key={i}>
                  <tr>
                    <td style={STYLES.td}><strong>{h.cluster_id}</strong></td>
                    <td style={STYLES.td}>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                        <span style={STYLES.badge(h.reason === 'cve_patch' ? 'red' : 'blue')}>
                          {h.reason === 'cve_patch' ? 'CVE Patch' : 'Version Upgrade'}
                        </span>
                        <span style={{ ...STYLES.badge(h.strategy === 'in_place' ? 'yellow' : 'green'), fontSize: 9 }}>
                          {h.strategy === 'in_place' ? 'In-Place' : 'Blue/Green'}
                        </span>
                      </div>
                    </td>
                    <td style={STYLES.td}>
                      <code style={{ fontSize: 11 }}>{h.target_version || '—'}</code>
                      {h.cve_ids && h.cve_ids.length > 0 && (
                        <div style={{ fontSize: 10, color: '#f85149', marginTop: 2 }}>{h.cve_ids.length} CVE{h.cve_ids.length > 1 ? 's' : ''} fixed</div>
                      )}
                    </td>
                    <td style={STYLES.td}><StatusBadge status={h.status} /></td>
                    <td style={STYLES.td}><span style={{ fontSize: 11 }}>{h.started_at ? new Date(h.started_at).toLocaleString() : '—'}</span></td>
                    <td style={STYLES.td}>
                      {h.steps && h.steps.length > 0 ? (
                        <span style={{ fontSize: 11, color: '#8b949e' }}>
                          {h.steps.length} steps
                          {h.steps.filter(s => s.status === 'failed').length > 0 && (
                            <span style={{ color: '#f85149', marginLeft: 4 }}>({h.steps.filter(s => s.status === 'failed').length} failed)</span>
                          )}
                        </span>
                      ) : '—'}
                    </td>
                    <td style={STYLES.td}>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                        {/* Retry: available for failed upgrades */}
                        {(h.status === 'failed' || h.status === 'preflight_failed') && (
                          <button style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }} onClick={() => onRetry(h)}>Retry</button>
                        )}
                        {/* Rollback: available for completed B/G upgrades (not already rolled back/cancelled/cleaned) */}
                        {h.deployment_id && h.status === 'complete' && !h.cleaned_up && (
                          <button style={{ ...STYLES.btn('warning'), ...STYLES.btnSm }} onClick={() => onRollback(h.deployment_id, h.region, h.cluster_id)}>Rollback</button>
                        )}
                        {/* Cleanup: delete B/G deployment resources after switchover */}
                        {h.deployment_id && !h.cleaned_up && (h.status === 'complete' || h.status === 'failed') && h.status !== 'rolled_back' && h.status !== 'cancelled' && (
                          cleanupStatus[h.cluster_id] === 'in_progress' ? (
                            <span style={{ ...STYLES.badge('yellow'), fontSize: 10 }}><Spinner size={10} /> Cleaning up...</span>
                          ) : cleanupStatus[h.cluster_id] === 'complete' ? (
                            <span style={{ ...STYLES.badge('green'), fontSize: 10 }}>✓ Cleaned up</span>
                          ) : (
                            <button style={{ ...STYLES.btn('danger'), ...STYLES.btnSm }} onClick={() => onDeleteDeployment(h.deployment_id, h.region, h.cluster_id)}>Cleanup</button>
                          )
                        )}
                        {/* Already cleaned up */}
                        {h.cleaned_up && (
                          <span style={{ ...STYLES.badge('green'), fontSize: 9 }}>Cleaned up</span>
                        )}
                        {/* For in-place or completed without deployment_id — nothing to rollback to */}
                        {!h.deployment_id && h.status === 'complete' && h.strategy === 'in_place' && (
                          <span style={{ fontSize: 10, color: '#8b949e' }}>In-place (no rollback)</span>
                        )}
                      </div>
                      {/* Persistent action error (e.g., cleanup/rollback failed) */}
                      {actionErrors[h.cluster_id] && (
                        <div style={{ marginTop: 6, padding: 8, background: '#1c0e0e', border: '1px solid #da363340', borderRadius: 4 }}>
                          <div style={{ fontSize: 10, color: '#f85149', fontWeight: 600, marginBottom: 3 }}>
                            {actionErrors[h.cluster_id].action} failed:
                          </div>
                          <div style={{ fontSize: 11, color: '#f0f6fc', wordBreak: 'break-all', userSelect: 'text' }}>
                            {actionErrors[h.cluster_id].error}
                          </div>
                        </div>
                      )}
                    </td>
                  </tr>
                  {/* Expandable error/steps row for failed entries */}
                  {(h.status === 'failed' || h.status === 'preflight_failed' || h.status === 'validation_failed') && (
                    <tr>
                      <td colSpan={10} style={{ padding: '0 14px 4px', borderBottom: '1px solid #21262d' }}>
                        <div
                          style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', padding: '6px 0', fontSize: 11, color: '#8b949e' }}
                          onClick={() => setExpandedHistory(prev => ({ ...prev, [h.cluster_id + '-' + i]: !prev[h.cluster_id + '-' + i] }))}
                        >
                          <span style={{ transition: 'transform 0.2s', transform: expandedHistory[h.cluster_id + '-' + i] ? 'rotate(90deg)' : 'rotate(0deg)' }}>▶</span>
                          <span>{expandedHistory[h.cluster_id + '-' + i] ? 'Hide details' : 'Show error details'}</span>
                          {h.steps && h.steps.filter(s => s.status === 'failed').length > 0 && (
                            <span style={{ color: '#f85149' }}>({h.steps.filter(s => s.status === 'failed').length} failed step{h.steps.filter(s => s.status === 'failed').length > 1 ? 's' : ''})</span>
                          )}
                        </div>
                        {expandedHistory[h.cluster_id + '-' + i] && (
                        <div style={{ paddingBottom: 8 }}>
                        {/* Top-level error */}
                        {h.error && (
                          <div style={{ padding: 10, background: '#1c0e0e', border: '1px solid #da363330', borderRadius: 6, marginBottom: 8 }}>
                            <div style={{ fontSize: 12, color: '#f85149', fontWeight: 600, marginBottom: 4 }}>Error</div>
                            <div style={{ fontSize: 12, color: '#f0f6fc', wordBreak: 'break-word' }}>{h.error}</div>
                          </div>
                        )}
                        {/* Failed steps with AI diagnosis */}
                        {h.steps && h.steps.filter(s => s.status === 'failed').map((step, j) => {
                          const stepKey = `${h.cluster_id}-${i}-${j}`
                          const aiDiagnosis = diagnoses[stepKey]
                          const fixResult = applyResults[stepKey]
                          return (
                          <div key={j} style={{ padding: 10, background: '#161b22', border: '1px solid #30363d', borderRadius: 6, marginBottom: 6 }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                              <span style={{ fontSize: 12, fontWeight: 600, color: '#f0f6fc' }}>{step.step}</span>
                              <span style={{ fontSize: 10, color: '#8b949e' }}>
                                {step.time && new Date(step.time).toLocaleString()}
                                {step.duration_sec != null && ` (${step.duration_sec}s)`}
                              </span>
                            </div>
                            {step.error && (
                              <div style={{ fontSize: 11, color: '#f85149', marginBottom: 6, wordBreak: 'break-word' }}>{step.error}</div>
                            )}
                            {/* AI Diagnose button */}
                            {!aiDiagnosis && step.error && (
                              <button
                                style={{ ...STYLES.btn('warning'), ...STYLES.btnSm, marginBottom: 8 }}
                                onClick={() => handleDiagnose(stepKey, step.error, h.cluster_id, step.step)}
                                disabled={diagnosing[stepKey]}
                              >
                                {diagnosing[stepKey] ? <><Spinner /> Analyzing...</> : 'AI Diagnose'}
                              </button>
                            )}
                            {/* Show AI diagnosis result */}
                            {aiDiagnosis && (
                              <div style={{ padding: 10, background: '#0d1117', borderRadius: 6, border: '1px solid #1f6feb40' }}>
                                <div style={{ fontSize: 11, fontWeight: 600, color: '#58a6ff', marginBottom: 6 }}>AI Diagnosis</div>
                                <div style={{ fontSize: 12, color: '#d29922', marginBottom: 6 }}>
                                  <strong>Root Cause:</strong> {typeof aiDiagnosis.diagnosis === 'string' ? aiDiagnosis.diagnosis : JSON.stringify(aiDiagnosis.diagnosis)}
                                </div>
                                <div style={{ fontSize: 12, color: '#c9d1d9', marginBottom: 6, whiteSpace: 'pre-wrap' }}>
                                  <strong>Fix:</strong> {typeof aiDiagnosis.remediation === 'string' ? aiDiagnosis.remediation : JSON.stringify(aiDiagnosis.remediation, null, 2)}
                                </div>
                                {aiDiagnosis.fix_details && typeof aiDiagnosis.fix_details === 'object' && (
                                  <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 8 }}>
                                    {typeof aiDiagnosis.fix_details.description === 'string' ? aiDiagnosis.fix_details.description : ''}
                                  </div>
                                )}
                                {/* Apply Fix button */}
                                {aiDiagnosis.auto_fixable && aiDiagnosis.fix_action && !fixResult && (
                                  <button
                                    style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }}
                                    onClick={() => handleApplyFix(stepKey, step.error, h.cluster_id, aiDiagnosis.fix_action)}
                                    disabled={applying[stepKey]}
                                  >
                                    {applying[stepKey] ? <><Spinner /> Applying Fix...</> : `Apply Fix: ${aiDiagnosis.fix_details?.description || aiDiagnosis.fix_action}`}
                                  </button>
                                )}
                                {/* Fix result */}
                                {fixResult && (
                                  <div style={{ marginTop: 8, padding: 8, borderRadius: 4, background: fixResult.status === 'success' ? '#1a7f3715' : '#da363315', border: `1px solid ${fixResult.status === 'success' ? '#3fb95040' : '#f8514940'}` }}>
                                    <div style={{ fontSize: 11, color: fixResult.status === 'success' ? '#3fb950' : '#f85149', fontWeight: 600 }}>
                                      {fixResult.status === 'success' ? 'Fix Applied Successfully' : 'Fix Failed'}
                                    </div>
                                    <div style={{ fontSize: 11, color: '#c9d1d9', marginTop: 4 }}>{fixResult.message}</div>
                                    {fixResult.next_step && (
                                      <div style={{ fontSize: 11, color: '#58a6ff', marginTop: 4 }}>{fixResult.next_step}</div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )}
                            {/* Fallback: show static diagnosis if no AI diagnosis yet */}
                            {!aiDiagnosis && step.diagnosis && step.diagnosis.diagnosis !== 'Unknown error — manual investigation required.' && (
                              <div style={{ padding: 8, background: '#0d1117', borderRadius: 4, border: '1px solid #30363d' }}>
                                <div style={{ fontSize: 11, color: '#d29922', marginBottom: 4 }}>
                                  <strong>Diagnosis:</strong> {step.diagnosis.diagnosis}
                                </div>
                                <div style={{ fontSize: 11, color: '#58a6ff', marginBottom: step.diagnosis.auto_fixable && step.diagnosis.fix_action ? 8 : 0 }}>
                                  <strong>Fix:</strong> {step.diagnosis.remediation}
                                </div>
                                {step.diagnosis.auto_fixable && step.diagnosis.fix_action && !applyResults[stepKey] && (
                                  <button
                                    style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }}
                                    onClick={() => handleApplyFix(stepKey, step.error, h.cluster_id, step.diagnosis.fix_action)}
                                    disabled={applying[stepKey]}
                                  >
                                    {applying[stepKey] ? <><Spinner /> Applying Fix...</> : `Apply Fix: ${step.diagnosis.fix_details?.description || step.diagnosis.fix_action}`}
                                  </button>
                                )}
                                {applyResults[stepKey] && (
                                  <div style={{ marginTop: 6, padding: 6, borderRadius: 4, background: applyResults[stepKey].status === 'success' ? '#1a7f3715' : applyResults[stepKey].status === 'info' ? '#1f6feb15' : '#da363315', border: `1px solid ${applyResults[stepKey].status === 'success' ? '#3fb95040' : applyResults[stepKey].status === 'info' ? '#58a6ff40' : '#f8514940'}` }}>
                                    <div style={{ fontSize: 11, color: applyResults[stepKey].status === 'success' ? '#3fb950' : applyResults[stepKey].status === 'info' ? '#58a6ff' : '#f85149', fontWeight: 600 }}>
                                      {applyResults[stepKey].status === 'success' ? '✓ Fix Applied' : applyResults[stepKey].status === 'info' ? 'ℹ Manual Step Required' : '✗ Fix Failed'}
                                    </div>
                                    <div style={{ fontSize: 11, color: '#c9d1d9', marginTop: 3 }}>{applyResults[stepKey].message || applyResults[stepKey].next_step}</div>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                          )
                        })}
                        {/* Successful steps summary */}
                        {h.steps && h.steps.filter(s => s.status !== 'failed').length > 0 && (
                          <details style={{ marginTop: 4 }}>
                            <summary style={{ fontSize: 11, color: '#8b949e', cursor: 'pointer' }}>
                              {h.steps.filter(s => s.status !== 'failed').length} other step(s)
                            </summary>
                            <div style={{ ...STYLES.timeline, marginTop: 6 }}>
                              {h.steps.filter(s => s.status !== 'failed').map((step, j) => (
                                <div key={j} style={{ ...STYLES.timelineItem, marginBottom: 6 }}>
                                  <div style={STYLES.timelineDot(step.status === 'skipped' ? '#8b949e' : '#238636')} />
                                  <div style={{ fontSize: 11 }}>
                                    <strong>{step.step}</strong>
                                    <span style={{ marginLeft: 6, color: '#8b949e' }}>{step.status}</span>
                                    {step.duration_sec != null && <span style={{ color: '#484f58', marginLeft: 4 }}>({step.duration_sec}s)</span>}
                                  </div>
                                </div>
                              ))}
                            </div>
                          </details>
                        )}
                        </div>
                        )}
                      </td>
                    </tr>
                  )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// AI Advisor Panel
// ─────────────────────────────────────────────────────────────────
function AdvisorPanel({ clusters }) {
  const [selectedCluster, setSelectedCluster] = useState('')
  const [error, setError] = useState('')
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [mode, setMode] = useState('recommend')

  const handleSubmit = async () => {
    if (!selectedCluster) return
    setLoading(true)
    setResult(null)
    try {
      // Use the cluster's own Region (from the fleet scan), not a fixed one.
      const clusterRegion = (clusters.find(c => c.cluster_id === selectedCluster) || {}).region || ''
      const body = { cluster_id: selectedCluster, region: clusterRegion }
      if (mode === 'diagnose' && error) body.error = error
      if (mode === 'ask' && question) body.question = question
      const res = await fetch(`${API}/advisor/recommend`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(body),
      })
      setResult(await res.json())
    } catch (e) {
      setResult({ error: e.message })
    } finally {
      setLoading(false)
    }
  }

  const strategyLabels = { blue_green: 'Blue/Green Deployment', in_place: 'In-Place Upgrade', backup_restore: 'Backup & Restore' }
  const strategyColors = { blue_green: '#58a6ff', in_place: '#3fb950', backup_restore: '#d29922' }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div>
          <h2 style={{ fontSize: 18, color: '#f0f6fc', margin: 0 }}>AI Upgrade Advisor</h2>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: '#8b949e' }}>Powered by Amazon Bedrock (Claude)</p>
        </div>
      </div>

      <div style={STYLES.card}>
        {/* Mode tabs */}
        <div style={{ display: 'flex', gap: 4, marginBottom: 16, background: '#0d1117', borderRadius: 6, padding: 3 }}>
          {[
            { key: 'recommend', label: 'Strategy' },
            { key: 'diagnose', label: 'Diagnose Error' },
            { key: 'ask', label: 'Ask Question' },
          ].map(m => (
            <button key={m.key} style={{ ...STYLES.tab(mode === m.key), fontSize: 12, padding: '6px 14px' }} onClick={() => setMode(m.key)}>{m.label}</button>
          ))}
        </div>

        {/* Cluster select */}
        <div style={{ marginBottom: 12 }}>
          <select
            style={{ ...STYLES.input, width: '100%', padding: '10px 12px' }}
            value={selectedCluster}
            onChange={e => setSelectedCluster(e.target.value)}
          >
            <option value="">Select a cluster...</option>
            {clusters.map(c => (
              <option key={c.cluster_id} value={c.cluster_id}>
                {c.cluster_id} ({c.engine_version}) - {c.environment}
              </option>
            ))}
          </select>
        </div>

        {mode === 'diagnose' && (
          <textarea
            style={{ ...STYLES.input, width: '100%', height: 80, resize: 'vertical', marginBottom: 12, boxSizing: 'border-box' }}
            placeholder="Paste the error message..."
            value={error}
            onChange={e => setError(e.target.value)}
          />
        )}

        {mode === 'ask' && (
          <textarea
            style={{ ...STYLES.input, width: '100%', height: 80, resize: 'vertical', marginBottom: 12, boxSizing: 'border-box' }}
            placeholder="e.g., Should I use Blue/Green for this cluster?"
            value={question}
            onChange={e => setQuestion(e.target.value)}
          />
        )}

        <button
          style={{ ...STYLES.btn('primary'), ...(loading || !selectedCluster ? STYLES.btnDisabled : {}) }}
          disabled={loading || !selectedCluster}
          onClick={handleSubmit}
        >
          {loading ? <><Spinner /> Analyzing...</> : mode === 'recommend' ? 'Get Recommendation' : mode === 'diagnose' ? 'Diagnose' : 'Ask'}
        </button>
      </div>

      {/* Results */}
      {result && (
        <div style={{ ...STYLES.card, marginTop: 16 }}>
          {result.error && !result.recommended_strategy && (
            <div style={{ color: '#f85149', fontSize: 13 }}>Error: {result.error}</div>
          )}

          {result.recommended_strategy && (
            <>
              <div style={{ marginBottom: 16 }}>
                <span style={{ fontSize: 11, color: '#8b949e', textTransform: 'uppercase' }}>Recommended</span>
                <h3 style={{ margin: '4px 0 0', color: strategyColors[result.recommended_strategy] || '#f0f6fc', fontSize: 18 }}>
                  {strategyLabels[result.recommended_strategy] || result.recommended_strategy}
                </h3>
              </div>
              <div style={{ fontSize: 13, color: '#c9d1d9', marginBottom: 16, lineHeight: 1.6 }}>{result.reasoning}</div>
              {result.steps && result.steps.length > 0 && (
                <ol style={{ margin: '0 0 16px', paddingLeft: 20, fontSize: 12, color: '#c9d1d9', lineHeight: 2 }}>
                  {result.steps.map((s, i) => <li key={i}>{s}</li>)}
                </ol>
              )}
              {result.risks && result.risks.length > 0 && (
                <div style={{ marginBottom: 12, padding: 10, background: '#da363310', borderRadius: 6 }}>
                  <div style={{ fontSize: 11, color: '#f85149', fontWeight: 600, marginBottom: 4 }}>Risks</div>
                  {result.risks.map((r, i) => <div key={i} style={{ fontSize: 12, color: '#f0f6fc', padding: '2px 0' }}>&#x2022; {r}</div>)}
                </div>
              )}
            </>
          )}

          {result.root_cause && (
            <>
              <div style={{ marginBottom: 12 }}>
                <span style={STYLES.badge(result.auto_fixable ? 'green' : 'yellow')}>
                  {result.auto_fixable ? 'Auto-fixable' : 'Manual fix needed'}
                </span>
              </div>
              <div style={{ fontSize: 14, color: '#f0f6fc', marginBottom: 12 }}>{result.root_cause}</div>
              {result.fix_steps && result.fix_steps.map((step, i) => (
                <div key={i} style={{ marginBottom: 8, padding: 10, background: '#0d1117', borderRadius: 6 }}>
                  <div style={{ fontSize: 12, color: '#c9d1d9' }}><strong>Step {step.step}:</strong> {step.action}</div>
                  {step.command && <code style={{ display: 'block', marginTop: 4, padding: 6, background: '#161b22', borderRadius: 4, fontSize: 11, color: '#58a6ff' }}>{step.command}</code>}
                </div>
              ))}
            </>
          )}

          {result.answer && !result.recommended_strategy && !result.root_cause && (
            <div style={{ fontSize: 13, color: '#c9d1d9', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>{result.answer}</div>
          )}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Maintenance Windows
// ─────────────────────────────────────────────────────────────────
function MaintenanceWindows({ windows, clusters, onSave }) {
  const [local, setLocal] = useState(windows)
  const [clusterInput, setClusterInput] = useState({})
  const [saving, setSaving] = useState(null)
  const days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

  useEffect(() => { setLocal(windows) }, [windows])

  const update = (env, field, value) => {
    setLocal(prev => ({ ...prev, [env]: { ...prev[env], [field]: value } }))
  }

  const toggleDay = (env, day) => {
    setLocal(prev => {
      const current = prev[env].days || []
      const updated = current.includes(day) ? current.filter(d => d !== day) : [...current, day]
      return { ...prev, [env]: { ...prev[env], days: updated } }
    })
  }

  const toggleCluster = (env, clusterId) => {
    setLocal(prev => {
      const current = prev[env].clusters || []
      const updated = current.includes(clusterId) ? current.filter(c => c !== clusterId) : [...current, clusterId]
      return { ...prev, [env]: { ...prev[env], clusters: updated } }
    })
  }

  const addClusterManual = (env) => {
    const val = (clusterInput[env] || '').trim()
    if (!val) return
    setLocal(prev => {
      const current = prev[env].clusters || []
      if (current.includes(val)) return prev
      return { ...prev, [env]: { ...prev[env], clusters: [...current, val] } }
    })
    setClusterInput(prev => ({ ...prev, [env]: '' }))
  }

  const handleSave = async (env) => {
    setSaving(env)
    await onSave(env, local[env])
    setSaving(null)
  }

  const allClusterIds = clusters.map(c => c.cluster_id)

  return (
    <div>
      <h2 style={{ fontSize: 18, color: '#f0f6fc', marginBottom: 20 }}>Maintenance Windows</h2>
      {Object.entries(local).map(([env, config]) => (
        <div key={env} style={STYLES.card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <h3 style={{ margin: 0, color: '#f0f6fc', textTransform: 'capitalize', fontSize: 16 }}>{env}</h3>
            <label style={{ fontSize: 12, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 8 }}>
              <input type="checkbox" checked={config.enabled} onChange={e => update(env, 'enabled', e.target.checked)} />
              Enforce window
            </label>
          </div>

          <div style={{ display: 'flex', gap: 20, alignItems: 'center', marginBottom: 14 }}>
            <label style={{ fontSize: 13, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 8 }}>
              Start (UTC):
              <input type="number" min={0} max={23} style={STYLES.input} value={config.start_hour_utc} onChange={e => update(env, 'start_hour_utc', parseInt(e.target.value))} />
            </label>
            <label style={{ fontSize: 13, color: '#8b949e', display: 'flex', alignItems: 'center', gap: 8 }}>
              End (UTC):
              <input type="number" min={0} max={24} style={STYLES.input} value={config.end_hour_utc} onChange={e => update(env, 'end_hour_utc', parseInt(e.target.value))} />
            </label>
          </div>

          <div style={{ display: 'flex', gap: 4, marginBottom: 14 }}>
            {days.map(day => (
              <button
                key={day}
                style={{ ...STYLES.btn(config.days?.includes(day) ? 'primary' : undefined), ...STYLES.btnSm, textTransform: 'uppercase', minWidth: 42 }}
                onClick={() => toggleDay(env, day)}
              >{day}</button>
            ))}
          </div>

          {/* Cluster assignment */}
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12, color: '#8b949e', marginBottom: 8 }}>
              Assigned Clusters {!(config.clusters?.length) && <span style={{ color: '#484f58' }}>(empty = all {env} clusters)</span>}
            </div>
            {allClusterIds.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                {allClusterIds.map(cid => (
                  <button
                    key={cid}
                    style={{ ...STYLES.btn((config.clusters || []).includes(cid) ? 'primary' : undefined), ...STYLES.btnSm, borderRadius: 12, fontSize: 11 }}
                    onClick={() => toggleCluster(env, cid)}
                  >{cid}</button>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <input
                type="text"
                placeholder="Add cluster ID..."
                style={{ ...STYLES.input, width: 220 }}
                value={clusterInput[env] || ''}
                onChange={e => setClusterInput(prev => ({ ...prev, [env]: e.target.value }))}
                onKeyDown={e => e.key === 'Enter' && addClusterManual(env)}
              />
              <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={() => addClusterManual(env)}>Add</button>
            </div>
            {(config.clusters || []).length > 0 && (
              <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                {config.clusters.map(cid => (
                  <span key={cid} style={{ ...STYLES.badge('blue'), display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                    {cid}
                    <span style={{ cursor: 'pointer', opacity: 0.7 }} onClick={() => toggleCluster(env, cid)}>&times;</span>
                  </span>
                ))}
              </div>
            )}
          </div>

          <button style={STYLES.btn('primary')} onClick={() => handleSave(env)} disabled={saving === env}>
            {saving === env ? <><Spinner size={10} /> Saving...</> : 'Save'}
          </button>
        </div>
      ))}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Executive Dashboard
// ─────────────────────────────────────────────────────────────────
function Dashboard({ clusters, upgrades, history, onNavigate, onStartUpgrade, secScan, secLoading, onRescan }) {
  const runSecurityScan = onRescan

  const total = clusters.length
  const upgradeAvailable = clusters.filter(c => c.upgrade_available).length
  const upToDate = total - upgradeAvailable
  const patchCompliance = total > 0 ? Math.round((upToDate / total) * 100) : 100

  const envGroups = clusters.reduce((acc, c) => {
    const env = c.environment || 'unknown'
    if (!acc[env]) acc[env] = { total: 0, patched: 0, needsUpgrade: 0 }
    acc[env].total++
    if (c.upgrade_available) acc[env].needsUpgrade++
    else acc[env].patched++
    return acc
  }, {})

  const activeUpgrades = Object.values(upgrades)
  const recentHistory = history.slice(-5).reverse()
  const successCount = history.filter(h => h.status === 'complete').length
  const failCount = history.filter(h => h.status === 'failed' || h.status === 'preflight_failed').length
  const successRate = (successCount + failCount) > 0 ? Math.round((successCount / (successCount + failCount)) * 100) : 100

  const complianceColor = patchCompliance >= 90 ? '#3fb950' : patchCompliance >= 70 ? '#d29922' : '#f85149'
  const successColor = successRate >= 90 ? '#3fb950' : successRate >= 70 ? '#d29922' : '#f85149'

  return (
    <div>
      {/* Hero metrics */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 24 }}>
        <div style={{ ...STYLES.card, textAlign: 'center', padding: 24 }}>
          <div style={{ fontSize: 36, fontWeight: 700, color: complianceColor }}>{patchCompliance}%</div>
          <div style={{ fontSize: 13, color: '#8b949e', marginTop: 4 }}>Patch Compliance</div>
        </div>
        <div style={{ ...STYLES.card, textAlign: 'center', padding: 24 }}>
          <div style={{ fontSize: 36, fontWeight: 700, color: '#f0f6fc' }}>{total}</div>
          <div style={{ fontSize: 13, color: '#8b949e', marginTop: 4 }}>Total Clusters</div>
        </div>
        <div style={{ ...STYLES.card, textAlign: 'center', padding: 24 }}>
          <div style={{ fontSize: 36, fontWeight: 700, color: upgradeAvailable > 0 ? '#d29922' : '#3fb950' }}>{upgradeAvailable}</div>
          <div style={{ fontSize: 13, color: '#8b949e', marginTop: 4 }}>Pending Upgrades</div>
        </div>
        <div style={{ ...STYLES.card, textAlign: 'center', padding: 24 }}>
          <div style={{ fontSize: 36, fontWeight: 700, color: successColor }}>{successRate}%</div>
          <div style={{ fontSize: 13, color: '#8b949e', marginTop: 4 }}>Upgrade Success Rate</div>
        </div>
      </div>

      {/* Environment breakdown */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24 }}>
        <div style={STYLES.card}>
          <div style={{ fontSize: 14, fontWeight: 600, color: '#f0f6fc', marginBottom: 16 }}>Compliance by Environment</div>
          {Object.entries(envGroups).sort(([a], [b]) => {
            const order = { production: 0, staging: 1, dev: 2 }
            return (order[a] ?? 3) - (order[b] ?? 3)
          }).map(([env, data]) => {
            const pct = data.total > 0 ? Math.round((data.patched / data.total) * 100) : 100
            const barColor = env === 'production' ? '#f85149' : env === 'staging' ? '#d29922' : '#3fb950'
            return (
              <div key={env} style={{ marginBottom: 14 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                  <span style={{ fontSize: 13, color: '#c9d1d9', textTransform: 'capitalize' }}>{env}</span>
                  <span style={{ fontSize: 12, color: '#8b949e' }}>{data.patched}/{data.total} patched</span>
                </div>
                <div style={{ ...STYLES.progressBar, height: 8 }}>
                  <div style={{ ...STYLES.progressFill(pct, barColor), height: '100%' }} />
                </div>
              </div>
            )
          })}
        </div>

        <div style={STYLES.card}>
          <div style={{ fontSize: 14, fontWeight: 600, color: '#f0f6fc', marginBottom: 16 }}>Active Upgrades</div>
          {activeUpgrades.length === 0 ? (
            <div style={{ fontSize: 13, color: '#8b949e', padding: '20px 0', textAlign: 'center' }}>No upgrades in progress</div>
          ) : (
            activeUpgrades.map(u => (
              <div key={u.cluster_id} style={{ marginBottom: 12, padding: 12, background: '#21262d', borderRadius: 8 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontSize: 13, color: '#f0f6fc', fontWeight: 500 }}>{u.cluster_id}</span>
                  <StatusBadge status={u.status} />
                </div>
                <div style={{ fontSize: 11, color: '#8b949e', marginTop: 4 }}>Target: {u.target_version}</div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Security Posture */}
      {secScan && !secScan.error && (
        <div style={{ ...STYLES.card, marginBottom: 24, borderColor: secScan.critical_count > 0 ? '#f8514940' : '#30363d' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: '#f0f6fc' }}>Security &amp; CVE Posture</div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: '#8b949e' }}>Sources: {secScan.cve_sources?.merged_count || 0} CVEs tracked</span>
              <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={runSecurityScan} disabled={secLoading}>
                {secLoading ? <><Spinner size={10} /> Scanning...</> : 'Rescan'}
              </button>
            </div>
          </div>

          {/* Urgency summary bar */}
          <div style={{ display: 'flex', gap: 16, marginBottom: 20 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#f85149' }} />
              <span style={{ fontSize: 13, color: '#f0f6fc' }}><strong>{secScan.critical_count}</strong> Critical</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#d29922' }} />
              <span style={{ fontSize: 13, color: '#f0f6fc' }}><strong>{secScan.high_count}</strong> High</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ width: 12, height: 12, borderRadius: '50%', background: '#3fb950' }} />
              <span style={{ fontSize: 13, color: '#f0f6fc' }}><strong>{secScan.total_clusters - secScan.critical_count - secScan.high_count}</strong> Low/Medium</span>
            </div>
          </div>

          {/* Fleet summary headline */}
          {secScan.fleet_summary && (
            <div style={{ padding: 12, background: secScan.fleet_summary.action_needed ? '#da363310' : '#1a7f3710', borderRadius: 8, marginBottom: 16, borderLeft: `3px solid ${secScan.fleet_summary.action_needed ? '#f85149' : '#3fb950'}` }}>
              <div style={{ fontSize: 13, color: '#f0f6fc', fontWeight: 500 }}>{secScan.fleet_summary.headline}</div>
              {secScan.fleet_summary.most_pressing_risk && (
                <div style={{ fontSize: 12, color: '#c9d1d9', marginTop: 6 }}>{secScan.fleet_summary.most_pressing_risk}</div>
              )}
              {secScan.fleet_summary.recommendations && secScan.fleet_summary.recommendations.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  {secScan.fleet_summary.recommendations.slice(0, 3).map((r, i) => (
                    <div key={i} style={{ fontSize: 12, color: '#8b949e', padding: '2px 0' }}>&#x2022; {r}</div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* CVE exposure table — critical and high only */}
          {secScan.clusters && secScan.clusters.filter(c => c.urgency_level === 'critical' || c.urgency_level === 'high').length > 0 && (
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: '#8b949e', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 8 }}>Exposed Clusters</div>
              <table style={{ ...STYLES.table }}>
                <thead>
                  <tr>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>Cluster</th>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>Version</th>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>Environment</th>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>Urgency</th>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>CVEs</th>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>Key Risk</th>
                    <th style={{ ...STYLES.th, fontSize: 10 }}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {secScan.clusters.filter(c => c.urgency_level === 'critical' || c.urgency_level === 'high').map(c => {
                    const fleetCluster = clusters.find(fc => fc.cluster_id === c.cluster_id)
                    return (
                    <tr key={c.cluster_id}>
                      <td style={{ ...STYLES.td, fontSize: 12 }}><strong style={{ color: '#f0f6fc' }}>{c.cluster_id}</strong></td>
                      <td style={{ ...STYLES.td, fontSize: 12 }}><code>{c.current_version}</code> &rarr; <span style={{ color: '#3fb950' }}>{c.target_version}</span></td>
                      <td style={STYLES.td}><StatusBadge status={c.environment} /></td>
                      <td style={STYLES.td}>
                        <span style={STYLES.badge(c.urgency_level === 'critical' ? 'red' : 'yellow')}>
                          {c.urgency_level} ({c.urgency_score})
                        </span>
                      </td>
                      <td style={{ ...STYLES.td, fontSize: 11 }}>
                        {c.cve_exposure?.slice(0, 2).map((cve, i) => (
                          <span key={i} style={{ display: 'inline-block', marginRight: 4, padding: '1px 6px', borderRadius: 4, fontSize: 10, background: cve.severity === 'HIGH' ? '#da363320' : '#9e6a0320', color: cve.severity === 'HIGH' ? '#f85149' : '#d29922' }}>
                            {cve.cve_id}
                          </span>
                        ))}
                        {(c.cve_exposure?.length || 0) > 2 && <span style={{ fontSize: 10, color: '#8b949e' }}>+{c.cve_exposure.length - 2}</span>}
                      </td>
                      <td style={{ ...STYLES.td, fontSize: 11, color: '#c9d1d9' }}>{c.reasons?.[0]?.split(':')[1]?.trim() || c.reasons?.[0] || ''}</td>
                      <td style={STYLES.td}>
                        {fleetCluster && fleetCluster.upgrade_available && fleetCluster.cluster_type === 'primary' ? (
                          <button style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }} onClick={() => {
                            const cveIds = (c.cve_exposure || []).map(cve => cve.cve_id || cve.id || cve).filter(Boolean)
                            onStartUpgrade({ ...fleetCluster, strategy: 'in_place', reason: 'cve_patch', cve_ids: cveIds })
                          }}>Patch</button>
                        ) : (
                          <span style={{ fontSize: 10, color: '#484f58' }}>—</span>
                        )}
                      </td>
                    </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
      {secLoading && !secScan && (
        <div style={{ ...STYLES.card, textAlign: 'center', padding: 30, marginBottom: 24 }}>
          <Spinner /> <span style={{ color: '#8b949e' }}>Running security scan...</span>
        </div>
      )}

      {/* Clusters needing attention + recent activity */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div style={STYLES.card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: '#f0f6fc' }}>Needs Attention</div>
            <button style={{ ...STYLES.btn('primary'), ...STYLES.btnSm }} onClick={() => onNavigate('fleet')}>View Fleet</button>
          </div>
          {clusters.filter(c => c.upgrade_available).length === 0 ? (
            <div style={{ fontSize: 13, color: '#3fb950', padding: '20px 0', textAlign: 'center' }}>All clusters are up to date</div>
          ) : (
            clusters.filter(c => c.upgrade_available).slice(0, 6).map(c => (
              <div key={c.cluster_id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #21262d' }}>
                <div>
                  <span style={{ fontSize: 13, color: '#f0f6fc' }}>{c.cluster_id}</span>
                  <span style={{ fontSize: 11, color: '#8b949e', marginLeft: 8 }}>{c.engine_version}</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 11, color: '#d29922' }}>{c.target_version}</span>
                  <StatusBadge status={c.environment} />
                </div>
              </div>
            ))
          )}
        </div>

        <div style={STYLES.card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: '#f0f6fc' }}>Recent Activity</div>
            <button style={{ ...STYLES.btn(), ...STYLES.btnSm }} onClick={() => onNavigate('progress')}>View All</button>
          </div>
          {recentHistory.length === 0 ? (
            <div style={{ fontSize: 13, color: '#8b949e', padding: '20px 0', textAlign: 'center' }}>No upgrade history yet</div>
          ) : (
            recentHistory.map((h, i) => (
              <div key={i} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #21262d' }}>
                <div>
                  <span style={{ fontSize: 13, color: '#f0f6fc' }}>{h.cluster_id}</span>
                  <span style={{ fontSize: 11, color: '#8b949e', marginLeft: 8 }}>&rarr; {h.target_version}</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <StatusBadge status={h.status} />
                  <span style={{ fontSize: 10, color: '#484f58' }}>{h.completed_at ? new Date(h.completed_at).toLocaleDateString() : ''}</span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Main App
// ─────────────────────────────────────────────────────────────────
export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [clusters, setClusters] = useState([])
  const [upgrades, setUpgrades] = useState({})
  const [history, setHistory] = useState([])
  const [windows, setWindows] = useState({})
  const [scanning, setScanning] = useState(false)
  const [lastScan, setLastScan] = useState(null)
  const [guardrailCluster, setGuardrailCluster] = useState(null)
  const [toast, setToast] = useState(null)
  const [discoverLoading, setDiscoverLoading] = useState(false)
  const [refreshLoading, setRefreshLoading] = useState(false)
  const [currentModel, setCurrentModel] = useState('')
  const [availableModels, setAvailableModels] = useState([])
  const [modelChanging, setModelChanging] = useState(false)
  const [secScan, setSecScan] = useState(null)
  const [secLoading, setSecLoading] = useState(false)
  const [actionErrors, setActionErrors] = useState({})
  const [cleanupStatus, setCleanupStatus] = useState({})

  const showToast = (message, type = 'success') => setToast({ message, type })

  const fetchModel = useCallback(async () => {
    try {
      const res = await fetch(`${API}/settings/model`)
      const data = await res.json()
      setCurrentModel(data.current_model || '')
      setAvailableModels(data.available_models || [])
    } catch (e) { /* ignore on initial load */ }
  }, [])

  const handleModelChange = async (modelId) => {
    setModelChanging(true)
    try {
      const res = await fetch(`${API}/settings/model`, {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify({ model_id: modelId }),
      })
      if (!res.ok) throw new Error(await res.text())
      setCurrentModel(modelId)
      const name = availableModels.find(m => m.id === modelId)?.name || modelId
      showToast(`Model switched to ${name}`)
    } catch (e) {
      showToast(`Failed to switch model: ${e.message}`, 'error')
    } finally {
      setModelChanging(false)
    }
  }

  const fetchFleet = useCallback(async () => {
    try {
      const res = await fetch(`${API}/fleet`)
      const data = await res.json()
      setClusters(data.clusters || [])
      setLastScan(data.last_scan)
    } catch (e) {
      showToast(`Failed to load fleet: ${e.message}`, 'error')
    }
  }, [])

  const upgradesLoaded = useRef(false)
  const fetchUpgrades = useCallback(async () => {
    try {
      const res = await fetch(`${API}/upgrades`)
      const data = await res.json()
      setUpgrades(data.active || data)
      setHistory(data.history || [])
      upgradesLoaded.current = true
    } catch (e) {
      showToast(`Failed to load upgrades: ${e.message}`, 'error')
    }
  }, [])

  const fetchWindows = useCallback(async () => {
    try {
      const res = await fetch(`${API}/windows`)
      setWindows(await res.json())
    } catch (e) {
      showToast(`Failed to load windows: ${e.message}`, 'error')
    }
  }, [])

  const runSecurityScan = useCallback(async () => {
    setSecLoading(true)
    try {
      const res = await fetch(`${API}/agent/scan`, { method: 'POST', headers: authHeaders() })
      setSecScan(await res.json())
    } catch (e) { setSecScan({ error: e.message }) }
    setSecLoading(false)
  }, [])

  useEffect(() => {
    fetchFleet()
    fetchUpgrades()
    fetchWindows()
    fetchModel()
    const interval = setInterval(fetchUpgrades, 10000)
    return () => clearInterval(interval)
  }, [fetchFleet, fetchUpgrades, fetchWindows, fetchModel])

  useEffect(() => { if (clusters.length > 0 && !secScan) runSecurityScan() }, [clusters.length])

  // When an upgrade finishes, the backend records the new engine version in
  // the fleet. Reload the fleet and rerun the security scan so the patched
  // cluster's CVE badge and Patch CVE button clear without a manual rescan.
  const completedRuns = useRef(null)
  useEffect(() => {
    if (!upgradesLoaded.current) return // ignore the empty initial state
    const done = history.filter(h => h.status === 'complete').length
    const previous = completedRuns.current
    completedRuns.current = done
    if (previous !== null && done > previous) {
      fetchFleet().then(() => runSecurityScan())
    }
  }, [history, fetchFleet, runSecurityScan])

  const handleScan = async () => {
    setScanning(true)
    try {
      // [] = let the backend use its configured SCAN_REGIONS (or its own Region).
      const res = await fetch(`${API}/fleet/scan`, { method: 'POST', headers: authHeaders(), body: JSON.stringify([]) })
      if (!res.ok) throw new Error(`Scan failed: ${res.status}`)
      const data = await res.json()
      setClusters(data.clusters || [])
      setLastScan(data.last_scan)
      showToast(`Found ${(data.clusters || []).length} clusters`)
      // Versions may have changed (e.g. after an upgrade): refresh the CVE
      // badges and Patch CVE buttons, which come from the security scan.
      runSecurityScan()
    } catch (e) {
      showToast(`Scan failed: ${e.message}`, 'error')
    } finally {
      setScanning(false)
    }
  }

  const handleUpgradeClick = (cluster) => {
    setGuardrailCluster(cluster)
  }

  // The confirm() calls below (retry, switchover, cancel, rollback, delete
  // deployment, delete cluster) are intentional, not leftover debug code.
  // Each gates a high-impact, hard-to-undo action against a real Aurora
  // cluster — switchover, permanent cluster deletion, rollback — so a native
  // blocking confirmation is used deliberately as a last-line-of-defense
  // against a misclick, on top of the backend's own guardrails. This is a
  // sample/reference tool (see README's non-production disclaimer); a
  // production fork should likely replace these with a proper modal that
  // also surfaces impact details, but the safety intent should stay.
  const handleRetryUpgrade = async (historyItem) => {
    if (!confirm(`Retry upgrade for ${historyItem.cluster_id} → ${historyItem.target_version}?`)) return
    try {
      const res = await fetch(`${API}/upgrades/start`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          cluster_id: historyItem.cluster_id,
          target_version: historyItem.target_version,
          region: historyItem.region || (clusters.find(c => c.cluster_id === historyItem.cluster_id) || {}).region || '',
          environment: historyItem.environment || 'dev',
          strategy: historyItem.strategy || 'blue_green',
          reason: historyItem.reason || 'retry',
          cve_ids: historyItem.cve_ids || [],
          skip_preflight: false,
          skip_window_check: true,
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      showToast(`Retry started for ${historyItem.cluster_id}`)
      setTab('progress')
      // Fetch immediately, then again after 2s and 5s to catch the state update
      await fetchUpgrades()
      setTimeout(fetchUpgrades, 2000)
      setTimeout(fetchUpgrades, 5000)
    } catch (e) {
      showToast(`Retry failed: ${e.message}`, 'error')
    }
  }

  const handleDismissUpgrade = async (clusterId) => {
    try {
      await fetch(`${API}/upgrades/${clusterId}/cancel`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ dismiss: true }),
      })
      await fetchUpgrades()
    } catch (e) {
      showToast(`Dismiss failed: ${e.message}`, 'error')
    }
  }

  const handleProceedUpgrade = async (cluster, skipWindow) => {
    setGuardrailCluster(null)
    try {
      const res = await fetch(`${API}/upgrades/start`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
          cluster_id: cluster.cluster_id,
          target_version: cluster.target_version,
          region: cluster.region,
          environment: cluster.environment,
          strategy: cluster.strategy || 'blue_green',
          reason: cluster.reason || 'version_upgrade',
          cve_ids: cluster.cve_ids || [],
          skip_preflight: true,
          skip_window_check: skipWindow,
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      showToast(`Upgrade started for ${cluster.cluster_id}`)
      setTab('progress')
      setTimeout(fetchUpgrades, 1000)
    } catch (e) {
      showToast(`Failed to start upgrade: ${e.message}`, 'error')
    }
  }

  const handleSwitchover = async (deploymentId, region) => {
    if (!confirm('Execute switchover? The upgraded cluster becomes the new primary.')) return
    try {
      const res = await fetch(`${API}/upgrades/switchover`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ deployment_id: deploymentId, region }) })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `HTTP ${res.status}`)
      }
      showToast('Switchover initiated')
      setTimeout(fetchUpgrades, 2000)
    } catch (e) {
      showToast(`Switchover failed: ${e.message}`, 'error')
    }
  }

  const handleCancel = async (clusterId) => {
    if (!confirm(`Cancel upgrade for ${clusterId}?`)) return
    try {
      await fetch(`${API}/upgrades/${clusterId}/cancel`, { method: 'POST', headers: authHeaders() })
      showToast(`Upgrade cancelled for ${clusterId}`)
      setTimeout(fetchUpgrades, 1000)
    } catch (e) {
      showToast(`Cancel failed: ${e.message}`, 'error')
    }
  }

  const handleRollback = async (deploymentId, region, clusterId) => {
    if (!confirm(`Rollback ${clusterId}? This switches back to the original cluster.`)) return
    try {
      await fetch(`${API}/upgrades/rollback`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ deployment_id: deploymentId, region, cluster_id: clusterId }) })
      showToast(`Rollback initiated for ${clusterId}`)
      setTimeout(fetchUpgrades, 2000)
    } catch (e) {
      showToast(`Rollback failed: ${e.message}`, 'error')
    }
  }

  const handleDeleteDeployment = async (deploymentId, region, clusterId) => {
    if (!confirm(`Cleanup Blue/Green deployment resources for ${clusterId}?\n\nThis removes the deployment metadata and old resources from AWS.`)) return
    setCleanupStatus(prev => ({ ...prev, [clusterId]: 'in_progress' }))
    try {
      const resp = await fetch(`${API}/deployments/delete`, {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ deployment_id: deploymentId, region, cluster_id: clusterId }),
      })
      const result = await resp.json()
      if (result.status === 'error') {
        setCleanupStatus(prev => ({ ...prev, [clusterId]: 'failed' }))
        setActionErrors(prev => ({ ...prev, [clusterId]: { action: 'cleanup', error: result.message, time: new Date().toISOString() } }))
        return
      }
      setCleanupStatus(prev => ({ ...prev, [clusterId]: 'complete' }))
      setActionErrors(prev => { const n = { ...prev }; delete n[clusterId]; return n })
      showToast(`Deployment cleanup complete for ${clusterId}`)
      await fetchUpgrades()
    } catch (e) {
      setCleanupStatus(prev => ({ ...prev, [clusterId]: 'failed' }))
      setActionErrors(prev => ({ ...prev, [clusterId]: { action: 'cleanup', error: e.message, time: new Date().toISOString() } }))
    }
  }

  const handleDeleteBGFromFleet = async (cluster) => {
    if (!confirm(`Delete Green cluster "${cluster.cluster_id}"?\n\nThe source cluster "${cluster.source_cluster}" will remain unchanged.`)) return
    try {
      const resp = await fetch(`${API}/deployments/delete`, {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ cluster_id: cluster.cluster_id, region: cluster.region, delete_target: true }),
      })
      const result = await resp.json()
      if (result.status === 'error') throw new Error(result.message)
      showToast(`Green cluster deletion initiated`)
      handleScan()
    } catch (e) {
      showToast(`Delete failed: ${e.message}`, 'error')
    }
  }

  const handleDeleteOldCluster = async (cluster) => {
    if (!confirm(`Permanently delete old cluster "${cluster.cluster_id}"?\n\nA final snapshot will be taken. This cannot be undone.`)) return
    try {
      const resp = await fetch(`${API}/clusters/${cluster.cluster_id}/delete`, {
        method: 'POST', headers: authHeaders(),
        // confirm_cluster_id echoes the cluster id to satisfy the backend's
        // server-side confirmation gate (not just this UI dialog). Final
        // snapshot is left on (skip_final_snapshot defaults to false).
        body: JSON.stringify({ region: cluster.region, confirm_cluster_id: cluster.cluster_id }),
      })
      const result = await resp.json()
      if (result.status === 'error') throw new Error(result.message)
      showToast(`Old cluster deletion initiated`)
      handleScan()
    } catch (e) {
      showToast(`Delete failed: ${e.message}`, 'error')
    }
  }

  const handleDiscover = async () => {
    setDiscoverLoading(true)
    try {
      // [] = let the backend use its configured SCAN_REGIONS (or its own Region).
      const res = await fetch(`${API}/deployments/discover`, { method: 'POST', headers: authHeaders(), body: JSON.stringify([]) })
      if (!res.ok) throw new Error(`Discovery failed: ${res.status}`)
      const data = await res.json()
      showToast(`Discovered ${data.total || 0} deployment(s) from AWS`)
      await fetchUpgrades()
    } catch (e) {
      showToast(`Discovery failed: ${e.message}`, 'error')
    } finally {
      setDiscoverLoading(false)
    }
  }

  const handleRefresh = async () => {
    setRefreshLoading(true)
    try {
      await fetchUpgrades()
      showToast('Upgrades refreshed')
    } catch (e) {
      showToast(`Refresh failed: ${e.message}`, 'error')
    } finally {
      setRefreshLoading(false)
    }
  }

  const handleSaveWindow = async (env, config) => {
    try {
      await fetch(`${API}/windows/${env}`, { method: 'PUT', headers: authHeaders(), body: JSON.stringify(config) })
      showToast(`${env} window saved`)
      fetchWindows()
    } catch (e) {
      showToast(`Failed to save: ${e.message}`, 'error')
    }
  }

  return (
    <div style={STYLES.app}>
      {/* CSS for animations */}
      <style>{`
        @keyframes spin { to { transform: rotate(360deg) } }
        @keyframes slideUp { from { transform: translateY(20px); opacity: 0 } to { transform: translateY(0); opacity: 1 } }
        tr:hover { background: #1f6feb08 !important }
        details > summary { list-style: none }
        details > summary::-webkit-details-marker { display: none }
        details > summary::before { content: '\\25B8 '; color: #8b949e; }
        details[open] > summary::before { content: '\\25BE '; }
      `}</style>

      <header style={STYLES.header}>
        <div>
          <div style={STYLES.title}>Aurora Patch Agent</div>
          <div style={STYLES.subtitle}>
            Fleet Patching & Upgrade Management
            {lastScan && <span> &middot; Last scan: {new Date(lastScan).toLocaleString()}</span>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          {availableModels.length > 0 && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 11, color: '#8b949e' }}>Model:</span>
              <select
                value={currentModel}
                onChange={(e) => handleModelChange(e.target.value)}
                disabled={modelChanging}
                style={{ ...STYLES.input, width: 'auto', padding: '4px 8px', fontSize: 12, opacity: modelChanging ? 0.6 : 1 }}
              >
                {availableModels.map(m => (
                  <option key={m.id} value={m.id}>{m.name} ({m.tier})</option>
                ))}
              </select>
            </div>
          )}
          <span style={STYLES.badge('green')}>Agent Active</span>
        </div>
      </header>

      <nav style={STYLES.tabs}>
        {[
          { key: 'dashboard', label: 'Dashboard' },
          { key: 'fleet', label: 'Fleet' },
          { key: 'progress', label: `Upgrades${Object.keys(upgrades).length > 0 ? ` (${Object.keys(upgrades).length})` : ''}` },
          { key: 'windows', label: 'Maintenance' },
          { key: 'advisor', label: 'AI Advisor' },
        ].map(t => (
          <button key={t.key} style={STYLES.tab(tab === t.key)} onClick={() => setTab(t.key)}>{t.label}</button>
        ))}
      </nav>

      {tab === 'dashboard' && <Dashboard clusters={clusters} upgrades={upgrades} history={history} onNavigate={setTab} onStartUpgrade={handleUpgradeClick} secScan={secScan} secLoading={secLoading} onRescan={runSecurityScan} />}
      {tab === 'fleet' && <FleetView clusters={clusters} onScan={handleScan} onStartUpgrade={handleUpgradeClick} scanning={scanning} onDeleteBG={handleDeleteBGFromFleet} onDeleteOldCluster={handleDeleteOldCluster} secScan={secScan} />}
      {tab === 'progress' && <UpgradeProgress upgrades={upgrades} history={history} onSwitchover={handleSwitchover} onCancel={handleCancel} onRollback={handleRollback} onDeleteDeployment={handleDeleteDeployment} onDiscover={handleDiscover} onRefresh={handleRefresh} onRetry={handleRetryUpgrade} onDismiss={handleDismissUpgrade} discoverLoading={discoverLoading} refreshLoading={refreshLoading} actionErrors={actionErrors} cleanupStatus={cleanupStatus} />}
      {tab === 'windows' && <MaintenanceWindows windows={windows} clusters={clusters} onSave={handleSaveWindow} />}
      {tab === 'advisor' && <AdvisorPanel clusters={clusters} />}

      {guardrailCluster && (
        <GuardrailsModal
          cluster={guardrailCluster}
          onClose={() => setGuardrailCluster(null)}
          onProceed={handleProceedUpgrade}
        />
      )}

      {toast && <Toast message={toast.message} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  )
}
