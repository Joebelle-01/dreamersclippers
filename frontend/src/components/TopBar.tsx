import { Activity, Clapperboard, Cpu, KeyRound, PlusCircle, Radio, RefreshCw, Sparkles, Wifi } from 'lucide-react'
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
      {/* Brand + Live Status + Workspace Mode Switcher */}
      <div className="flex items-center gap-3.5 flex-wrap min-w-0">
        <div className="flex items-center gap-2.5">
          <div
            aria-hidden="true"
            className="w-8 h-8 rounded-lg flex items-center justify-center bg-accent text-white shadow-sm"
          >
            <Sparkles size={16} />
          </div>
          <div className="flex items-center gap-2">
            <h1 className="desk-wordmark">StreamClippers</h1>
            <span className="studio-pill studio-pill--accent">Studio</span>
          </div>
        </div>

        <div
          role="tablist"
          aria-label="Studio workspace view"
          className="seg-group grid-cols-2 ml-1"
        >
          <button
            type="button"
            role="tab"
            aria-selected={activeWorkspace === 'studio'}
            onClick={() => onSelectWorkspace('studio')}
            className="seg-cell flex items-center gap-1.5"
          >
            <Clapperboard size={13} aria-hidden="true" />
            <span>Clip Editor</span>
            <span className="num opacity-80">({pendingCount + exportedCount})</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeWorkspace === 'radar'}
            onClick={() => onSelectWorkspace('radar')}
            className="seg-cell flex items-center gap-1.5"
          >
            <Activity size={13} aria-hidden="true" />
            <span>Live Radar</span>
            {activeMonitors > 0 && (
              <span className="w-2 h-2 rounded-full bg-emerald-400 inline-block" aria-hidden="true" />
            )}
          </button>
        </div>

        <div
          role="status"
          aria-live="polite"
          className={`studio-pill ${
            activeMonitors > 0 ? 'studio-pill--success' : ''
          }`}
        >
          <Radio size={13} aria-hidden="true" />
          <span className="num">
            {activeMonitors > 0 ? `${activeMonitors} Monitoring` : 'Standby'}
          </span>
        </div>
      </div>

      {/* System Telemetry + Primary Actions */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="hidden xl:flex items-center gap-2.5 text-xs">
          {statusLoading && !systemStatus ? (
            <span className="studio-pill">Checking engine...</span>
          ) : statusError ? (
            <div className="flex items-center gap-2">
              <span className="studio-pill studio-pill--danger">Backend offline</span>
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
                className={`studio-pill ${
                  !ffmpegReady ? 'studio-pill--danger' : nvencReady ? 'studio-pill--cyan' : ''
                }`}
                title={systemStatus?.gpu_name ?? 'Video Encoder'}
              >
                <Cpu size={13} aria-hidden="true" />
                <span>
                  {!ffmpegReady
                    ? 'FFmpeg Missing'
                    : nvencReady
                      ? systemStatus?.gpu_name
                        ? `NVENC · ${systemStatus.gpu_name.replace('NVIDIA GeForce ', '')}`
                        : 'RTX NVENC'
                      : 'x264 CPU'}
                </span>
              </span>

              <span
                className={`studio-pill ${
                  wsState === 'open'
                    ? 'studio-pill--success'
                    : wsState === 'connecting'
                      ? 'studio-pill--accent'
                      : 'studio-pill--danger'
                }`}
              >
                <Wifi size={13} aria-hidden="true" />
                <span>
                  {wsState === 'open' ? 'Live Sync' : wsState === 'connecting' ? 'Syncing' : 'Offline'}
                </span>
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onOpenTwitchModal}
            className={`btn btn--compact ${
              twitchStatus?.user_connected ? 'btn--accent-outline' : 'btn--quiet'
            }`}
          >
            <KeyRound size={14} aria-hidden="true" />
            <span>{twitchStatus?.user_connected ? 'Twitch Linked' : 'Twitch API'}</span>
          </button>

          <button
            type="button"
            onClick={onOpenVodModal}
            className="btn btn--primary btn--compact !min-h-[36px] !px-3.5"
          >
            <PlusCircle size={15} aria-hidden="true" />
            <span>New Video / VOD</span>
          </button>
        </div>
      </div>
    </header>
  )
}
