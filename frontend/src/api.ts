import type {
  CaptionSegment,
  Channel,
  ChannelCreateInput,
  ChannelUpdateInput,
  ChatSample,
  ClipFolder,
  ClipItem,
  ClipPeriod,
  ClipUpdateInput,
  CropBox,
  HypeTimelinePoint,
  LiveSocketEvent,
  PublishClipRequest,
  PublishClipResponse,
  StreamMarkerResponse,
  SystemStatus,
  ThumbnailRequest,
  TwitchClipSummary,
  TwitchOAuthStartResponse,
  TwitchStatus,
  VideoAnalyzeResponse,
  VideoAnalyzeUploadParams,
  VideoAnalyzeUrlRequest,
  VodScanRequest,
  VodScanResponse,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const API_ACCESS_TOKEN = import.meta.env.VITE_STREAMCLIPPERS_API_TOKEN?.trim() ?? ''

function authorizedHeaders(headers?: HeadersInit): Headers {
  const result = new Headers(headers)
  if (API_ACCESS_TOKEN) {
    result.set('Authorization', `Bearer ${API_ACCESS_TOKEN}`)
  }
  return result
}

function authorizedMediaUrl(path: string): string {
  if (!API_ACCESS_TOKEN) return path
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}access_token=${encodeURIComponent(API_ACCESS_TOKEN)}`
}

function extractErrorMessage(body: unknown, fallback: string): string {
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0]
      if (typeof first === 'object' && first !== null && 'msg' in first) {
        return String((first as { msg: unknown }).msg)
      }
    }
  }
  return fallback
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...Object.fromEntries(authorizedHeaders(init?.headers).entries()),
    },
  })
  if (!res.ok) {
    let parsed: unknown = null
    try {
      parsed = await res.json()
    } catch {
      parsed = null
    }
    const fallback =
      res.status >= 500 && parsed === null
        ? 'Backend server unavailable on 127.0.0.1:8430.'
        : `Request failed with status ${res.status}`
    throw new ApiError(res.status, extractErrorMessage(parsed, fallback))
  }
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

const RESERVED_TWITCH_PATHS = new Set([
  'videos',
  'directory',
  'downloads',
  'jobs',
  'p',
  'clip',
  'clips',
  'moderator',
  'settings',
  'subscriptions',
  'inventory',
  'drops',
  'wallet',
  'search',
  'turbo',
  'login',
  'signup',
  'popout',
  'embed',
  'u',
  'products',
  'store',
])

const ALLOWED_TWITCH_HOSTS = new Set(['twitch.tv', 'www.twitch.tv', 'm.twitch.tv'])

export function extractTwitchLogin(raw: string): string | null {
  const cleaned = raw.trim()
  if (!cleaned || /\s/.test(cleaned)) {
    return null
  }
  if (cleaned.startsWith('@') && !cleaned.includes('/') && !cleaned.includes('.')) {
    const slug = cleaned.slice(1).toLowerCase()
    return /^[a-z0-9_]{3,25}$/.test(slug) ? slug : null
  }
  const lower = cleaned.toLowerCase()
  if (
    cleaned.includes('://') ||
    lower.startsWith('twitch.tv/') ||
    lower.startsWith('www.twitch.tv/') ||
    lower.startsWith('m.twitch.tv/')
  ) {
    const candidate = cleaned.includes('://') ? cleaned : `https://${cleaned}`
    try {
      const parsed = new URL(candidate)
      if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
        return null
      }
      if (parsed.username !== '' || parsed.password !== '' || parsed.port !== '') {
        return null
      }
      if (!ALLOWED_TWITCH_HOSTS.has(parsed.hostname.toLowerCase())) {
        return null
      }
      const parts = parsed.pathname.split('/').filter(Boolean)
      if (parts.length !== 1) {
        return null
      }
      const slug = parts[0].toLowerCase()
      if (RESERVED_TWITCH_PATHS.has(slug)) {
        return null
      }
      return /^[a-z0-9_]{3,25}$/.test(slug) ? slug : null
    } catch {
      return null
    }
  }
  return /^[a-zA-Z0-9_]{3,25}$/.test(cleaned) ? cleaned.toLowerCase() : null
}

export function isValidTwitchLogin(raw: string): boolean {
  return extractTwitchLogin(raw) !== null
}

export function isValidTwitchVodUrl(raw: string): boolean {
  return /^https:\/\/(www\.|m\.)?twitch\.tv\/videos\/\d+\/?$/.test(raw.trim())
}

const ALLOWED_YOUTUBE_HOSTS = new Set(['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'])
const YOUTUBE_ID_RE = /^[A-Za-z0-9_-]{11}$/

export function detectVideoUrlPlatform(raw: string): 'youtube' | 'twitch_vod' | null {
  const cleaned = raw.trim()
  if (!cleaned || /\s/.test(cleaned)) {
    return null
  }
  if (isValidTwitchVodUrl(cleaned)) {
    return 'twitch_vod'
  }
  try {
    const parsed = new URL(cleaned)
    if (parsed.protocol !== 'https:' || parsed.username !== '' || parsed.password !== '' || parsed.port !== '') {
      return null
    }
    const host = parsed.hostname.toLowerCase()
    if (!ALLOWED_YOUTUBE_HOSTS.has(host)) {
      return null
    }
    const trimmedPath = parsed.pathname.replace(/\/+$/, '')
    if (host === 'youtu.be') {
      const seg = trimmedPath.replace(/^\/+/, '')
      return YOUTUBE_ID_RE.test(seg) ? 'youtube' : null
    }
    if (trimmedPath === '/watch') {
      const vid = parsed.searchParams.get('v') ?? ''
      return YOUTUBE_ID_RE.test(vid) ? 'youtube' : null
    }
    const parts = trimmedPath.split('/').filter(Boolean)
    if (parts.length === 2 && (parts[0] === 'live' || parts[0] === 'shorts') && YOUTUBE_ID_RE.test(parts[1])) {
      return 'youtube'
    }
    return null
  } catch {
    return null
  }
}

export function isValidTwitchClipUrl(raw: string): boolean {
  const cleaned = raw.trim()
  return (
    /^https:\/\/clips\.twitch\.tv\/[A-Za-z0-9_-]+/.test(cleaned) ||
    /^https:\/\/(www\.|m\.)?twitch\.tv\/[a-zA-Z0-9_]{3,25}\/clip\/[A-Za-z0-9_-]+/.test(cleaned)
  )
}

export function parseCropBox(
  obj: CropBox | null | undefined,
  json: string | null | undefined,
): CropBox | null {
  if (obj && typeof obj.x === 'number' && typeof obj.y === 'number' && typeof obj.w === 'number' && typeof obj.h === 'number') {
    return obj
  }
  if (!json) return null
  try {
    const parsed: unknown = JSON.parse(json)
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      'x' in parsed &&
      'y' in parsed &&
      'w' in parsed &&
      'h' in parsed
    ) {
      const box = parsed as Record<string, unknown>
      if (
        typeof box.x === 'number' &&
        typeof box.y === 'number' &&
        typeof box.w === 'number' &&
        typeof box.h === 'number'
      ) {
        return { x: box.x, y: box.y, w: box.w, h: box.h }
      }
    }
  } catch {
    return null
  }
  return null
}

export function parseTimeline(clip: ClipItem): HypeTimelinePoint[] {
  if (Array.isArray(clip.hype_timeline) && clip.hype_timeline.length > 0) {
    return clip.hype_timeline
  }
  try {
    const parsed: unknown = JSON.parse(clip.hype_timeline_json || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is HypeTimelinePoint =>
        typeof item === 'object' &&
        item !== null &&
        typeof (item as HypeTimelinePoint).t === 'number' &&
        typeof (item as HypeTimelinePoint).fused === 'number',
    )
  } catch {
    return []
  }
}

export function parseChatSamples(clip: ClipItem): ChatSample[] {
  if (Array.isArray(clip.trigger_chat_samples) && clip.trigger_chat_samples.length > 0) {
    return clip.trigger_chat_samples
  }
  try {
    const parsed: unknown = JSON.parse(clip.trigger_chat_samples_json || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is ChatSample =>
        typeof item === 'object' &&
        item !== null &&
        typeof (item as ChatSample).author === 'string' &&
        typeof (item as ChatSample).text === 'string',
    )
  } catch {
    return []
  }
}

export function parseCaptions(clip: ClipItem): CaptionSegment[] {
  if (Array.isArray(clip.captions) && clip.captions.length > 0) {
    return clip.captions
  }
  try {
    const parsed: unknown = JSON.parse(clip.captions_json || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is CaptionSegment =>
        typeof item === 'object' &&
        item !== null &&
        typeof (item as CaptionSegment).start === 'number' &&
        typeof (item as CaptionSegment).end === 'number' &&
        typeof (item as CaptionSegment).text === 'string',
    )
  } catch {
    return []
  }
}

export function getClipFolderKey(clip: ClipItem): string {
  const ref = (clip.source_ref || '').trim()
  if (ref) {
    return ref
  }
  return `${clip.source_type || 'live'}:${clip.channel_login || 'default'}`
}

export function getClipFolderMeta(clip: ClipItem): { label: string; sublabel: string } {
  const ref = (clip.source_ref || '').trim()
  if (clip.source_type === 'youtube_vod') {
    let ytId = ref
    try {
      const parsed = new URL(ref)
      ytId =
        parsed.searchParams.get('v') ||
        parsed.pathname.split('/').filter(Boolean).pop() ||
        ref
    } catch {
      ytId = ref
    }
    const sub =
      clip.channel_login && clip.channel_login !== 'twitch_vod'
        ? `@${clip.channel_login}`
        : 'YouTube Video'
    return { label: `YouTube · ${ytId}`, sublabel: sub }
  }

  if (clip.source_type === 'vod') {
    const match = ref.match(/\/videos\/(\d+)/)
    const vodId = match ? match[1] : ref || 'VOD'
    return {
      label: `VOD · #${vodId}`,
      sublabel: clip.channel_login ? `@${clip.channel_login}` : 'Twitch VOD',
    }
  }

  if (clip.source_type === 'video_upload') {
    return {
      label: ref || 'Uploaded Video',
      sublabel: 'Local Video File',
    }
  }

  if (clip.source_type === 'twitch_clip') {
    return {
      label: `@${clip.channel_login} · Clips`,
      sublabel: 'Imported Twitch Clips',
    }
  }

  return {
    label: `@${clip.channel_login} · Live`,
    sublabel: 'Live Stream Buffer',
  }
}

export function groupClipsIntoFolders(clips: ClipItem[]): ClipFolder[] {
  const map = new Map<string, ClipFolder>()
  for (const clip of clips) {
    if (clip.status === 'discarded') continue
    const key = getClipFolderKey(clip)
    const existing = map.get(key)
    if (!existing) {
      const meta = getClipFolderMeta(clip)
      map.set(key, {
        key,
        label: meta.label,
        sublabel: meta.sublabel,
        sourceType: clip.source_type,
        clipCount: 1,
        pendingCount: clip.status === 'pending_review' ? 1 : 0,
        exportedCount: clip.status === 'exported' ? 1 : 0,
        latestAt: clip.captured_at || '',
      })
    } else {
      existing.clipCount += 1
      if (clip.status === 'pending_review') existing.pendingCount += 1
      if (clip.status === 'exported') existing.exportedCount += 1
      if ((clip.captured_at || '') > existing.latestAt) {
        existing.latestAt = clip.captured_at || existing.latestAt
      }
    }
  }
  return Array.from(map.values()).sort((a, b) => b.latestAt.localeCompare(a.latestAt))
}

export function fetchSystemStatus(): Promise<SystemStatus> {
  return requestJson<SystemStatus>('/api/system/status')
}

export function fetchChannels(): Promise<Channel[]> {
  return requestJson<Channel[]>('/api/channels')
}

export function createChannel(payload: ChannelCreateInput): Promise<Channel> {
  return requestJson<Channel>('/api/channels', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateChannel(channelId: number, payload: ChannelUpdateInput): Promise<Channel> {
  return requestJson<Channel>(`/api/channels/${channelId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteChannel(channelId: number): Promise<void> {
  return requestJson<void>(`/api/channels/${channelId}`, {
    method: 'DELETE',
  })
}

export function startChannelMonitor(channelId: number): Promise<Channel> {
  return requestJson<Channel>(`/api/channels/${channelId}/start`, {
    method: 'POST',
  })
}

export function stopChannelMonitor(channelId: number): Promise<Channel> {
  return requestJson<Channel>(`/api/channels/${channelId}/stop`, {
    method: 'POST',
  })
}

export function triggerClipNow(channelId: number, durationSec?: number): Promise<ClipItem> {
  return requestJson<ClipItem>(`/api/channels/${channelId}/clip-now`, {
    method: 'POST',
    ...(durationSec != null ? { body: JSON.stringify({ duration_sec: durationSec }) } : {}),
  })
}

export function startVodScan(payload: VodScanRequest): Promise<VodScanResponse> {
  return requestJson<VodScanResponse>('/api/vods/scan', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function startVideoUrlAnalysis(payload: VideoAnalyzeUrlRequest): Promise<VideoAnalyzeResponse> {
  return requestJson<VideoAnalyzeResponse>('/api/videos/analyze-url', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function startVideoUploadAnalysis(
  params: VideoAnalyzeUploadParams,
): Promise<VideoAnalyzeResponse> {
  const form = new FormData()
  form.append('file', params.file)
  if (params.channel_id != null) {
    form.append('channel_id', String(params.channel_id))
  }
  if (params.max_clips != null) {
    form.append('max_clips', String(params.max_clips))
  }
  if (params.min_clip_sec != null) {
    form.append('min_clip_sec', String(params.min_clip_sec))
  }
  if (params.max_clip_sec != null) {
    form.append('max_clip_sec', String(params.max_clip_sec))
  }
  if (params.viral_threshold != null) {
    form.append('viral_threshold', String(params.viral_threshold))
  }
  if (params.use_speech_analysis != null) {
    form.append('use_speech_analysis', String(params.use_speech_analysis))
  }

  const res = await fetch('/api/videos/analyze-upload', {
    method: 'POST',
    body: form,
    headers: authorizedHeaders(),
  })
  if (!res.ok) {
    let parsed: unknown = null
    try {
      parsed = await res.json()
    } catch {
      parsed = null
    }
    const fallback =
      res.status >= 500 && parsed === null
        ? 'Backend server unavailable on 127.0.0.1:8430.'
        : `Upload failed with status ${res.status}`
    throw new ApiError(res.status, extractErrorMessage(parsed, fallback))
  }
  return (await res.json()) as VideoAnalyzeResponse
}

export function fetchClips(params?: { status?: string; channel_id?: number }): Promise<ClipItem[]> {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.channel_id != null) qs.set('channel_id', String(params.channel_id))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return requestJson<ClipItem[]>(`/api/clips${suffix}`)
}

export function updateClip(clipId: string, payload: ClipUpdateInput): Promise<ClipItem> {
  return requestJson<ClipItem>(`/api/clips/${clipId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteClip(clipId: string): Promise<void> {
  return requestJson<void>(`/api/clips/${clipId}`, {
    method: 'DELETE',
  })
}

export function detectClipWebcam(clipId: string): Promise<ClipItem> {
  return requestJson<ClipItem>(`/api/clips/${clipId}/detect-webcam`, {
    method: 'POST',
  })
}

export function saveClipPreset(clipId: string): Promise<Channel> {
  return requestJson<Channel>(`/api/clips/${clipId}/save-preset`, {
    method: 'POST',
  })
}

export function renderClipExport(clipId: string): Promise<ClipItem | { status: string; clip_id: string }> {
  return requestJson<ClipItem | { status: string; clip_id: string }>(`/api/clips/${clipId}/render`, {
    method: 'POST',
  })
}

export function generateClipCaptions(
  clipId: string,
  options?: { streamer_only?: boolean; sensitivity?: number },
): Promise<ClipItem> {
  return requestJson<ClipItem>(`/api/clips/${clipId}/captions/generate`, {
    method: 'POST',
    body: JSON.stringify({
      streamer_only: options?.streamer_only ?? true,
      sensitivity: options?.sensitivity ?? 0.5,
    }),
  })
}

export function generateClipThumbnail(clipId: string, payload: ThumbnailRequest): Promise<ClipItem> {
  return requestJson<ClipItem>(`/api/clips/${clipId}/thumbnail`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function fetchTwitchStatus(): Promise<TwitchStatus> {
  return requestJson<TwitchStatus>('/api/twitch/status')
}

export function saveTwitchConfig(clientId: string, clientSecret: string): Promise<TwitchStatus> {
  return requestJson<TwitchStatus>('/api/twitch/config', {
    method: 'PUT',
    body: JSON.stringify({
      client_id: clientId,
      client_secret: clientSecret,
    }),
  })
}

export function startTwitchOAuth(mode: 'viewer' | 'broadcaster'): Promise<TwitchOAuthStartResponse> {
  return requestJson<TwitchOAuthStartResponse>(
    `/api/twitch/oauth/start?mode=${encodeURIComponent(mode)}`,
  )
}

export function disconnectTwitchOAuth(): Promise<TwitchStatus> {
  return requestJson<TwitchStatus>('/api/twitch/oauth/disconnect', {
    method: 'POST',
  })
}

const PERIOD_MAP: Record<ClipPeriod, 'day' | 'week' | 'month'> = {
  '24h': 'day',
  '7d': 'week',
  '30d': 'month',
}

export async function fetchTwitchTopClips(
  login: string,
  period: ClipPeriod = '24h',
): Promise<TwitchClipSummary[]> {
  const mappedPeriod = PERIOD_MAP[period]
  const res = await requestJson<
    TwitchClipSummary[] | { clips?: TwitchClipSummary[]; data?: TwitchClipSummary[] }
  >(
    `/api/twitch/clips/${encodeURIComponent(login.trim().toLowerCase())}?period=${mappedPeriod}&first=20`,
  )
  if (Array.isArray(res)) return res
  if (Array.isArray(res.clips)) return res.clips
  if (Array.isArray(res.data)) return res.data
  return []
}

export function importTwitchClip(
  clipUrl: string,
  broadcasterLogin?: string | null,
  preferPortrait = false,
): Promise<ClipItem> {
  return requestJson<ClipItem>('/api/twitch/clips/import', {
    method: 'POST',
    body: JSON.stringify({
      clip_url: clipUrl.trim(),
      broadcaster_login: broadcasterLogin || null,
      prefer_portrait: preferPortrait,
    }),
  })
}

export function publishChannelClip(
  channelId: number,
  payload: PublishClipRequest = {},
): Promise<PublishClipResponse> {
  return requestJson<PublishClipResponse>(`/api/channels/${channelId}/publish-clip`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function createChannelMarker(
  channelId: number,
  description?: string,
): Promise<StreamMarkerResponse> {
  return requestJson<StreamMarkerResponse>(`/api/channels/${channelId}/marker`, {
    method: 'POST',
    body: JSON.stringify({
      description: description?.trim() ? description.trim() : null,
    }),
  })
}

export function rawVideoUrl(clipId: string): string {
  return authorizedMediaUrl(`/api/media/raw/${encodeURIComponent(clipId)}`)
}

export function exportVideoUrl(clipId: string): string {
  return authorizedMediaUrl(`/api/media/export/${encodeURIComponent(clipId)}`)
}

export function thumbImageUrl(clipId: string): string {
  return authorizedMediaUrl(`/api/media/thumb/${encodeURIComponent(clipId)}`)
}

export function exportThumbUrl(clipId: string, cacheBust?: string | number | null): string {
  const base = `/api/media/export-thumb/${encodeURIComponent(clipId)}`
  const versioned = cacheBust != null ? `${base}?v=${encodeURIComponent(String(cacheBust))}` : base
  return authorizedMediaUrl(versioned)
}

export function exportSrtUrl(clipId: string): string {
  return authorizedMediaUrl(`/api/media/srt/${encodeURIComponent(clipId)}`)
}

function isLiveEvent(value: unknown): value is LiveSocketEvent {
  if (typeof value !== 'object' || value === null) return false
  const rec = value as Record<string, unknown>
  return typeof rec.type === 'string' && 'payload' in rec
}

export function connectLiveSocket(
  onEvent: (event: LiveSocketEvent) => void,
  onConnectionChange: (state: 'connecting' | 'open' | 'offline') => void,
): () => void {
  let closed = false
  let socket: WebSocket | null = null
  let retryTimer: number | null = null
  let attempt = 0

  const scheduleReconnect = () => {
    if (closed) return
    const delay = Math.min(8000, 1200 * 2 ** Math.min(attempt, 3))
    attempt += 1
    retryTimer = window.setTimeout(openSocket, delay)
  }

  const openSocket = () => {
    if (closed) return
    onConnectionChange('connecting')
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const endpoint = new URL(`${protocol}//${window.location.host}/ws/live`)
    if (API_ACCESS_TOKEN) {
      endpoint.searchParams.set('access_token', API_ACCESS_TOKEN)
    }
    try {
      socket = new WebSocket(endpoint)
    } catch {
      onConnectionChange('offline')
      scheduleReconnect()
      return
    }

    socket.onopen = () => {
      if (closed) return
      attempt = 0
      onConnectionChange('open')
    }

    socket.onmessage = (msg) => {
      try {
        const parsed: unknown = JSON.parse(String(msg.data))
        if (isLiveEvent(parsed)) {
          onEvent(parsed)
        }
      } catch {
        // Ignore malformed frame safely
      }
    }

    socket.onerror = () => {
      if (!closed) {
        onConnectionChange('offline')
      }
    }

    socket.onclose = () => {
      if (closed) return
      onConnectionChange('offline')
      scheduleReconnect()
    }
  }

  openSocket()

  return () => {
    closed = true
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer)
    }
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      socket.close()
    }
  }
}
