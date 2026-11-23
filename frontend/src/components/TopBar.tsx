import { Film, KeyRound, Radio, RefreshCw, Video } from 'lucide-react'
import type { SystemStatus, TwitchStatus } from '../types'

interface TopBarProps {
  systemStatus: SystemStatus | null
  statusLoading: boolean
  statusError: string | null
  wsState: 'connecting' | 'open' | 'offline'
  pendingCount: number
  exportedCount: number
  twitchStatus: TwitchStatus | null
  onRetryStatus: () => void
  onOpenVodModal: () => void
  onOpenTwitchModal: () => void
}

export function TopBar({
  systemStatus,
  statusLoading,
  statusError,
  wsState,
  pendingCount,
  exportedCount,
  twitchStatus,
  onRetryStatus,
  onOpenVodModal,
  onOpenTwitchModal,
}: TopBarProps) {
  const activeMonitors = systemStatus?.active_monitors ?? 0
  const ffmpegReady = Boolean(systemStatus?.ffmpeg_path)
  const nvencReady = Boolean(systemStatus?.nvenc_available)

  return (
    <header className="desk-topbar">
      <div className="flex items-center gap-4 min-w-0">
        <h1 className="desk-wordmark">StreamClippers</h1>
        <div
          role="status"
          aria-live="polite"
          className={`flex items-center gap-2 px-3 py-1 border ${
            activeMonitors > 0
              ? 'border-accent bg-paper-2 text-accent'
              : 'border-rule bg-paper-2 text-muted'
          }`}
        >
          <Radio size={15} aria-hidden="true" />
          <span className="num text-sm font-medium">
            {activeMonitors > 0 ? `Live ${activeMonitors}` : 'Standby'}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-3 md:pr-4 md:border-r md:border-rule text-sm">
          {statusLoading && !systemStatus ? (
            <span className="text-muted">Checking encoder</span>
          ) : statusError ? (
            <div className="flex items-center gap-2 text-error">
              <span>Backend offline</span>
              <button
                type="button"
                onClick={onRetryStatus}
                className="btn btn--quiet btn--compact"
                aria-label="Retry backend status check"
              >
                <RefreshCw size={14} aria-hidden="true" />
                <span>Retry</span>
              </button>
            </div>
          ) : (
            <dl className="flex items-center gap-4 m-0 flex-wrap">
              <div className="flex items-center gap-2">
                <dt className="text-muted">FFmpeg</dt>
                <dd className={`m-0 font-medium ${ffmpegReady ? 'text-ink' : 'text-error'}`}>
                  {ffmpegReady ? 'Ready' : 'Missing'}
                </dd>
              </div>
              <div className="flex items-center gap-2 md:pl-4 md:border-l md:border-rule">
                <dt className="text-muted">Encoder</dt>
                <dd className={`m-0 font-medium ${nvencReady ? 'text-success' : 'text-ink-2'}`}>
                  {nvencReady
                    ? systemStatus?.gpu_name
                      ? `NVENC (${systemStatus.gpu_name})`
                      : 'RTX NVENC'
                    : 'x264 CPU'}
                </dd>
              </div>
              <div className="flex items-center gap-2 md:pl-4 md:border-l md:border-rule">
                <dt className="text-muted">Telemetry</dt>
                <dd
                  className={`m-0 font-medium ${
                    wsState === 'open'
                      ? 'text-success'
                      : wsState === 'connecting'
                        ? 'text-accent'
                        : 'text-error'
                  }`}
                >
                  {wsState === 'open' ? 'Connected' : wsState === 'connecting' ? 'Linking' : 'Disconnected'}
                </dd>
              </div>
            </dl>
          )}
        </div>

        <div className="flex items-center gap-3 md:pr-4 md:border-r md:border-rule text-sm">
          <div className="flex items-center gap-2">
            <Film size={15} className="text-accent" aria-hidden="true" />
            <span className="text-muted">Review queue</span>
            <span className="num font-semibold text-ink">{pendingCount}</span>
          </div>
          <div className="flex items-center gap-2 pl-3 border-l border-rule">
            <span className="text-muted">Exported</span>
            <span className="num font-medium text-ink-2">{exportedCount}</span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onOpenTwitchModal}
            className={`btn whitespace-nowrap ${
              twitchStatus?.user_connected ? 'btn--accent-outline' : 'btn--quiet'
            }`}
          >
            <KeyRound size={15} aria-hidden="true" />
            <span>{twitchStatus?.user_connected ? 'Twitch Linked' : 'Twitch API'}</span>
          </button>

          <button
            type="button"
            onClick={onOpenVodModal}
            className="btn btn--accent-outline whitespace-nowrap"
          >
            <Video size={16} aria-hidden="true" />
            <span>Analyze Video</span>
          </button>
        </div>
      </div>
    </header>
  )
}
