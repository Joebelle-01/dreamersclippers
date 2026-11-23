import { useEffect, useRef, useState } from 'react'
import { Radio, Video, X } from 'lucide-react'
import { extractTwitchLogin, isValidTwitchVodUrl } from '../api'
import type { Channel, VodProgressEvent, VodScanRequest } from '../types'

interface VodScanModalProps {
  open: boolean
  channels: Channel[]
  activeJob: VodProgressEvent | null
  onClose: () => void
  onSubmitScan: (payload: VodScanRequest) => Promise<string>
  onMonitorLiveChannel: (login: string) => Promise<void>
}

export function VodScanModal({
  open,
  channels,
  activeJob,
  onClose,
  onSubmitScan,
  onMonitorLiveChannel,
}: VodScanModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null)
  const urlInputRef = useRef<HTMLInputElement | null>(null)

  const [vodUrl, setVodUrl] = useState('')
  const [channelId, setChannelId] = useState<string>('')
  const [maxClips, setMaxClips] = useState('5')
  const [hypeThreshold, setHypeThreshold] = useState('65')
  const [touched, setTouched] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [monitoringLive, setMonitoringLive] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submittedJobId, setSubmittedJobId] = useState<string | null>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (open && !dialog.open) {
      dialog.showModal()
      window.requestAnimationFrame(() => urlInputRef.current?.focus())
    } else if (!open && dialog.open) {
      dialog.close()
    }
  }, [open])

  const trimmedUrl = vodUrl.trim()
  const isVodValid = isValidTwitchVodUrl(trimmedUrl)
  const detectedLiveLogin = !isVodValid && trimmedUrl ? extractTwitchLogin(trimmedUrl) : null
  const invalidUrl = touched && !isVodValid && !detectedLiveLogin
  const displayError =
    error ?? (invalidUrl ? 'Enter an HTTPS URL like https://www.twitch.tv/videos/123456789.' : null)

  const handleMonitorDetectedLive = async () => {
    if (!detectedLiveLogin || monitoringLive) return
    setError(null)
    setMonitoringLive(true)
    try {
      await onMonitorLiveChannel(detectedLiveLogin)
      setVodUrl('')
      setTouched(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start live monitoring.')
    } finally {
      setMonitoringLive(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setTouched(true)
    setError(null)

    if (detectedLiveLogin) {
      await handleMonitorDetectedLive()
      return
    }

    if (!isVodValid) {
      setError('Only https://twitch.tv/videos/<id> URLs are permitted for VOD scanning.')
      urlInputRef.current?.focus()
      return
    }
    const maxVal = Math.round(Number(maxClips))
    const thVal = Number(hypeThreshold)
    if (!Number.isFinite(maxVal) || maxVal < 1 || maxVal > 20) {
      setError('Max clips must be between 1 and 20.')
      return
    }
    if (!Number.isFinite(thVal) || thVal < 10 || thVal > 100) {
      setError('Hype threshold must be between 10 and 100.')
      return
    }

    setSubmitting(true)
    try {
      const jobId = await onSubmitScan({
        vod_url: trimmedUrl,
        channel_id: channelId ? Number(channelId) : null,
        max_clips: maxVal,
        hype_threshold: thVal,
      })
      setSubmittedJobId(jobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start VOD scan.')
    } finally {
      setSubmitting(false)
    }
  }

  const visibleProgress =
    activeJob && (!submittedJobId || activeJob.job_id === submittedJobId) ? activeJob : null
  const rawProgress = visibleProgress?.progress ?? 0
  const pct = Math.max(0, Math.min(100, Math.round(rawProgress * (rawProgress <= 1 ? 100 : 1))))

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="vod-scan-title"
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
      onClick={(e) => {
        if (e.target === dialogRef.current) onClose()
      }}
      className="darkroom-dialog"
    >
      <div className="p-5 flex flex-col gap-4">
        <div className="flex items-center justify-between gap-4 pb-3 border-b border-rule">
          <div className="flex items-center gap-2">
            <Video size={18} className="text-accent" aria-hidden="true" />
            <h2 id="vod-scan-title" className="section-title text-ink">
              Scan Twitch VOD
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close VOD scanner"
            className="btn btn--compact btn--quiet"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
          <div>
            <label htmlFor="vod-url-input" className="block text-sm text-ink-2 mb-1">
              Twitch VOD URL
            </label>
            <input
              ref={urlInputRef}
              id="vod-url-input"
              name="vod_url"
              type="url"
              placeholder="https://www.twitch.tv/videos/123456789"
              value={vodUrl}
              onChange={(e) => {
                setVodUrl(e.target.value)
                setError(null)
              }}
              onBlur={() => setTouched(true)}
              aria-required="true"
              aria-invalid={Boolean(displayError)}
              aria-describedby="vod-url-help"
              disabled={submitting}
              className="field-input"
            />
            <p
              id="vod-url-help"
              aria-live="polite"
              className={`field-help ${displayError ? 'field-help--error' : ''}`}
            >
              {displayError ?? 'Extracts top audio, motion, and chat replay peaks directly to Review Queue.'}
            </p>
          </div>

          {detectedLiveLogin && (
            <div className="p-3 border border-accent bg-paper flex flex-col gap-3">
              <p className="text-sm text-ink">
                This is a live Twitch channel ({detectedLiveLogin}), not a VOD URL.
              </p>
              <button
                type="button"
                disabled={monitoringLive || submitting}
                aria-busy={monitoringLive}
                onClick={() => void handleMonitorDetectedLive()}
                className="btn btn--primary w-full"
              >
                {monitoringLive ? (
                  <span className="desk-spinner" aria-hidden="true" />
                ) : (
                  <Radio size={16} aria-hidden="true" />
                )}
                <span>
                  {monitoringLive
                    ? `Starting ${detectedLiveLogin}`
                    : `Add and Monitor ${detectedLiveLogin} Live`}
                </span>
              </button>
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label htmlFor="vod-channel-select" className="block text-sm text-ink-2 mb-1">
                Channel preset
              </label>
              <select
                id="vod-channel-select"
                value={channelId}
                onChange={(e) => setChannelId(e.target.value)}
                disabled={submitting}
                className="field-select"
              >
                <option value="">Auto-detect</option>
                {channels.map((ch) => (
                  <option key={ch.id} value={String(ch.id)}>
                    {ch.display_name || ch.channel_login}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="vod-max-clips" className="block text-sm text-ink-2 mb-1">
                Max clips
              </label>
              <input
                id="vod-max-clips"
                type="number"
                min={1}
                max={20}
                value={maxClips}
                onChange={(e) => setMaxClips(e.target.value)}
                disabled={submitting}
                className="field-input num"
              />
            </div>

            <div>
              <label htmlFor="vod-threshold" className="block text-sm text-ink-2 mb-1">
                Hype threshold
              </label>
              <input
                id="vod-threshold"
                type="number"
                min={10}
                max={100}
                value={hypeThreshold}
                onChange={(e) => setHypeThreshold(e.target.value)}
                disabled={submitting}
                className="field-input num"
              />
            </div>
          </div>

          {visibleProgress && (
            <div className="p-3 border border-rule bg-paper flex flex-col gap-2">
              <div className="flex items-center justify-between text-sm">
                <span className="font-medium text-ink">
                  {visibleProgress.message || visibleProgress.stage || 'Scanning VOD'}
                </span>
                <span className="num text-accent font-medium">{pct}%</span>
              </div>
              <div
                role="progressbar"
                aria-label="VOD scan progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                className="h-2 w-full bg-paper-2 border border-rule overflow-hidden"
              >
                <div
                  className={
                    visibleProgress.error || visibleProgress.status === 'failed'
                      ? 'h-full bg-error'
                      : 'h-full bg-accent'
                  }
                  style={{ width: `${pct}%` }}
                />
              </div>
              {visibleProgress.clips_found != null && (
                <p className="text-xs text-muted num">
                  {visibleProgress.clips_found} clips extracted to Review Queue.
                </p>
              )}
              {(visibleProgress.error || visibleProgress.status === 'failed') && (
                <p className="text-xs text-error" role="alert">
                  {visibleProgress.error || visibleProgress.message || 'VOD scan failed.'}
                </p>
              )}
            </div>
          )}

          <div className="flex items-center justify-end gap-3 pt-2 border-t border-rule">
            <button type="button" onClick={onClose} className="btn btn--quiet">
              Close
            </button>
            <button
              type="submit"
              disabled={submitting}
              aria-busy={submitting}
              className="btn btn--primary"
            >
              {submitting && <span className="desk-spinner" aria-hidden="true" />}
              <span>{submitting ? 'Scanning VOD' : 'Start VOD Scan'}</span>
            </button>
          </div>
        </form>
      </div>
    </dialog>
  )
}
