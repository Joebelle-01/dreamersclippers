import { useState } from 'react'
import {
  Bookmark,
  Clapperboard,
  Folder,
  FolderOpen,
  Play,
  Plus,
  Scissors,
  SlidersHorizontal,
  Square,
  Trash2,
} from 'lucide-react'
import { extractTwitchLogin } from '../api'
import type {
  Channel,
  ChannelCreateInput,
  ChannelMetadataEvent,
  ChannelUpdateInput,
  ClipFolder,
  HypeTickEvent,
  PublishClipResponse,
  StreamMarkerResponse,
  TwitchStatus,
} from '../types'

interface ChannelRailProps {
  channels: Channel[]
  loading: boolean
  error: string | null
  selectedChannelId: number | null
  latestTicks: Record<number, HypeTickEvent>
  metadataByChannel: Record<number, ChannelMetadataEvent>
  twitchStatus: TwitchStatus | null
  clipFolders: ClipFolder[]
  activeFolderKey: string
  onSelectFolder: (folderKey: string) => void
  onSelectChannel: (channelId: number) => void
  onRetry: () => void
  onCreateChannel: (payload: ChannelCreateInput, startImmediately?: boolean) => Promise<void>
  onUpdateChannel: (channelId: number, payload: ChannelUpdateInput) => Promise<void>
  onDeleteChannel: (channelId: number) => Promise<void>
  onStartMonitor: (channelId: number) => Promise<void>
  onStopMonitor: (channelId: number) => Promise<void>
  onClipNow: (channelId: number, durationSec?: number) => Promise<void>
  onPublishClip: (channelId: number) => Promise<PublishClipResponse>
  onCreateMarker: (channelId: number) => Promise<StreamMarkerResponse>
}

function defaultClipNowSec(ch: Channel): number {
  if (ch.pre_roll_sec >= 180) return 180
  if (ch.pre_roll_sec >= 120) return 120
  if (ch.pre_roll_sec >= 90) return 90
  return 60
}

export function ChannelRail({
  channels,
  loading,
  error,
  selectedChannelId,
  latestTicks,
  metadataByChannel,
  twitchStatus,
  clipFolders,
  activeFolderKey,
  onSelectFolder,
  onSelectChannel,
  onRetry,
  onCreateChannel,
  onUpdateChannel,
  onDeleteChannel,
  onStartMonitor,
  onStopMonitor,
  onClipNow,
  onPublishClip,
  onCreateMarker,
}: ChannelRailProps) {
  const [showAddForm, setShowAddForm] = useState(false)
  const [loginInput, setLoginInput] = useState('')
  const [thresholdInput, setThresholdInput] = useState('68')
  const [startImmediately, setStartImmediately] = useState(true)
  const [formTouched, setFormTouched] = useState(false)
  const [addBusy, setAddBusy] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [addSuccess, setAddSuccess] = useState<string | null>(null)

  const [expandedTuningId, setExpandedTuningId] = useState<number | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null)
  const [busyActions, setBusyActions] = useState<Record<string, boolean>>({})
  const [actionErrors, setActionErrors] = useState<Record<number, string>>({})
  const [tuningDrafts, setTuningDrafts] = useState<
    Record<
      number,
      {
        hype_threshold: string
        pre_roll_sec: string
        post_roll_sec: string
        auto_monitor_online: boolean
      }
    >
  >({})
  const [tuningSavedId, setTuningSavedId] = useState<number | null>(null)
  const [twitchFeedback, setTwitchFeedback] = useState<
    Record<number, { text: string; editUrl?: string }>
  >({})
  const [clipDurationByChannel, setClipDurationByChannel] = useState<Record<number, number>>({})

  const trimmedLogin = loginInput.trim()
  const extractedLogin = extractTwitchLogin(trimmedLogin)
  const isUrlOrHandle =
    trimmedLogin.includes('://') || trimmedLogin.includes('/') || trimmedLogin.startsWith('@')
  const loginInvalid = formTouched && trimmedLogin.length > 0 && extractedLogin === null
  const visibleFormError =
    addError ??
    (formTouched && trimmedLogin.length === 0
      ? 'Enter a Twitch channel name or live stream URL.'
      : loginInvalid
        ? 'Enter a valid Twitch channel (e.g. caseoh_ or https://www.twitch.tv/caseoh_).'
        : null)

  const setBusy = (key: string, flag: boolean) => {
    setBusyActions((prev) => ({ ...prev, [key]: flag }))
  }

  const handleCreateSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setFormTouched(true)
    setAddError(null)
    setAddSuccess(null)

    if (!extractedLogin) {
      setAddError('Enter a valid Twitch channel name or https://www.twitch.tv/<channel> URL.')
      return
    }
    const parsedThreshold = Number(thresholdInput)
    if (!Number.isFinite(parsedThreshold) || parsedThreshold < 10 || parsedThreshold > 100) {
      setAddError('Hype threshold must be between 10 and 100.')
      return
    }

    setAddBusy(true)
    try {
      await onCreateChannel(
        {
          channel_login: extractedLogin,
          display_name: extractedLogin,
          hype_threshold: parsedThreshold,
        },
        startImmediately,
      )
      setLoginInput('')
      setFormTouched(false)
      setAddSuccess(`Added ${extractedLogin}.`)
      setShowAddForm(false)
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Unable to add channel.')
    } finally {
      setAddBusy(false)
    }
  }

  const openTuning = (ch: Channel) => {
    if (expandedTuningId === ch.id) {
      setExpandedTuningId(null)
      return
    }
    setTuningDrafts((prev) => ({
      ...prev,
      [ch.id]: {
        hype_threshold: String(ch.hype_threshold),
        pre_roll_sec: String(ch.pre_roll_sec),
        post_roll_sec: String(ch.post_roll_sec),
        auto_monitor_online: Boolean(ch.auto_monitor_online),
      },
    }))
    setTuningSavedId(null)
    setExpandedTuningId(ch.id)
  }

  const handleSaveTuning = async (ch: Channel) => {
    const draft = tuningDrafts[ch.id]
    if (!draft) return
    const hypeThreshold = Number(draft.hype_threshold)
    const preRoll = Math.round(Number(draft.pre_roll_sec))
    const postRoll = Math.round(Number(draft.post_roll_sec))
    if (
      !Number.isFinite(hypeThreshold) ||
      hypeThreshold < 10 ||
      hypeThreshold > 100 ||
      !Number.isFinite(preRoll) ||
      preRoll < 5 ||
      preRoll > 180 ||
      !Number.isFinite(postRoll) ||
      postRoll < 5 ||
      postRoll > 120
    ) {
      setActionErrors((prev) => ({
        ...prev,
        [ch.id]: 'Threshold 10–100, pre-roll 5–180s, post-roll 5–120s.',
      }))
      return
    }

    const key = `tune-${ch.id}`
    setBusy(key, true)
    setActionErrors((prev) => ({ ...prev, [ch.id]: '' }))
    try {
      await onUpdateChannel(ch.id, {
        hype_threshold: hypeThreshold,
        pre_roll_sec: preRoll,
        post_roll_sec: postRoll,
        auto_monitor_online: draft.auto_monitor_online,
      })
      setTuningSavedId(ch.id)
    } catch (err) {
      setActionErrors((prev) => ({
        ...prev,
        [ch.id]: err instanceof Error ? err.message : 'Failed to save channel settings.',
      }))
    } finally {
      setBusy(key, false)
    }
  }

  const runChannelAction = async (chId: number, actionKey: string, fn: () => Promise<void>) => {
    setBusy(actionKey, true)
    setActionErrors((prev) => ({ ...prev, [chId]: '' }))
    try {
      await fn()
    } catch (err) {
      setActionErrors((prev) => ({
        ...prev,
        [chId]: err instanceof Error ? err.message : 'Channel action failed.',
      }))
    } finally {
      setBusy(actionKey, false)
    }
  }

  return (
    <aside className="desk-rail" aria-label="Video folders and live channels">
      {/* Video Folders Bin (Primary Quick-Switcher) */}
      <div className="border-b border-rule p-3.5 flex flex-col gap-2.5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Folder size={15} className="text-accent" aria-hidden="true" />
            <h2 className="eyebrow-label !text-ink">Video Projects</h2>
          </div>
          <span className="studio-pill num">{clipFolders.length}</span>
        </div>

        {clipFolders.length === 0 ? (
          <p className="text-xs text-muted py-1">
            Analyzed URLs, uploads, and live captures are organized into project folders here.
          </p>
        ) : (
          <ul className="list-none m-0 p-0 flex flex-col gap-1.5 max-h-56 overflow-y-auto pr-0.5">
            {clipFolders.map((folder) => {
              const isActive = activeFolderKey === folder.key
              return (
                <li key={folder.key}>
                  <button
                    type="button"
                    data-selected={isActive ? 'true' : 'false'}
                    onClick={() => onSelectFolder(folder.key)}
                    className="interactive-row p-2.5 flex flex-col gap-1"
                  >
                    <div className="flex items-center justify-between gap-2 w-full">
                      <span className="flex items-center gap-2 min-w-0">
                        {isActive ? (
                          <FolderOpen size={14} className="text-accent shrink-0" aria-hidden="true" />
                        ) : (
                          <Folder size={14} className="text-muted shrink-0" aria-hidden="true" />
                        )}
                        <span className="text-xs font-semibold text-ink truncate">
                          {folder.label}
                        </span>
                      </span>
                      <span className="studio-pill studio-pill--accent !py-0 !px-2 num">
                        {folder.clipCount}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-[11px] text-muted w-full pl-5">
                      <span className="truncate">{folder.sublabel}</span>
                      <span className="num shrink-0">
                        {folder.exportedCount > 0
                          ? `${folder.exportedCount}/${folder.clipCount} done`
                          : `${folder.pendingCount} ready`}
                      </span>
                    </div>
                  </button>
                </li>
              )
            })}
            {clipFolders.length > 1 && (
              <li>
                <button
                  type="button"
                  aria-pressed={activeFolderKey === '__all__'}
                  onClick={() => onSelectFolder('__all__')}
                  className={`btn btn--compact w-full ${
                    activeFolderKey === '__all__' ? 'btn--accent-outline' : 'btn--ghost'
                  }`}
                >
                  View All Folders Combined
                </button>
              </li>
            )}
          </ul>
        )}
      </div>

      {/* Live Stream Channels Section */}
      <div className="flex items-center justify-between px-3.5 py-3 border-b border-rule">
        <span className="eyebrow-label !text-ink">Live Streamers</span>
        <button
          type="button"
          onClick={() => {
            setShowAddForm((v) => !v)
            setAddError(null)
            setAddSuccess(null)
          }}
          aria-expanded={showAddForm}
          className="btn btn--quiet btn--compact"
        >
          <Plus size={13} aria-hidden="true" />
          <span>{showAddForm ? 'Close' : 'Add Streamer'}</span>
        </button>
      </div>

      {showAddForm && (
        <form onSubmit={handleCreateSubmit} noValidate className="p-3.5 border-b border-rule bg-paper-2/60">
          <div className="flex flex-col gap-2.5">
            <div>
              <label htmlFor="channel-login-input" className="block text-xs font-medium text-ink-2 mb-1">
                Twitch handle or URL
              </label>
              <input
                id="channel-login-input"
                name="channel_login"
                type="text"
                autoComplete="off"
                placeholder="xqc or https://twitch.tv/xqc"
                value={loginInput}
                onChange={(e) => {
                  setLoginInput(e.target.value)
                  setAddError(null)
                  setAddSuccess(null)
                }}
                onBlur={() => setFormTouched(true)}
                aria-required="true"
                aria-invalid={Boolean(visibleFormError)}
                aria-describedby="channel-login-help"
                data-state={addSuccess ? 'success' : undefined}
                disabled={addBusy}
                className="field-input"
              />
            </div>

            <div>
              <label htmlFor="channel-threshold-input" className="block text-xs font-medium text-ink-2 mb-1">
                Auto-clip hype trigger (10–100)
              </label>
              <input
                id="channel-threshold-input"
                name="hype_threshold"
                type="number"
                min={10}
                max={100}
                step={1}
                value={thresholdInput}
                onChange={(e) => setThresholdInput(e.target.value)}
                disabled={addBusy}
                className="field-input num"
              />
            </div>

            <label className="flex items-center gap-2 py-0.5 text-xs text-ink-2 cursor-pointer">
              <input
                type="checkbox"
                checked={startImmediately}
                onChange={(e) => setStartImmediately(e.target.checked)}
                disabled={addBusy}
                className="w-4 h-4 accent-accent rounded"
              />
              <span>Start live monitoring right away</span>
            </label>

            <p
              id="channel-login-help"
              aria-live="polite"
              className={`field-help ${
                visibleFormError
                  ? 'field-help--error'
                  : addSuccess
                    ? 'field-help--success'
                    : ''
              }`}
            >
              {visibleFormError ??
                addSuccess ??
                (extractedLogin && isUrlOrHandle
                  ? `Will add @${extractedLogin}`
                  : 'Monitors chat, audio, and motion in real time.')}
            </p>

            <button
              type="submit"
              disabled={addBusy}
              aria-busy={addBusy}
              className="btn btn--primary btn--compact !min-h-[36px] w-full"
            >
              {addBusy ? <span className="desk-spinner" aria-hidden="true" /> : <Plus size={14} aria-hidden="true" />}
              <span>{addBusy ? 'Adding...' : 'Save Streamer'}</span>
            </button>
          </div>
        </form>
      )}

      <div className="flex-1 overflow-y-auto p-3">
        {loading && channels.length === 0 ? (
          <div className="flex flex-col gap-2.5" aria-busy="true" aria-label="Loading channels">
            {[1, 2].map((slot) => (
              <div key={slot} className="studio-card p-3">
                <div className="h-3.5 rounded bg-paper-3 w-2/3 mb-2" />
                <div className="h-2 rounded bg-paper-3 w-full mb-3" />
                <div className="h-7 rounded bg-paper-3 w-full" />
              </div>
            ))}
          </div>
        ) : error && channels.length === 0 ? (
          <div className="studio-card p-3 flex flex-col gap-2.5">
            <p className="text-xs text-error">{error}</p>
            <button type="button" onClick={onRetry} className="btn btn--quiet btn--compact">
              Retry loading channels
            </button>
          </div>
        ) : channels.length === 0 ? (
          <div className="studio-card p-3.5 flex flex-col gap-2.5">
            <p className="text-xs font-semibold text-ink">No live channels added yet</p>
            <p className="text-xs text-muted">
              Add a Twitch streamer to auto-capture hype clips while they stream live.
            </p>
            <button
              type="button"
              onClick={() => setShowAddForm(true)}
              className="btn btn--accent-outline btn--compact w-full"
            >
              <Plus size={14} aria-hidden="true" />
              <span>Add Streamer</span>
            </button>
          </div>
        ) : (
          <ul className="list-none m-0 p-0 flex flex-col gap-2.5">
            {channels.map((ch) => {
              const tick = latestTicks[ch.id]
              const meta = metadataByChannel[ch.id]
              const currentHype = tick ? tick.fused_score : 0
              const isSelected = selectedChannelId === ch.id
              const toggleBusy = Boolean(busyActions[`toggle-${ch.id}`])
              const clipBusy = Boolean(busyActions[`clip-${ch.id}`])
              const tuneBusy = Boolean(busyActions[`tune-${ch.id}`])
              const deleteBusy = Boolean(busyActions[`del-${ch.id}`])
              const publishBusy = Boolean(busyActions[`pub-${ch.id}`])
              const markerBusy = Boolean(busyActions[`mark-${ch.id}`])
              const chErr = actionErrors[ch.id]
              const draft = tuningDrafts[ch.id]
              const isTuning = expandedTuningId === ch.id
              const chTwitchFb = twitchFeedback[ch.id]

              return (
                <li
                  key={ch.id}
                  className={`studio-card p-3 flex flex-col gap-2.5 transition-colors ${
                    isSelected ? '!border-accent/60 bg-paper-3/60' : ''
                  }`}
                >
                  <div>
                    <button
                      type="button"
                      aria-pressed={isSelected}
                      onClick={() => onSelectChannel(ch.id)}
                      className="w-full text-left flex items-center justify-between gap-2 bg-transparent border-0 p-0 cursor-pointer"
                    >
                      <span className="font-semibold text-sm text-ink truncate">
                        @{ch.display_name || ch.channel_login}
                      </span>
                      <span
                        className={`studio-pill !py-0.5 !px-2 ${
                          ch.is_monitoring ? 'studio-pill--success' : ''
                        }`}
                      >
                        {ch.is_monitoring ? 'Live' : 'Idle'}
                      </span>
                    </button>

                    <div className="flex items-center justify-between text-xs text-muted mt-1.5 mb-1">
                      <span>Hype meter</span>
                      <span className="num text-ink-2">
                        {currentHype.toFixed(1)} / {ch.hype_threshold.toFixed(0)}
                      </span>
                    </div>

                    {meta?.is_live && (
                      <div className="flex items-center justify-between gap-2 text-xs mb-1.5">
                        <span className="text-ink-2 truncate">
                          {meta.game_name || 'Streaming'}
                        </span>
                        <span className="num text-accent shrink-0">
                          {meta.viewer_count != null
                            ? `${meta.viewer_count.toLocaleString()} viewers`
                            : 'Online'}
                        </span>
                      </div>
                    )}

                    <div
                      role="meter"
                      aria-label={`${ch.channel_login} live hype level`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Math.round(currentHype)}
                      className="relative h-1.5 w-full rounded-full bg-inset border border-rule overflow-hidden"
                    >
                      <div
                        className={
                          currentHype >= ch.hype_threshold
                            ? 'h-full rounded-full bg-accent'
                            : 'h-full rounded-full bg-emerald-400/80'
                        }
                        style={{
                          width: `${Math.max(0, Math.min(100, currentHype))}%`,
                        }}
                      />
                    </div>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        disabled={toggleBusy}
                        aria-busy={toggleBusy}
                        onClick={() =>
                          runChannelAction(ch.id, `toggle-${ch.id}`, () =>
                            ch.is_monitoring ? onStopMonitor(ch.id) : onStartMonitor(ch.id),
                          )
                        }
                        className={`btn btn--compact flex-1 ${
                          ch.is_monitoring ? 'btn--danger' : 'btn--accent-outline'
                        }`}
                      >
                        {toggleBusy ? (
                          <span className="desk-spinner" aria-hidden="true" />
                        ) : ch.is_monitoring ? (
                          <Square size={12} aria-hidden="true" />
                        ) : (
                          <Play size={12} aria-hidden="true" />
                        )}
                        <span>{ch.is_monitoring ? 'Stop' : 'Monitor'}</span>
                      </button>

                      <button
                        type="button"
                        aria-label={`Configure ${ch.channel_login}`}
                        aria-expanded={isTuning}
                        onClick={() => openTuning(ch)}
                        className="btn btn--compact btn--quiet btn--icon"
                      >
                        <SlidersHorizontal size={13} aria-hidden="true" />
                      </button>
                    </div>

                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        disabled={!ch.is_monitoring || clipBusy}
                        aria-busy={clipBusy}
                        title={
                          ch.is_monitoring
                            ? 'Save rolling buffer clip right now'
                            : 'Start monitoring to buffer video before clipping'
                        }
                        onClick={() => {
                          const sec = clipDurationByChannel[ch.id] ?? defaultClipNowSec(ch)
                          void runChannelAction(ch.id, `clip-${ch.id}`, () => onClipNow(ch.id, sec))
                        }}
                        className="btn btn--compact btn--quiet flex-1"
                      >
                        {clipBusy ? (
                          <span className="desk-spinner" aria-hidden="true" />
                        ) : (
                          <Scissors size={12} aria-hidden="true" />
                        )}
                        <span>Clip Now</span>
                      </button>

                      <select
                        aria-label={`Clip duration for ${ch.channel_login}`}
                        disabled={clipBusy}
                        value={String(clipDurationByChannel[ch.id] ?? defaultClipNowSec(ch))}
                        onChange={(e) => {
                          const val = Number(e.target.value)
                          setClipDurationByChannel((prev) => ({ ...prev, [ch.id]: val }))
                        }}
                        className="field-select num !min-h-[32px] !px-2 !text-xs w-20 shrink-0"
                      >
                        <option value="60">60s</option>
                        <option value="90">90s</option>
                        <option value="120">120s</option>
                        <option value="180">180s</option>
                      </select>
                    </div>
                  </div>

                  {chErr && (
                    <p className="text-xs text-error" role="alert">
                      {chErr}
                    </p>
                  )}

                  {isTuning && draft && (
                    <div className="pt-2.5 border-t border-rule flex flex-col gap-2">
                      <div className="grid grid-cols-3 gap-1.5">
                        <div>
                          <label htmlFor={`th-${ch.id}`} className="block text-[11px] text-muted mb-1">
                            Trigger
                          </label>
                          <input
                            id={`th-${ch.id}`}
                            type="number"
                            min={10}
                            max={100}
                            value={draft.hype_threshold}
                            onChange={(e) =>
                              setTuningDrafts((prev) => ({
                                ...prev,
                                [ch.id]: { ...draft, hype_threshold: e.target.value },
                              }))
                            }
                            className="field-input num !min-h-[32px] !px-2 !text-xs"
                          />
                        </div>
                        <div>
                          <label htmlFor={`pre-${ch.id}`} className="block text-[11px] text-muted mb-1">
                            Pre (s)
                          </label>
                          <input
                            id={`pre-${ch.id}`}
                            type="number"
                            min={5}
                            max={180}
                            value={draft.pre_roll_sec}
                            onChange={(e) =>
                              setTuningDrafts((prev) => ({
                                ...prev,
                                [ch.id]: { ...draft, pre_roll_sec: e.target.value },
                              }))
                            }
                            className="field-input num !min-h-[32px] !px-2 !text-xs"
                          />
                        </div>
                        <div>
                          <label htmlFor={`post-${ch.id}`} className="block text-[11px] text-muted mb-1">
                            Post (s)
                          </label>
                          <input
                            id={`post-${ch.id}`}
                            type="number"
                            min={5}
                            max={120}
                            value={draft.post_roll_sec}
                            onChange={(e) =>
                              setTuningDrafts((prev) => ({
                                ...prev,
                                [ch.id]: { ...draft, post_roll_sec: e.target.value },
                              }))
                            }
                            className="field-input num !min-h-[32px] !px-2 !text-xs"
                          />
                        </div>
                      </div>

                      <label className="flex items-center gap-2 py-0.5 text-xs text-ink-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={draft.auto_monitor_online}
                          onChange={(e) =>
                            setTuningDrafts((prev) => ({
                              ...prev,
                              [ch.id]: { ...draft, auto_monitor_online: e.target.checked },
                            }))
                          }
                          className="w-4 h-4 accent-accent rounded"
                        />
                        <span>Auto-start when live</span>
                      </label>

                      <div className="flex items-center justify-between gap-1.5 pt-1">
                        <button
                          type="button"
                          disabled={tuneBusy}
                          aria-busy={tuneBusy}
                          onClick={() => handleSaveTuning(ch)}
                          className="btn btn--compact btn--accent-outline flex-1"
                        >
                          {tuneBusy ? 'Saving...' : 'Save'}
                        </button>

                        {confirmDeleteId === ch.id ? (
                          <div className="flex items-center gap-1">
                            <button
                              type="button"
                              disabled={deleteBusy}
                              aria-busy={deleteBusy}
                              onClick={() =>
                                runChannelAction(ch.id, `del-${ch.id}`, () => onDeleteChannel(ch.id))
                              }
                              className="btn btn--compact btn--danger"
                            >
                              Delete
                            </button>
                            <button
                              type="button"
                              onClick={() => setConfirmDeleteId(null)}
                              className="btn btn--compact btn--quiet"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            type="button"
                            aria-label={`Remove ${ch.channel_login}`}
                            onClick={() => setConfirmDeleteId(ch.id)}
                            className="btn btn--compact btn--quiet btn--icon"
                          >
                            <Trash2 size={13} aria-hidden="true" />
                          </button>
                        )}
                      </div>

                      {twitchStatus?.user_connected && (
                        <div className="pt-2 border-t border-rule flex flex-col gap-1.5">
                          <div className="flex items-center gap-1.5">
                            <button
                              type="button"
                              disabled={publishBusy}
                              aria-busy={publishBusy}
                              onClick={() =>
                                runChannelAction(ch.id, `pub-${ch.id}`, async () => {
                                  const res = await onPublishClip(ch.id)
                                  setTwitchFeedback((prev) => ({
                                    ...prev,
                                    [ch.id]: {
                                      text: 'Twitch clip published.',
                                      editUrl: res.edit_url,
                                    },
                                  }))
                                })
                              }
                              className="btn btn--compact btn--accent-outline whitespace-nowrap flex-1"
                            >
                              <Clapperboard size={12} aria-hidden="true" />
                              <span>{publishBusy ? 'Clipping' : 'Twitch Clip'}</span>
                            </button>

                            {twitchStatus.has_broadcaster_scopes && (
                              <button
                                type="button"
                                disabled={markerBusy}
                                aria-busy={markerBusy}
                                onClick={() =>
                                  runChannelAction(ch.id, `mark-${ch.id}`, async () => {
                                    await onCreateMarker(ch.id)
                                    setTwitchFeedback((prev) => ({
                                      ...prev,
                                      [ch.id]: { text: 'Stream marker added.' },
                                    }))
                                  })
                                }
                                className="btn btn--compact btn--quiet whitespace-nowrap flex-1"
                              >
                                <Bookmark size={12} aria-hidden="true" />
                                <span>{markerBusy ? 'Marking' : 'Marker'}</span>
                              </button>
                            )}
                          </div>

                          {chTwitchFb && (
                            <p className="text-xs text-success">
                              <span>{chTwitchFb.text}</span>
                              {chTwitchFb.editUrl && (
                                <a
                                  href={chTwitchFb.editUrl}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="underline text-accent ml-2"
                                >
                                  Edit on Twitch
                                </a>
                              )}
                            </p>
                          )}
                        </div>
                      )}

                      {tuningSavedId === ch.id && (
                        <p className="field-help field-help--success">Channel settings saved.</p>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </aside>
  )
}
