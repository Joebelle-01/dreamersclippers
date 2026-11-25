import { useMemo, useState } from 'react'
import { Activity, Clapperboard, MessageSquare, Tv } from 'lucide-react'
import type { Channel, ChatBurstEvent, HypeTickEvent } from '../types'

interface LiveRadarPanelProps {
  channel: Channel | null
  ticks: HypeTickEvent[]
  chatMessages: ChatBurstEvent[]
  compact?: boolean
  onExpandRadar?: () => void
  onSwitchToEditor?: () => void
}

const CHART_W = 640
const CHART_H = 168
const PAD_X = 10
const PAD_Y = 12

function buildPolyline(
  ticks: HypeTickEvent[],
  pick: (t: HypeTickEvent) => number,
): string {
  if (ticks.length === 0) return ''
  const plotW = CHART_W - PAD_X * 2
  const plotH = CHART_H - PAD_Y * 2
  if (ticks.length === 1) {
    const y = PAD_Y + plotH * (1 - Math.max(0, Math.min(100, pick(ticks[0]))) / 100)
    return `${PAD_X},${y.toFixed(1)} ${(PAD_X + plotW).toFixed(1)},${y.toFixed(1)}`
  }
  return ticks
    .map((pt, idx) => {
      const x = PAD_X + (idx / (ticks.length - 1)) * plotW
      const val = Math.max(0, Math.min(100, pick(pt)))
      const y = PAD_Y + plotH * (1 - val / 100)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

export function LiveRadarPanel({
  channel,
  ticks,
  chatMessages,
  compact = false,
  onExpandRadar,
  onSwitchToEditor,
}: LiveRadarPanelProps) {
  const [showLivePreview, setShowLivePreview] = useState(false)
  const [chatMode, setChatMode] = useState<'hype' | 'twitch'>('hype')
  const latest = ticks.length > 0 ? ticks[ticks.length - 1] : null
  const threshold = latest?.threshold ?? channel?.hype_threshold ?? 68
  const activeChatMode = showLivePreview && channel ? chatMode : 'hype'
  const parentHost =
    typeof window !== 'undefined' && window.location.hostname
      ? window.location.hostname
      : '127.0.0.1'

  const handleTogglePreview = () => {
    setShowLivePreview((prev) => {
      const next = !prev
      if (!next) {
        setChatMode('hype')
      }
      return next
    })
  }

  const { fusedPoints, chatPoints, audioPoints, motionPoints, thresholdY, triggers } = useMemo(() => {
    const plotW = CHART_W - PAD_X * 2
    const plotH = CHART_H - PAD_Y * 2
    const clampedTh = Math.max(0, Math.min(100, threshold))
    const thY = PAD_Y + plotH * (1 - clampedTh / 100)
    const trigXs = ticks
      .map((pt, idx) =>
        pt.should_trigger
          ? PAD_X + (ticks.length > 1 ? (idx / (ticks.length - 1)) * plotW : plotW)
          : null,
      )
      .filter((x): x is number => x !== null)

    return {
      fusedPoints: buildPolyline(ticks, (t) => t.fused_score),
      chatPoints: buildPolyline(ticks, (t) => t.chat_score),
      audioPoints: buildPolyline(ticks, (t) => t.audio_score),
      motionPoints: buildPolyline(ticks, (t) => t.motion_score),
      thresholdY: thY,
      triggers: trigXs,
    }
  }, [ticks, threshold])

  if (compact) {
    if (!channel) return null
    return (
      <div className="border-b border-rule bg-paper px-4 py-1.5 flex items-center justify-between gap-4 flex-wrap text-xs">
        <div className="flex items-center gap-4 flex-wrap">
          <span className={`status-tag ${channel.is_monitoring ? 'status-tag--live' : ''}`}>
            <span>
              {channel.is_monitoring
                ? `Live radar: @${channel.channel_login}`
                : `Channel radar: @${channel.channel_login} (idle)`}
            </span>
          </span>
          <span className="text-muted pl-3 border-l border-rule">
            Fused <strong className="num text-ink">{latest ? latest.fused_score.toFixed(1) : '0.0'}</strong> /{' '}
            <span className="num">{threshold.toFixed(0)}</span>
          </span>
          <span className="hidden md:inline-flex items-center gap-3 pl-3 border-l border-rule text-muted">
            <span>
              Chat <strong className="num text-ink-2">{latest ? latest.chat_score.toFixed(1) : '0.0'}</strong>
            </span>
            <span>
              Audio <strong className="num text-ink-2">{latest ? latest.audio_score.toFixed(1) : '0.0'}</strong>
            </span>
            <span>
              Motion <strong className="num text-ink-2">{latest ? latest.motion_score.toFixed(1) : '0.0'}</strong>
            </span>
          </span>
        </div>
        {onExpandRadar && (
          <button type="button" onClick={onExpandRadar} className="btn btn--compact btn--quiet">
            <Activity size={13} aria-hidden="true" />
            <span>Open radar</span>
          </button>
        )}
      </div>
    )
  }

  return (
    <section
      aria-labelledby="live-radar-heading"
      className="flex-1 p-6 flex flex-col gap-6 bg-paper"
    >
      <div className="flex items-center justify-between gap-4 flex-wrap pb-4 border-b border-rule">
        <div className="flex items-center gap-3">
          <Activity size={16} className="text-accent shrink-0" aria-hidden="true" />
          <h2 id="live-radar-heading" className="section-title">
            {channel ? `Stream telemetry: @${channel.channel_login}` : 'Stream telemetry'}
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {channel && (
            <button
              type="button"
              aria-pressed={showLivePreview}
              onClick={handleTogglePreview}
              className="btn btn--compact btn--quiet"
            >
              <Tv size={14} aria-hidden="true" />
              <span>{showLivePreview ? 'Hide stream' : 'Show stream'}</span>
            </button>
          )}
          {onSwitchToEditor && (
            <button type="button" onClick={onSwitchToEditor} className="btn btn--compact btn--quiet">
              <Clapperboard size={14} aria-hidden="true" />
              <span>Cut console</span>
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_340px] gap-6 items-start">
        <div className="flex flex-col gap-6 min-w-0">
          <dl className="grid grid-cols-2 sm:grid-cols-5 m-0 border-t border-b border-rule">
            <div className="py-3 pr-4 border-r border-rule">
              <dt className="panel-label">Fused hype</dt>
              <dd className="m-0 mt-1 fig text-2xl text-accent">
                {latest ? latest.fused_score.toFixed(1) : '0.0'}
              </dd>
            </div>
            <div className="py-3 px-4 sm:border-r border-rule">
              <dt className="panel-label">Chat velocity</dt>
              <dd className="m-0 mt-1 fig text-2xl text-ink">
                {latest ? latest.chat_score.toFixed(1) : '0.0'}
              </dd>
            </div>
            <div className="py-3 pr-4 sm:px-4 border-t sm:border-t-0 border-r border-rule">
              <dt className="panel-label">Audio RMS</dt>
              <dd className="m-0 mt-1 fig text-2xl text-ink">
                {latest ? latest.audio_score.toFixed(1) : '0.0'}
              </dd>
            </div>
            <div className="py-3 px-4 border-t sm:border-t-0 sm:border-r border-rule">
              <dt className="panel-label">Frame motion</dt>
              <dd className="m-0 mt-1 fig text-2xl text-ink">
                {latest ? latest.motion_score.toFixed(1) : '0.0'}
              </dd>
            </div>
            <div className="py-3 sm:pl-4 border-t sm:border-t-0 border-rule col-span-2 sm:col-span-1">
              <dt className="panel-label">Trigger gate</dt>
              <dd className="m-0 mt-1 fig text-2xl text-muted">{threshold.toFixed(0)}</dd>
            </div>
          </dl>

          <div className="well p-4 flex flex-col gap-3">
            <svg
              viewBox={`0 0 ${CHART_W} ${CHART_H}`}
              role="img"
              aria-label="Live hype telemetry oscilloscope plotting fused, chat, audio, and motion signals"
              className="w-full h-48 bg-inset border border-rule"
              preserveAspectRatio="none"
            >
              {[25, 50, 75].map((pct) => {
                const y = PAD_Y + (CHART_H - PAD_Y * 2) * (1 - pct / 100)
                return (
                  <line
                    key={pct}
                    x1={PAD_X}
                    y1={y}
                    x2={CHART_W - PAD_X}
                    y2={y}
                    stroke="var(--color-rule)"
                    strokeWidth={1}
                  />
                )
              })}

              <line
                x1={PAD_X}
                y1={thresholdY}
                x2={CHART_W - PAD_X}
                y2={thresholdY}
                stroke="var(--color-accent)"
                strokeWidth={1.25}
                strokeDasharray="5 5"
              />

              {triggers.map((tx, i) => (
                <line
                  key={`${tx}-${i}`}
                  x1={tx}
                  y1={PAD_Y}
                  x2={tx}
                  y2={CHART_H - PAD_Y}
                  stroke="var(--color-error)"
                  strokeWidth={1.5}
                />
              ))}

              {motionPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-success)"
                  strokeWidth={1.35}
                  points={motionPoints}
                />
              )}
              {audioPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-signal-game)"
                  strokeWidth={1.35}
                  points={audioPoints}
                />
              )}
              {chatPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-signal-chat)"
                  strokeWidth={1.35}
                  points={chatPoints}
                />
              )}
              {fusedPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-accent)"
                  strokeWidth={2.2}
                  points={fusedPoints}
                />
              )}
            </svg>

            {ticks.length === 0 && (
              <div className="flex items-center justify-between text-xs text-muted">
                <span>
                  {channel
                    ? channel.is_monitoring
                      ? 'Waiting for incoming 1s telemetry ticks from live pipeline…'
                      : `@${channel.channel_login} is idle. Select Monitor in the channel rail to capture live bursts.`
                    : 'Select or add a Twitch channel in the left rail to observe live signal traces.'}
                </span>
                {latest?.top_tokens && latest.top_tokens.length > 0 && (
                  <span className="text-ink-2">Peaks: {latest.top_tokens.slice(0, 4).join(', ')}</span>
                )}
              </div>
            )}
          </div>

          {showLivePreview && channel && (
            <div className="twitch-embed-wrap">
              <iframe
                title={`Twitch live preview for ${channel.display_name || channel.channel_login}`}
                src={`https://player.twitch.tv/?channel=${encodeURIComponent(channel.channel_login)}&parent=${encodeURIComponent(parentHost)}&muted=true&autoplay=true`}
                allowFullScreen
                className="twitch-embed-frame"
              />
            </div>
          )}
        </div>

        <div className="well p-4 flex flex-col gap-3 min-w-0">
          <div className="flex items-center justify-between gap-2 flex-wrap pb-2 border-b border-rule">
            <div className="flex items-center gap-2">
              <MessageSquare size={14} className="text-accent" aria-hidden="true" />
              <h3 className="text-sm font-medium text-ink">
                {activeChatMode === 'twitch' ? 'Twitch chat' : 'Chat burst feed'}
              </h3>
            </div>
            {showLivePreview && channel ? (
              <div className="seg-group grid-cols-2" role="group" aria-label="Chat panel mode">
                <button
                  type="button"
                  aria-pressed={activeChatMode === 'hype'}
                  onClick={() => setChatMode('hype')}
                  className="seg-cell"
                >
                  Bursts
                </button>
                <button
                  type="button"
                  aria-pressed={activeChatMode === 'twitch'}
                  onClick={() => setChatMode('twitch')}
                  className="seg-cell"
                >
                  Full chat
                </button>
              </div>
            ) : (
              <span className="num text-xs text-muted">
                {latest?.unique_chatters != null
                  ? `${latest.unique_chatters} chatters`
                  : `${chatMessages.length} msgs`}
              </span>
            )}
          </div>

          {activeChatMode === 'twitch' && channel ? (
            <div className="flex-1 min-h-80 border border-rule bg-inset overflow-hidden flex flex-col">
              <iframe
                title={`Twitch chat for ${channel.display_name || channel.channel_login}`}
                src={`https://www.twitch.tv/embed/${encodeURIComponent(channel.channel_login)}/chat?parent=${encodeURIComponent(parentHost)}&darkpopout`}
                sandbox="allow-storage-access-by-user-activation allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-modals"
                className="twitch-chat-frame"
              />
            </div>
          ) : (
            <div className="min-h-64 max-h-96 overflow-y-auto border border-rule bg-inset p-3">
              {chatMessages.length === 0 ? (
                <p className="text-xs text-muted">
                  Live IRC chat bursts and emote spikes appear here while monitoring.
                </p>
              ) : (
                <ul className="list-none m-0 p-0 flex flex-col gap-2">
                  {chatMessages.slice(0, 18).map((msg, idx) => (
                    <li
                      key={`${msg.author}-${String(msg.ts)}-${idx}`}
                      className="text-xs leading-snug break-words"
                    >
                      <span className="font-medium text-accent mr-2">{msg.author}</span>
                      <span className="text-ink-2">{msg.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
