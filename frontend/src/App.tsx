import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  connectLiveSocket,
  createChannel,
  createChannelMarker,
  deleteChannel,
  deleteClip,
  detectClipWebcam,
  disconnectTwitchOAuth,
  fetchChannels,
  fetchClips,
  fetchSystemStatus,
  fetchTwitchStatus,
  fetchTwitchTopClips,
  generateClipCaptions,
  generateClipThumbnail,
  getClipFolderKey,
  groupClipsIntoFolders,
  importTwitchClip,
  publishChannelClip,
  renderClipExport,
  saveClipPreset,
  saveTwitchConfig,
  startChannelMonitor,
  startTwitchOAuth,
  startVideoUploadAnalysis,
  startVideoUrlAnalysis,
  stopChannelMonitor,
  triggerClipNow,
  updateChannel,
  updateClip,
} from './api'
import { ChannelRail } from './components/ChannelRail'
import { LiveRadarPanel } from './components/LiveRadarPanel'
import { ReviewWorkbench } from './components/ReviewWorkbench'
import { TopBar } from './components/TopBar'
import { TwitchSettingsModal } from './components/TwitchSettingsModal'
import { VodScanModal } from './components/VodScanModal'
import type {
  Channel,
  ChannelCreateInput,
  ChannelMetadataEvent,
  ChannelUpdateInput,
  ChatBurstEvent,
  ClipItem,
  ClipPeriod,
  ClipUpdateInput,
  HypeTickEvent,
  LiveSocketEvent,
  SystemStatus,
  ThumbnailRequest,
  TwitchStatus,
  VideoAnalyzeUploadParams,
  VideoAnalyzeUrlRequest,
  VodProgressEvent,
} from './types'

const MAX_TICKS = 60
const MAX_CHAT = 50

export function App() {
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)
  const [statusError, setStatusError] = useState<string | null>(null)

  const [channels, setChannels] = useState<Channel[]>([])
  const [channelsLoading, setChannelsLoading] = useState(true)
  const [channelsError, setChannelsError] = useState<string | null>(null)

  const [clips, setClips] = useState<ClipItem[]>([])
  const [clipsLoading, setClipsLoading] = useState(true)
  const [clipsError, setClipsError] = useState<string | null>(null)

  const [selectedChannelId, setSelectedChannelId] = useState<number | null>(null)
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null)
  const [selectedFolderKey, setSelectedFolderKey] = useState<string>('__latest__')

  const [wsState, setWsState] = useState<'connecting' | 'open' | 'offline'>('connecting')
  const [ticksByChannel, setTicksByChannel] = useState<Record<number, HypeTickEvent[]>>({})
  const [latestTicks, setLatestTicks] = useState<Record<number, HypeTickEvent>>({})
  const [chatByChannel, setChatByChannel] = useState<Record<number, ChatBurstEvent[]>>({})
  const [metadataByChannel, setMetadataByChannel] = useState<Record<number, ChannelMetadataEvent>>({})
  const [twitchStatus, setTwitchStatus] = useState<TwitchStatus | null>(null)
  const [twitchStatusLoading, setTwitchStatusLoading] = useState(false)
  const [twitchModalOpen, setTwitchModalOpen] = useState(false)
  const [activeVodJob, setActiveVodJob] = useState<VodProgressEvent | null>(null)
  const [vodModalOpen, setVodModalOpen] = useState(false)

  const loadStatus = useCallback(async () => {
    setStatusLoading(true)
    setStatusError(null)
    try {
      const data = await fetchSystemStatus()
      setSystemStatus(data)
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : 'Unable to reach StreamClippers API.')
    } finally {
      setStatusLoading(false)
    }
  }, [])

  const loadChannels = useCallback(async () => {
    setChannelsLoading(true)
    setChannelsError(null)
    try {
      const data = await fetchChannels()
      setChannels(data)
      setSelectedChannelId((prev) => {
        if (prev !== null && data.some((c) => c.id === prev)) return prev
        return data[0]?.id ?? null
      })
    } catch (err) {
      setChannelsError(err instanceof Error ? err.message : 'Unable to load channels.')
    } finally {
      setChannelsLoading(false)
    }
  }, [])

  const loadClips = useCallback(async () => {
    setClipsLoading(true)
    setClipsError(null)
    try {
      const data = await fetchClips()
      setClips(data)
      setSelectedClipId((prev) => {
        if (prev !== null && data.some((c) => c.id === prev)) return prev
        const firstPending = data.find((c) => c.status === 'pending_review')
        return firstPending?.id ?? data[0]?.id ?? null
      })
    } catch (err) {
      setClipsError(err instanceof Error ? err.message : 'Unable to load clips.')
    } finally {
      setClipsLoading(false)
    }
  }, [])

  const loadTwitchStatus = useCallback(async () => {
    setTwitchStatusLoading(true)
    try {
      const data = await fetchTwitchStatus()
      setTwitchStatus(data)
    } catch {
      // Studio remains operational when Twitch Helix is unconfigured or offline
    } finally {
      setTwitchStatusLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadStatus()
    void loadChannels()
    void loadClips()
    void loadTwitchStatus()
  }, [loadChannels, loadClips, loadStatus, loadTwitchStatus])

  const handleWsEvent = useCallback(
    (event: LiveSocketEvent) => {
      if (event.type === 'hype_tick') {
        const tick = event.payload
        setLatestTicks((prev) => ({ ...prev, [tick.channel_id]: tick }))
        setTicksByChannel((prev) => {
          const existing = prev[tick.channel_id] ?? []
          const next = [...existing, tick].slice(-MAX_TICKS)
          return { ...prev, [tick.channel_id]: next }
        })
        return
      }

      if (event.type === 'chat_burst') {
        const burst = event.payload
        setChatByChannel((prev) => {
          const existing = prev[burst.channel_id] ?? []
          const next = [burst, ...existing].slice(0, MAX_CHAT)
          return { ...prev, [burst.channel_id]: next }
        })
        return
      }

      if (event.type === 'channel_metadata') {
        const meta = event.payload
        setMetadataByChannel((prev) => ({ ...prev, [meta.channel_id]: meta }))
        if (meta.broadcaster_id) {
          setChannels((prev) =>
            prev.map((ch) =>
              ch.id === meta.channel_id && !ch.broadcaster_id
                ? { ...ch, broadcaster_id: meta.broadcaster_id }
                : ch,
            ),
          )
        }
        return
      }

      if (event.type === 'clip_created') {
        const clip = 'clip' in event.payload ? event.payload.clip : event.payload
        const folderKey = getClipFolderKey(clip)
        setClips((prev) => {
          const filtered = prev.filter((c) => c.id !== clip.id)
          return [clip, ...filtered]
        })
        setSelectedFolderKey((prevKey) => {
          if (prevKey !== folderKey) {
            setSelectedClipId(clip.id)
          }
          return folderKey
        })
        setSelectedClipId((prev) => prev ?? clip.id)
        void loadStatus()
        return
      }

      if (event.type === 'render_status') {
        const rs = event.payload
        const nextError = rs.error_message ?? rs.error
        setClips((prev) =>
          prev.map((c) =>
            c.id === rs.clip_id
              ? {
                  ...c,
                  status: rs.status,
                  export_video_path:
                    rs.export_video_path !== undefined ? rs.export_video_path : c.export_video_path,
                  export_srt_path:
                    rs.export_srt_path !== undefined ? rs.export_srt_path : c.export_srt_path,
                  export_thumb_path:
                    rs.export_thumb_path !== undefined ? rs.export_thumb_path : c.export_thumb_path,
                  encoder_used:
                    rs.encoder_used !== undefined ? rs.encoder_used : c.encoder_used,
                  error_message: nextError !== undefined ? nextError : c.error_message,
                }
              : c,
          ),
        )
        if (rs.status === 'exported' || rs.status === 'failed') {
          void loadClips()
          void loadStatus()
        }
        return
      }

      if (event.type === 'vod_progress') {
        const vp = event.payload
        setActiveVodJob(vp)
        if (vp.progress >= 1 || vp.progress >= 100 || vp.status === 'completed' || vp.stage === 'complete') {
          void loadClips()
          void loadStatus()
        }
        return
      }

      if (event.type === 'monitor_status') {
        const ms = event.payload
        if (Array.isArray(ms.active_channel_ids)) {
          const activeSet = new Set(ms.active_channel_ids)
          setChannels((prev) =>
            prev.map((ch) => ({ ...ch, is_monitoring: activeSet.has(ch.id) })),
          )
        } else if (ms.channel_id != null && typeof ms.is_monitoring === 'boolean') {
          setChannels((prev) =>
            prev.map((ch) =>
              ch.id === ms.channel_id ? { ...ch, is_monitoring: Boolean(ms.is_monitoring) } : ch,
            ),
          )
        }
        void loadStatus()
      }
    },
    [loadClips, loadStatus],
  )

  useEffect(() => {
    const disconnect = connectLiveSocket(handleWsEvent, setWsState)
    return disconnect
  }, [handleWsEvent])

  const selectedChannel = useMemo(
    () => channels.find((c) => c.id === selectedChannelId) ?? channels[0] ?? null,
    [channels, selectedChannelId],
  )

  const handleCreateChannel = async (
    payload: ChannelCreateInput,
    startImmediately = false,
  ) => {
    const created = await createChannel(payload)
    setChannels((prev) => [created, ...prev.filter((c) => c.id !== created.id)])
    setSelectedChannelId(created.id)
    if (startImmediately) {
      const started = await startChannelMonitor(created.id)
      setChannels((prev) => prev.map((c) => (c.id === started.id ? started : c)))
    }
    void loadStatus()
  }

  const handleMonitorLiveChannel = async (login: string) => {
    const normalized = login.trim().toLowerCase()
    let target = channels.find((c) => c.channel_login === normalized)
    if (!target) {
      target = await createChannel({
        channel_login: normalized,
        display_name: normalized,
        hype_threshold: 68,
      })
      setChannels((prev) => [target as Channel, ...prev.filter((c) => c.id !== target?.id)])
    }
    setSelectedChannelId(target.id)
    if (!target.is_monitoring) {
      const started = await startChannelMonitor(target.id)
      setChannels((prev) => prev.map((c) => (c.id === started.id ? started : c)))
    }
    setVodModalOpen(false)
    void loadStatus()
  }

  const handleUpdateChannel = async (channelId: number, payload: ChannelUpdateInput) => {
    const updated = await updateChannel(channelId, payload)
    setChannels((prev) => prev.map((c) => (c.id === channelId ? updated : c)))
  }

  const handleDeleteChannel = async (channelId: number) => {
    await deleteChannel(channelId)
    setChannels((prev) => prev.filter((c) => c.id !== channelId))
    setSelectedChannelId((prev) => (prev === channelId ? null : prev))
    void loadStatus()
  }

  const handleStartMonitor = async (channelId: number) => {
    const updated = await startChannelMonitor(channelId)
    setChannels((prev) => prev.map((c) => (c.id === channelId ? updated : c)))
    setSelectedChannelId(channelId)
    void loadStatus()
  }

  const handleStopMonitor = async (channelId: number) => {
    const updated = await stopChannelMonitor(channelId)
    setChannels((prev) => prev.map((c) => (c.id === channelId ? updated : c)))
    void loadStatus()
  }

  const handleClipNow = async (channelId: number, durationSec?: number) => {
    const created = await triggerClipNow(channelId, durationSec)
    setClips((prev) => [created, ...prev.filter((c) => c.id !== created.id)])
    setSelectedFolderKey(getClipFolderKey(created))
    setSelectedClipId(created.id)
    void loadStatus()
  }

  const handleUpdateClip = async (clipId: string, payload: ClipUpdateInput) => {
    const updated = await updateClip(clipId, payload)
    setClips((prev) => prev.map((c) => (c.id === clipId ? updated : c)))
    return updated
  }

  const handleDetectWebcam = async (clipId: string) => {
    const updated = await detectClipWebcam(clipId)
    setClips((prev) => prev.map((c) => (c.id === clipId ? updated : c)))
    return updated
  }

  const handleSavePreset = async (clipId: string) => {
    const updatedChannel = await saveClipPreset(clipId)
    setChannels((prev) =>
      prev.map((ch) => (ch.id === updatedChannel.id ? updatedChannel : ch)),
    )
  }

  const handleRenderClip = async (clipId: string) => {
    setClips((prev) =>
      prev.map((c) => (c.id === clipId ? { ...c, status: 'rendering', error_message: null } : c)),
    )
    const res = await renderClipExport(clipId)
    if ('id' in res) {
      setClips((prev) => prev.map((c) => (c.id === clipId ? res : c)))
    }
    void loadStatus()
  }

  const handleGenerateCaptions = async (clipId: string) => {
    const updated = await generateClipCaptions(clipId)
    setClips((prev) => prev.map((c) => (c.id === clipId ? updated : c)))
    return updated
  }

  const handleGenerateThumbnail = async (clipId: string, payload: ThumbnailRequest) => {
    const updated = await generateClipThumbnail(clipId, payload)
    setClips((prev) => prev.map((c) => (c.id === clipId ? updated : c)))
    return updated
  }

  const handleDeleteClip = async (clipId: string) => {
    await deleteClip(clipId)
    setClips((prev) => {
      const next = prev.filter((c) => c.id !== clipId)
      setSelectedClipId((curr) => (curr === clipId ? next[0]?.id ?? null : curr))
      return next
    })
    void loadStatus()
  }

  const handleAnalyzeUrl = async (payload: VideoAnalyzeUrlRequest) => {
    const res = await startVideoUrlAnalysis(payload)
    setActiveVodJob({
      job_id: res.job_id,
      vod_url: payload.video_url,
      stage: 'Queued',
      progress: 5,
      message: 'Starting full-video viral analysis.',
      status: res.status,
    })
    return res.job_id
  }

  const handleAnalyzeUpload = async (params: VideoAnalyzeUploadParams) => {
    const res = await startVideoUploadAnalysis(params)
    setActiveVodJob({
      job_id: res.job_id,
      vod_url: params.file.name,
      stage: 'Queued',
      progress: 5,
      message: `Analyzing ${params.file.name} for viral moments.`,
      status: res.status,
    })
    return res.job_id
  }

  const handleSaveTwitchConfig = async (clientId: string, clientSecret: string) => {
    const updated = await saveTwitchConfig(clientId, clientSecret)
    setTwitchStatus(updated)
  }

  const handleStartTwitchOAuth = async (mode: 'viewer' | 'broadcaster') => {
    const res = await startTwitchOAuth(mode)
    return res.authorize_url
  }

  const handleDisconnectTwitchOAuth = async () => {
    const updated = await disconnectTwitchOAuth()
    setTwitchStatus(updated)
  }

  const handleFetchTopClips = async (login: string, period: ClipPeriod) => {
    return fetchTwitchTopClips(login, period)
  }

  const handleImportTwitchClip = async (
    clipUrl: string,
    broadcasterLogin?: string | null,
    preferPortrait = false,
  ) => {
    const imported = await importTwitchClip(clipUrl, broadcasterLogin, preferPortrait)
    setClips((prev) => [imported, ...prev.filter((c) => c.id !== imported.id)])
    setSelectedFolderKey(getClipFolderKey(imported))
    setSelectedClipId(imported.id)
    setTwitchModalOpen(false)
    void loadStatus()
  }

  const handlePublishChannelClip = async (channelId: number) => {
    return publishChannelClip(channelId)
  }

  const handleCreateChannelMarker = async (channelId: number) => {
    return createChannelMarker(channelId)
  }

  const pendingCount = useMemo(
    () =>
      systemStatus?.pending_clips ??
      clips.filter((c) => c.status === 'pending_review').length,
    [clips, systemStatus?.pending_clips],
  )

  const exportedCount = useMemo(
    () =>
      systemStatus?.exported_clips ??
      clips.filter((c) => c.status === 'exported').length,
    [clips, systemStatus?.exported_clips],
  )

  const clipFolders = useMemo(() => groupClipsIntoFolders(clips), [clips])

  const activeFolderKey = useMemo(() => {
    if (selectedFolderKey === '__all__') return '__all__'
    if (
      selectedFolderKey !== '__latest__' &&
      clipFolders.some((f) => f.key === selectedFolderKey)
    ) {
      return selectedFolderKey
    }
    return clipFolders[0]?.key ?? '__all__'
  }, [clipFolders, selectedFolderKey])

  const handleSelectFolder = useCallback(
    (folderKey: string) => {
      setSelectedFolderKey(folderKey)
      if (folderKey === '__all__') return
      const matching = clips.filter(
        (c) => c.status !== 'discarded' && getClipFolderKey(c) === folderKey,
      )
      const firstPending = matching.find((c) => c.status === 'pending_review')
      const pick = firstPending ?? matching[0]
      if (pick) {
        setSelectedClipId(pick.id)
      }
    },
    [clips],
  )

  return (
    <div className="desk-shell">
      <a className="skip-link" href="#main-workbench">
        Skip to content
      </a>

      <TopBar
        systemStatus={systemStatus}
        statusLoading={statusLoading}
        statusError={statusError}
        wsState={wsState}
        pendingCount={pendingCount}
        exportedCount={exportedCount}
        twitchStatus={twitchStatus}
        onRetryStatus={loadStatus}
        onOpenVodModal={() => setVodModalOpen(true)}
        onOpenTwitchModal={() => setTwitchModalOpen(true)}
      />

      <div className="desk-workspace">
        <ChannelRail
          channels={channels}
          loading={channelsLoading}
          error={channelsError}
          selectedChannelId={selectedChannel?.id ?? null}
          latestTicks={latestTicks}
          metadataByChannel={metadataByChannel}
          twitchStatus={twitchStatus}
          clipFolders={clipFolders}
          activeFolderKey={activeFolderKey}
          onSelectFolder={handleSelectFolder}
          onSelectChannel={setSelectedChannelId}
          onRetry={loadChannels}
          onCreateChannel={handleCreateChannel}
          onUpdateChannel={handleUpdateChannel}
          onDeleteChannel={handleDeleteChannel}
          onStartMonitor={handleStartMonitor}
          onStopMonitor={handleStopMonitor}
          onClipNow={handleClipNow}
          onPublishClip={handlePublishChannelClip}
          onCreateMarker={handleCreateChannelMarker}
        />

        <main id="main-workbench" className="desk-main">
          <LiveRadarPanel
            channel={selectedChannel}
            ticks={selectedChannel ? (ticksByChannel[selectedChannel.id] ?? []) : []}
            chatMessages={selectedChannel ? (chatByChannel[selectedChannel.id] ?? []) : []}
          />

          <ReviewWorkbench
            clips={clips}
            clipFolders={clipFolders}
            activeFolderKey={activeFolderKey}
            onSelectFolder={handleSelectFolder}
            loading={clipsLoading}
            error={clipsError}
            selectedClipId={selectedClipId}
            onSelectClip={setSelectedClipId}
            onRetryClips={loadClips}
            onUpdateClip={handleUpdateClip}
            onDetectWebcam={handleDetectWebcam}
            onSavePreset={handleSavePreset}
            onRenderClip={handleRenderClip}
            onGenerateCaptions={handleGenerateCaptions}
            onGenerateThumbnail={handleGenerateThumbnail}
            onDeleteClip={handleDeleteClip}
            onOpenVodModal={() => setVodModalOpen(true)}
          />
        </main>
      </div>

      <VodScanModal
        open={vodModalOpen}
        channels={channels}
        activeJob={activeVodJob}
        onClose={() => {
          setVodModalOpen(false)
          setActiveVodJob((curr) =>
            curr &&
            (curr.status === 'completed' ||
              curr.status === 'failed' ||
              curr.stage === 'complete' ||
              curr.progress >= 100)
              ? null
              : curr,
          )
        }}
        onAnalyzeUrl={handleAnalyzeUrl}
        onAnalyzeUpload={handleAnalyzeUpload}
        onMonitorLiveChannel={handleMonitorLiveChannel}
      />

      <TwitchSettingsModal
        open={twitchModalOpen}
        channels={channels}
        twitchStatus={twitchStatus}
        statusLoading={twitchStatusLoading}
        onClose={() => setTwitchModalOpen(false)}
        onRefreshStatus={loadTwitchStatus}
        onSaveConfig={handleSaveTwitchConfig}
        onStartOAuth={handleStartTwitchOAuth}
        onDisconnectOAuth={handleDisconnectTwitchOAuth}
        onFetchTopClips={handleFetchTopClips}
        onImportClip={handleImportTwitchClip}
      />
    </div>
  )
}
export default App
