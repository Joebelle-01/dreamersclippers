import { Activity, Clapperboard, Cpu, KeyRound, Plus, Radio, RefreshCw, Terminal, Wifi } from 'lucide-react'
import type { SystemStatus, TwitchStatus } from '../types'

export type StudioWorkspaceTab = 'studio' | 'radar'

interface TopBarProps {
  activeWorkspace: StudioWorkspaceTab
  onSelectWorkspace: (tab: StudioWorkspaceTab) => void
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
  activeWorkspace,
  onSelectWorkspace,
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
      <div className="flex items-center gap-4 flex-wrap min-w-0">
        <div className="flex items-center gap-3 pr-4 border-r border-rule">
          <Terminal size={16} className="text-accent shrink-0" aria-hidden="true" />
          <h1 className="desk-wordmark">StreamClippers</h1>
        </div>

        <div
          role="tablist"
          aria-label="Studio workspace view"
          className="seg-group grid-cols-2"
        >
          <button
            type="button"
            role="tab"
            aria-selected={activeWorkspace === 'studio'}
            onClick={() => onSelectWorkspace('studio')}
            className="seg-cell flex items-center gap-2"
          >
            <Clapperboard size={13} aria-hidden="true" />
            <span>Cut console</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeWorkspace === 'radar'}
            onClick={() => onSelectWorkspace('radar')}
            className="seg-cell flex items-center gap-2"
          >
            <Activity size={13} aria-hidden="true" />
            <span>Telemetry radar</span>
          </button>
        </div>

        <div className="hidden md:flex items-center gap-4 pl-3 border-l border-rule text-xs">
          <span className="status-tag">
            Queued <strong className="num text-ink-2">{pendingCount}</strong>
          </span>
          <span className="status-tag">
            Exported <strong className="num text-ink-2">{exportedCount}</strong>
          </span>
          <span
            role="status"
            aria-live="polite"
            className={`status-tag pl-3 border-l border-rule ${
              activeMonitors > 0 ? 'status-tag--live' : ''
            }`}
          >
            <Radio size={12} aria-hidden="true" />
            <span className="num">
              {activeMonitors > 0 ? `${activeMonitors} live` : 'Standby'}
            </span>
          </span>
        </div>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <div className="hidden 2xl:flex items-center gap-3 text-xs">
          {statusLoading && !systemStatus ? (
            <span className="status-tag">Checking engine…</span>
          ) : statusError ? (
            <div className="flex items-center gap-2">
              <span className="status-tag status-tag--alert">Engine offline</span>
              <button
                type="button"
                onClick={onRetryStatus}
                className="btn btn--quiet btn--compact"
                aria-label="Retry backend status check"
              >
                <RefreshCw size={13} aria-hidden="true" />
                <span>Retry</span>
              </button>
            </div>
          ) : (
            <>
              <span
                className={`status-tag ${
                  !ffmpegReady ? 'status-tag--alert' : nvencReady ? 'status-tag--ready' : ''
                }`}
                title={systemStatus?.gpu_name ?? 'Video Encoder'}
              >
                <Cpu size={12} aria-hidden="true" />
                <span>
                  {!ffmpegReady
                    ? 'FFmpeg missing'
                    : nvencReady
                      ? systemStatus?.gpu_name
                        ? `NVENC ${systemStatus.gpu_name.replace('NVIDIA GeForce ', '')}`
                        : 'RTX NVENC'
                      : 'x264 CPU'}
                </span>
              </span>

              <span
                className={`status-tag pl-3 border-l border-rule ${
                  wsState === 'open'
                    ? 'status-tag--ready'
                    : wsState === 'connecting'
                      ? 'status-tag--live'
                      : 'status-tag--alert'
                }`}
              >
                <Wifi size={12} aria-hidden="true" />
                <span>
                  {wsState === 'open' ? 'Socket synced' : wsState === 'connecting' ? 'Syncing…' : 'Offline'}
                </span>
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onOpenTwitchModal}
            className="btn btn--quiet btn--compact"
          >
            <KeyRound size={13} aria-hidden="true" />
            <span>{twitchStatus?.user_connected ? 'Twitch linked' : 'Twitch API'}</span>
          </button>

          <button
            type="button"
            onClick={onOpenVodModal}
            className="btn btn--quiet btn--compact"
          >
            <Plus size={14} aria-hidden="true" />
            <span>New video</span>
          </button>
        </div>
      </div>
    </header>
  )
}
