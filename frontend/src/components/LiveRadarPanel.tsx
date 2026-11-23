import { useMemo, useState } from 'react'
import { Activity, MessageSquare, Tv } from 'lucide-react'
import type { Channel, ChatBurstEvent, HypeTickEvent } from '../types'

interface LiveRadarPanelProps {
  channel: Channel | null
  ticks: HypeTickEvent[]
  chatMessages: ChatBurstEvent[]
}

const CHART_W = 640
const CHART_H = 148
const PAD_X = 8
const PAD_Y = 10

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

export function LiveRadarPanel({ channel, ticks, chatMessages }: LiveRadarPanelProps) {
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

  return (
    <section
      aria-labelledby="live-radar-heading"
      className="border-b border-rule bg-paper"
    >
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="desk-well grained p-4 border-b lg:border-b-0 lg:border-r border-rule min-w-0">
          <div className="flex items-center justify-between gap-4 flex-wrap mb-3">
            <div className="flex items-center gap-3 min-w-0">
              <Activity size={17} className="text-accent shrink-0" aria-hidden="true" />
              <h2 id="live-radar-heading" className="section-title text-ink">
                {channel ? `Live Radar: ${channel.display_name}` : 'Live Radar'}
              </h2>
            </div>

            <dl className="flex items-center gap-4 m-0 text-sm flex-wrap">
              <div className="flex items-center gap-2">
                <dt className="text-accent">Fused</dt>
                <dd className="m-0 num font-semibold text-ink">
                  {latest ? latest.fused_score.toFixed(1) : '0.0'}
                </dd>
              </div>
              <div className="flex items-center gap-2 pl-3 border-l border-rule">
                <dt className="text-signal-chat">Chat</dt>
                <dd className="m-0 num text-ink-2">
                  {latest ? latest.chat_score.toFixed(1) : '0.0'}
                </dd>
              </div>
              <div className="flex items-center gap-2 pl-3 border-l border-rule">
                <dt className="text-signal-game">Audio</dt>
                <dd className="m-0 num text-ink-2">
                  {latest ? latest.audio_score.toFixed(1) : '0.0'}
                </dd>
              </div>
              <div className="flex items-center gap-2 pl-3 border-l border-rule">
                <dt className="text-success">Motion</dt>
                <dd className="m-0 num text-ink-2">
                  {latest ? latest.motion_score.toFixed(1) : '0.0'}
                </dd>
              </div>
              <div className="flex items-center gap-2 pl-3 border-l border-rule">
                <dt className="text-muted">Trigger</dt>
                <dd className="m-0 num text-muted">{threshold.toFixed(0)}</dd>
              </div>
              {channel && (
                <div className="pl-3 border-l border-rule">
                  <button
                    type="button"
                    aria-pressed={showLivePreview}
                    onClick={handleTogglePreview}
                    className={`btn btn--compact ${
                      showLivePreview ? 'btn--accent-outline' : 'btn--quiet'
                    }`}
                  >
                    <Tv size={14} aria-hidden="true" />
                    <span>{showLivePreview ? 'Hide Preview' : 'Live Preview'}</span>
                  </button>
                </div>
              )}
            </dl>
          </div>

          <div className="relative border border-rule bg-paper">
            <svg
              viewBox={`0 0 ${CHART_W} ${CHART_H}`}
              role="img"
              aria-label="Live hype telemetry oscilloscope plotting fused, chat, audio, and motion signals"
              className="w-full h-36"
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
                strokeWidth={1}
                strokeDasharray="4 4"
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
                  strokeWidth={1.25}
                  points={motionPoints}
                />
              )}
              {audioPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-signal-game)"
                  strokeWidth={1.25}
                  points={audioPoints}
                />
              )}
              {chatPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-signal-chat)"
                  strokeWidth={1.25}
                  points={chatPoints}
                />
              )}
              {fusedPoints && (
                <polyline
                  fill="none"
                  stroke="var(--color-accent)"
                  strokeWidth={2}
                  points={fusedPoints}
                />
              )}
            </svg>

            {ticks.length === 0 && (
              <div className="px-3 py-2 border-t border-rule flex items-center justify-between text-xs text-muted">
                <span>
                  {channel
                    ? channel.is_monitoring
                      ? 'Awaiting incoming 2s telemetry ticks from stream pipeline.'
                      : `Channel @${channel.channel_login} is idle. Start monitoring to stream live ticks.`
                    : 'Select or add a Twitch channel to observe live signal traces.'}
                </span>
                {latest?.top_tokens && latest.top_tokens.length > 0 && (
                  <span className="text-ink-2">Peaks: {latest.top_tokens.slice(0, 4).join(', ')}</span>
                )}
              </div>
            )}
          </div>

          {showLivePreview && channel && (
            <div className="mt-3 twitch-embed-wrap">
              <iframe
                title={`Twitch live preview for ${channel.display_name || channel.channel_login}`}
                src={`https://player.twitch.tv/?channel=${encodeURIComponent(channel.channel_login)}&parent=${encodeURIComponent(parentHost)}&muted=true&autoplay=true`}
                allowFullScreen
                className="twitch-embed-frame"
              />
            </div>
          )}
        </div>

        <div className="p-4 bg-paper-2 flex flex-col min-w-0">
          <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
            <div className="flex items-center gap-2">
              <MessageSquare size={15} className="text-muted" aria-hidden="true" />
              <h3 className="text-sm font-medium text-ink">
                {activeChatMode === 'twitch' ? 'Twitch Chat' : 'Chat Burst Feed'}
              </h3>
            </div>
            <div className="flex items-center gap-2">
              {showLivePreview && channel ? (
                <div className="flex items-center" role="group" aria-label="Chat panel mode">
                  <button
                    type="button"
                    aria-pressed={activeChatMode === 'hype'}
                    onClick={() => setChatMode('hype')}
                    className="seg-cell"
                  >
                    Hype Bursts
                  </button>
                  <button
                    type="button"
                    aria-pressed={activeChatMode === 'twitch'}
                    onClick={() => setChatMode('twitch')}
                    className="seg-cell"
                  >
                    Twitch Chat
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
          </div>

          {activeChatMode === 'twitch' && channel ? (
            <div className="flex-1 min-h-80 border border-rule bg-paper flex flex-col">
              <iframe
                title={`Twitch chat for ${channel.display_name || channel.channel_login}`}
                src={`https://www.twitch.tv/embed/${encodeURIComponent(channel.channel_login)}/chat?parent=${encodeURIComponent(parentHost)}&darkpopout`}
                sandbox="allow-storage-access-by-user-activation allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-modals"
                className="twitch-chat-frame"
              />
            </div>
          ) : (
            <div
              className={`flex-1 overflow-hidden border border-rule bg-paper p-2 ${
                showLivePreview ? 'min-h-60 max-h-96' : 'h-36'
              }`}
            >
              {chatMessages.length === 0 ? (
                <p className="text-xs text-muted">
                  Live IRC bursts and trigger chat samples appear here while monitoring.
                </p>
              ) : (
                <ul className="list-none m-0 p-0 flex flex-col gap-1">
                  {chatMessages.slice(0, showLivePreview ? 10 : 5).map((msg, idx) => (
                    <li
                      key={`${msg.author}-${String(msg.ts)}-${idx}`}
                      className="text-xs leading-snug truncate"
                    >
                      <span className="font-semibold text-accent mr-2">{msg.author}</span>
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
