import { useEffect, useRef, useState } from 'react'
import { Check, CheckCircle2, Link2, Radio, Sparkles, Upload, Video, X } from 'lucide-react'
import { detectVideoUrlPlatform, extractTwitchLogin } from '../api'
import type {
  Channel,
  VideoAnalyzeUploadParams,
  VideoAnalyzeUrlRequest,
  VodProgressEvent,
} from '../types'

interface VodScanModalProps {
  open: boolean
  channels: Channel[]
  activeJob: VodProgressEvent | null
  onClose: () => void
  onAnalyzeUrl: (payload: VideoAnalyzeUrlRequest) => Promise<string>
  onAnalyzeUpload: (params: VideoAnalyzeUploadParams) => Promise<string>
  onMonitorLiveChannel: (login: string) => Promise<void>
}

type AnalyzeTab = 'url' | 'upload'
type ClipLengthPreset = 'quick' | 'standard' | 'story' | 'long' | 'extended'

const ALLOWED_EXTENSIONS = ['.mp4', '.mkv', '.mov', '.webm', '.m4v']

function getLengthBounds(preset: ClipLengthPreset): { min: number; max: number } {
  if (preset === 'quick') return { min: 15, max: 35 }
  if (preset === 'story') return { min: 40, max: 75 }
  if (preset === 'long') return { min: 60, max: 90 }
  if (preset === 'extended') return { min: 60, max: 120 }
  return { min: 20, max: 55 }
}

function isSupportedVideoFile(file: File): boolean {
  const lower = file.name.toLowerCase()
  return ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext))
}

function formatMegabytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function VodScanModal({
  open,
  channels,
  activeJob,
  onClose,
  onAnalyzeUrl,
  onAnalyzeUpload,
  onMonitorLiveChannel,
}: VodScanModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null)
  const urlInputRef = useRef<HTMLInputElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const [tab, setTab] = useState<AnalyzeTab>('url')
  const [videoUrl, setVideoUrl] = useState('')
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [channelId, setChannelId] = useState<string>('')
  const [maxClips, setMaxClips] = useState('5')
  const [lengthPreset, setLengthPreset] = useState<ClipLengthPreset>('standard')
  const [viralThreshold, setViralThreshold] = useState('50')
  const [useSpeechAnalysis, setUseSpeechAnalysis] = useState(true)
  const [touched, setTouched] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [monitoringLive, setMonitoringLive] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submittedJobId, setSubmittedJobId] = useState<string | null>(null)
  const [dismissedJobId, setDismissedJobId] = useState<string | null>(null)

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

  const trimmedUrl = videoUrl.trim()
  const detectedPlatform = detectVideoUrlPlatform(trimmedUrl)
  const isUrlValid = detectedPlatform !== null
  const detectedLiveLogin = !isUrlValid && trimmedUrl ? extractTwitchLogin(trimmedUrl) : null
  const invalidUrl = tab === 'url' && touched && !isUrlValid && !detectedLiveLogin

  const urlHelpError = invalidUrl
    ? 'Enter an HTTPS YouTube URL (youtube.com/watch?v=... or youtu.be/...) or Twitch VOD URL (twitch.tv/videos/...).'
    : null
  const displayError = error ?? urlHelpError

  const handleMonitorDetectedLive = async () => {
    if (!detectedLiveLogin || monitoringLive) return
    setError(null)
    setMonitoringLive(true)
    try {
      await onMonitorLiveChannel(detectedLiveLogin)
      setVideoUrl('')
      setTouched(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to start live monitoring.')
    } finally {
      setMonitoringLive(false)
    }
  }

  const acceptCandidateFile = (candidate: File | null) => {
    if (!candidate) return
    if (!isSupportedVideoFile(candidate)) {
      setSelectedFile(null)
      setError('Unsupported file format. Choose .mp4, .mkv, .mov, .webm, or .m4v.')
      return
    }
    setSelectedFile(candidate)
    setDismissedJobId(activeJob?.job_id ?? null)
    setError(null)
  }

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setTouched(true)
    setError(null)

    if (tab === 'url' && detectedLiveLogin) {
      await handleMonitorDetectedLive()
      return
    }

    const maxVal = Math.round(Number(maxClips))
    const thVal = Number(viralThreshold)
    if (!Number.isFinite(maxVal) || maxVal < 1 || maxVal > 20) {
      setError('Max clips must be between 1 and 20.')
      return
    }
    if (!Number.isFinite(thVal) || thVal < 10 || thVal > 100) {
      setError('Min viral score must be between 10 and 100.')
      return
    }

    const bounds = getLengthBounds(lengthPreset)
    const parsedChannelId = channelId ? Number(channelId) : null

    if (tab === 'url') {
      if (!isUrlValid) {
        setError('Only HTTPS YouTube or Twitch VOD URLs are supported.')
        urlInputRef.current?.focus()
        return
      }
      setSubmitting(true)
      try {
        const jobId = await onAnalyzeUrl({
          video_url: trimmedUrl,
          channel_id: parsedChannelId,
          max_clips: maxVal,
          min_clip_sec: bounds.min,
          max_clip_sec: bounds.max,
          viral_threshold: thVal,
          use_speech_analysis: useSpeechAnalysis,
        })
        setSubmittedJobId(jobId)
        setDismissedJobId(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to start video URL analysis.')
      } finally {
        setSubmitting(false)
      }
      return
    }

    if (!selectedFile) {
      setError('Select or drop a recorded video file first.')
      return
    }

    setSubmitting(true)
    try {
      const jobId = await onAnalyzeUpload({
        file: selectedFile,
        channel_id: parsedChannelId,
        max_clips: maxVal,
        min_clip_sec: bounds.min,
        max_clip_sec: bounds.max,
        viral_threshold: thVal,
        use_speech_analysis: useSpeechAnalysis,
      })
      setSubmittedJobId(jobId)
      setDismissedJobId(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to upload and analyze video.')
    } finally {
      setSubmitting(false)
    }
  }

  const visibleProgress =
    activeJob &&
    activeJob.job_id !== dismissedJobId &&
    (!submittedJobId || activeJob.job_id === submittedJobId)
      ? activeJob
      : null
  const rawProgress = visibleProgress?.progress ?? 0
  const pct = Math.max(0, Math.min(100, Math.round(rawProgress * (rawProgress <= 1 ? 100 : 1))))
  const isJobFailed = Boolean(visibleProgress?.error || visibleProgress?.status === 'failed')
  const isJobDone = Boolean(
    visibleProgress &&
      !isJobFailed &&
      (visibleProgress.status === 'completed' || visibleProgress.stage === 'complete' || pct >= 100),
  )
  const isJobRunning = Boolean(visibleProgress && !isJobFailed && !isJobDone)
  const busy = submitting || isJobRunning

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="analyze-video-title"
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
            <Sparkles size={18} className="text-accent" aria-hidden="true" />
            <h2 id="analyze-video-title" className="section-title text-ink">
              Analyze Video
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close video analyzer"
            className="btn btn--compact btn--quiet"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div role="tablist" aria-label="Video input mode" className="grid grid-cols-2 gap-2">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'url'}
            disabled={submitting}
            onClick={() => {
              setTab('url')
              setError(null)
            }}
            className={`btn ${tab === 'url' ? 'btn--accent-outline' : 'btn--quiet'}`}
          >
            <Link2 size={15} aria-hidden="true" />
            <span>Video URL</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'upload'}
            disabled={submitting}
            onClick={() => {
              setTab('upload')
              setError(null)
            }}
            className={`btn ${tab === 'upload' ? 'btn--accent-outline' : 'btn--quiet'}`}
          >
            <Upload size={15} aria-hidden="true" />
            <span>Upload Video</span>
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
          {tab === 'url' ? (
            <div>
              <div className="flex items-center justify-between gap-2 mb-1">
                <label htmlFor="analyze-url-input" className="block text-sm text-ink-2">
                  YouTube or Twitch VOD URL
                </label>
                {detectedPlatform && (
                  <span className="px-2 py-0.5 border border-accent text-accent text-xs font-medium">
                    {detectedPlatform === 'youtube' ? 'YouTube' : 'Twitch VOD'}
                  </span>
                )}
              </div>
              <input
                ref={urlInputRef}
                id="analyze-url-input"
                name="video_url"
                type="url"
                placeholder="https://www.youtube.com/watch?v=... or https://www.twitch.tv/videos/..."
                value={videoUrl}
                onChange={(e) => {
                  setVideoUrl(e.target.value)
                  if (isJobDone && activeJob) setDismissedJobId(activeJob.job_id)
                  setError(null)
                }}
                onBlur={() => setTouched(true)}
                aria-required="true"
                aria-invalid={Boolean(displayError)}
                aria-describedby="analyze-url-help"
                disabled={submitting}
                className="field-input"
              />
              <p
                id="analyze-url-help"
                aria-live="polite"
                className={`field-help ${displayError ? 'field-help--error' : ''}`}
              >
                {displayError ??
                  'Scans the full video for viral hooks, loudness bursts, reactions, and visual motion.'}
              </p>

              {detectedLiveLogin && (
                <div className="mt-3 p-3 border border-accent bg-paper flex flex-col gap-3">
                  <p className="text-sm text-ink">
                    This is a live Twitch channel ({detectedLiveLogin}), not a recorded video URL.
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
                    <span>{monitoringLive ? 'Starting Live' : 'Add Live Monitor'}</span>
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div>
              <span className="block text-sm text-ink-2 mb-1">
                Local Video File (.mp4, .mkv, .mov, .webm, .m4v)
              </span>
              <input
                ref={fileInputRef}
                id="analyze-file-input"
                type="file"
                accept=".mp4,.mkv,.mov,.webm,.m4v"
                disabled={submitting}
                onChange={(e) => acceptCandidateFile(e.target.files?.[0] ?? null)}
                className="sr-only"
              />
              <div
                onDragOver={(e) => {
                  e.preventDefault()
                  if (!submitting) setDragOver(true)
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault()
                  setDragOver(false)
                  if (!submitting) {
                    acceptCandidateFile(e.dataTransfer.files?.[0] ?? null)
                  }
                }}
                className={`p-4 border border-dashed flex flex-col gap-3 items-start ${
                  dragOver ? 'border-accent bg-paper-2' : 'border-rule bg-paper'
                }`}
              >
                <div className="flex items-center justify-between gap-3 w-full flex-wrap">
                  <div className="min-w-0">
                    {selectedFile ? (
                      <>
                        <p className="text-sm font-medium text-ink truncate">{selectedFile.name}</p>
                        <p className="text-xs text-muted num mt-0.5">
                          {formatMegabytes(selectedFile.size)}
                        </p>
                      </>
                    ) : (
                      <p className="text-sm text-muted">
                        Drag and drop a recorded video here, or browse from disk.
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      type="button"
                      disabled={submitting}
                      onClick={() => fileInputRef.current?.click()}
                      className="btn btn--quiet btn--compact"
                    >
                      <Video size={14} aria-hidden="true" />
                      <span>Choose Video File</span>
                    </button>
                    {selectedFile && (
                      <button
                        type="button"
                        disabled={submitting}
                        onClick={() => {
                          setSelectedFile(null)
                          if (fileInputRef.current) fileInputRef.current.value = ''
                        }}
                        className="btn btn--quiet btn--compact"
                      >
                        Clear
                      </button>
                    )}
                  </div>
                </div>
              </div>
              {displayError && (
                <p className="field-help field-help--error" role="alert">
                  {displayError}
                </p>
              )}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label htmlFor="analyze-max-clips" className="block text-sm text-ink-2 mb-1">
                Max clips
              </label>
              <input
                id="analyze-max-clips"
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
              <label htmlFor="analyze-length-select" className="block text-sm text-ink-2 mb-1">
                Clip length
              </label>
              <select
                id="analyze-length-select"
                value={lengthPreset}
                onChange={(e) => setLengthPreset(e.target.value as ClipLengthPreset)}
                disabled={submitting}
                className="field-select"
              >
                <option value="quick">15-35s (Quick)</option>
                <option value="standard">20-55s (Standard)</option>
                <option value="story">40-75s (Story)</option>
                <option value="long">60-90s (60s+ Viral)</option>
                <option value="extended">60-120s (Long Form)</option>
              </select>
            </div>

            <div>
              <label htmlFor="analyze-threshold" className="block text-sm text-ink-2 mb-1">
                Min viral score
              </label>
              <input
                id="analyze-threshold"
                type="number"
                min={10}
                max={100}
                value={viralThreshold}
                onChange={(e) => setViralThreshold(e.target.value)}
                disabled={submitting}
                className="field-input num"
              />
            </div>

            <div>
              <label htmlFor="analyze-channel-select" className="block text-sm text-ink-2 mb-1">
                Channel preset
              </label>
              <select
                id="analyze-channel-select"
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
          </div>

          <label className="flex items-center gap-2 text-sm text-ink-2 cursor-pointer">
            <input
              type="checkbox"
              checked={useSpeechAnalysis}
              disabled={submitting}
              onChange={(e) => setUseSpeechAnalysis(e.target.checked)}
              className="accent-current"
            />
            <span>Analyze speech &amp; viral hooks with local Whisper captions</span>
          </label>

          {visibleProgress && (
            <div
              className={`p-3 border bg-paper flex flex-col gap-2 ${
                isJobDone ? 'border-success' : isJobFailed ? 'border-error' : 'border-rule'
              }`}
            >
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="font-medium text-ink">
                  {visibleProgress.message || visibleProgress.stage || 'Analyzing video'}
                </span>
                {isJobDone ? (
                  <span className="flex items-center gap-1.5 num text-success font-medium shrink-0">
                    <CheckCircle2 size={15} aria-hidden="true" />
                    <span>Done · 100%</span>
                  </span>
                ) : (
                  <span className="num text-accent font-medium shrink-0">{pct}%</span>
                )}
              </div>
              <div
                role="progressbar"
                aria-label="Video analysis progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
                className="h-2 w-full bg-paper-2 border border-rule overflow-hidden"
              >
                <div
                  className={
                    isJobFailed
                      ? 'h-full bg-error'
                      : isJobDone
                        ? 'h-full bg-success'
                        : 'h-full bg-accent'
                  }
                  style={{ width: `${pct}%` }}
                />
              </div>
              {visibleProgress.clips_found != null && (
                <p className={`text-xs num ${isJobDone ? 'text-success font-medium' : 'text-muted'}`}>
                  {visibleProgress.clips_found} viral clip(s) ready in Review Queue.
                </p>
              )}
              {isJobFailed && (
                <p className="text-xs text-error" role="alert">
                  {visibleProgress.error || visibleProgress.message || 'Video analysis failed.'}
                </p>
              )}
            </div>
          )}

          <div className="flex items-center justify-end gap-3 pt-2 border-t border-rule">
            {isJobDone ? (
              <>
                <button
                  type="button"
                  onClick={() => {
                    if (visibleProgress) setDismissedJobId(visibleProgress.job_id)
                  }}
                  className="btn btn--quiet"
                >
                  Analyze Another
                </button>
                <button
                  type="button"
                  onClick={onClose}
                  className="btn btn--primary"
                >
                  <Check size={15} aria-hidden="true" />
                  <span>Done</span>
                </button>
              </>
            ) : (
              <>
                <button type="button" onClick={onClose} className="btn btn--quiet">
                  Close
                </button>
                <button
                  type="submit"
                  disabled={busy}
                  aria-busy={busy}
                  className="btn btn--primary"
                >
                  {busy && <span className="desk-spinner" aria-hidden="true" />}
                  <span>{busy ? 'Analyzing Video' : 'Find Viral Clips'}</span>
                </button>
              </>
            )}
          </div>
        </form>
      </div>
    </dialog>
  )
}
