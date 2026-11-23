export type LayoutMode = 'split_top_cam' | 'blur_no_cam'

export type CaptionStyle = 'bold_pop' | 'clean_box' | 'classic_cc'

export type CaptionPosition = 'seam' | 'lower_safe'

export type ClipPeriod = '24h' | '7d' | '30d'

export type ClipStatus =
  | 'pending_review'
  | 'rendering'
  | 'exported'
  | 'discarded'
  | 'failed'

export interface CaptionWord {
  start: number
  end: number
  word: string
}

export interface CaptionSegment {
  start: number
  end: number
  text: string
  words?: CaptionWord[]
}

export interface ThumbnailRequest {
  at_sec?: number | null
  title_overlay?: boolean | null
  title_text?: string | null
}

export interface CropBox {
  x: number
  y: number
  w: number
  h: number
}

export interface ChatSample {
  author: string
  text: string
  ts: number | string
}

export interface HypeTimelinePoint {
  t: number
  chat: number
  audio: number
  motion: number
  fused: number
}

export interface Channel {
  id: number
  platform: string
  channel_login: string
  display_name: string
  is_monitoring: boolean
  auto_monitor_online?: boolean
  broadcaster_id?: string | null
  hype_threshold: number
  pre_roll_sec: number
  post_roll_sec: number
  cooldown_sec: number
  weight_chat: number
  weight_audio: number
  weight_motion: number
  preset_layout_mode: LayoutMode
  preset_split_ratio: number
  preset_webcam_json: string | null
  preset_gameplay_json: string | null
  preset_webcam: CropBox | null
  preset_gameplay: CropBox | null
  created_at: string
}

export interface ChannelCreateInput {
  channel_login: string
  display_name?: string
  auto_monitor_online?: boolean
  hype_threshold?: number
  pre_roll_sec?: number
  post_roll_sec?: number
  cooldown_sec?: number
}

export interface ChannelUpdateInput {
  display_name?: string
  auto_monitor_online?: boolean
  hype_threshold?: number
  pre_roll_sec?: number
  post_roll_sec?: number
  cooldown_sec?: number
  weight_chat?: number
  weight_audio?: number
  weight_motion?: number
  preset_layout_mode?: LayoutMode
  preset_split_ratio?: number
  preset_webcam?: CropBox | null
  preset_gameplay?: CropBox | null
}

export interface ClipItem {
  id: string
  channel_id: number
  channel_login: string
  source_type: 'live' | 'vod' | 'twitch_clip' | 'youtube_vod' | 'video_upload' | string
  source_ref: string
  title: string
  status: ClipStatus
  hype_score: number
  trigger_reason: string
  trigger_chat_samples_json: string
  hype_timeline_json: string
  trigger_chat_samples?: ChatSample[]
  hype_timeline?: HypeTimelinePoint[]
  raw_video_path: string
  thumbnail_path: string | null
  export_video_path: string | null
  duration_sec: number
  trim_start_sec: number
  trim_end_sec: number
  layout_mode: LayoutMode
  split_ratio: number
  webcam_box_json: string | null
  gameplay_box_json: string | null
  webcam_box?: CropBox | null
  gameplay_box?: CropBox | null
  webcam_confidence: number
  normalize_audio: boolean
  captions_enabled?: boolean
  caption_style?: CaptionStyle | string
  caption_position?: CaptionPosition | string
  captions_json?: string
  captions?: CaptionSegment[]
  export_srt_path?: string | null
  export_thumb_path?: string | null
  thumb_time_sec?: number | null
  thumb_title_overlay?: boolean
  encoder_used: string | null
  error_message: string | null
  captured_at: string
  exported_at: string | null
}

export interface ClipUpdateInput {
  title?: string
  status?: ClipStatus
  trim_start_sec?: number
  trim_end_sec?: number
  layout_mode?: LayoutMode
  split_ratio?: number
  normalize_audio?: boolean
  captions_enabled?: boolean
  caption_style?: CaptionStyle
  caption_position?: CaptionPosition
  captions?: CaptionSegment[]
  captions_json?: string
  thumb_time_sec?: number | null
  thumb_title_overlay?: boolean
  webcam_box?: CropBox | null
  gameplay_box?: CropBox | null
}

export interface SystemStatus {
  ffmpeg_path: string
  ffprobe_path: string
  nvenc_available: boolean
  gpu_name: string | null
  active_monitors: number
  pending_clips: number
  exported_clips: number
  data_dir: string
}

export interface VodScanRequest {
  vod_url: string
  channel_id?: number | null
  max_clips?: number
  hype_threshold?: number
}

export interface VodScanResponse {
  job_id: string
  status: string
}

export interface VideoAnalyzeUrlRequest {
  video_url: string
  channel_id?: number | null
  max_clips?: number
  min_clip_sec?: number
  max_clip_sec?: number
  viral_threshold?: number
  use_speech_analysis?: boolean
}

export interface VideoAnalyzeUploadParams {
  file: File
  channel_id?: number | null
  max_clips?: number
  min_clip_sec?: number
  max_clip_sec?: number
  viral_threshold?: number
  use_speech_analysis?: boolean
}

export interface VideoAnalyzeResponse {
  job_id: string
  status: string
  platform?: string
  source_type?: string
}

export interface TwitchStatus {
  configured: boolean
  client_id_masked: string | null
  app_token_ready: boolean
  user_connected: boolean
  user_login: string | null
  user_id: string | null
  scopes: string[]
  has_broadcaster_scopes: boolean
  eventsub_connected: boolean
  redirect_uri: string
}

export interface TwitchOAuthStartResponse {
  authorize_url: string
  state: string
  redirect_uri: string
}

export interface TwitchClipSummary {
  id: string
  url: string
  embed_url?: string
  broadcaster_id?: string
  broadcaster_name?: string
  creator_name?: string
  title: string
  view_count: number
  created_at: string
  thumbnail_url?: string
  duration: number
  vod_offset?: number | null
}

export interface PublishClipRequest {
  title?: string | null
  duration?: number | null
  vod_id?: string | null
  vod_offset?: number | null
}

export interface PublishClipResponse {
  id: string
  edit_url: string
}

export interface StreamMarkerResponse {
  id?: string
  created_at?: string
  description?: string
  position_seconds?: number
  url?: string
  URL?: string
}

export interface HypeTickEvent {
  channel_id: number
  channel_login: string
  t: number
  chat_score: number
  audio_score: number
  motion_score: number
  fused_score: number
  threshold?: number
  unique_chatters?: number
  top_tokens?: string[]
  should_trigger?: boolean
}

export interface ChatBurstEvent {
  channel_id: number
  channel_login: string
  author: string
  text: string
  ts: number | string
}

export interface ChannelMetadataEvent {
  channel_id: number
  channel_login: string
  is_live: boolean
  game_name?: string | null
  stream_title?: string | null
  viewer_count?: number | null
  broadcaster_id?: string | null
}

export interface RenderStatusEvent {
  clip_id: string
  status: ClipStatus
  stage?: string
  export_video_path?: string | null
  export_srt_path?: string | null
  export_thumb_path?: string | null
  encoder_used?: string | null
  error_message?: string | null
  error?: string | null
  progress?: number
}

export interface VodProgressEvent {
  job_id: string
  vod_url?: string
  stage?: string
  progress: number
  message?: string
  clips_found?: number
  status?: string
  error?: string
}

export interface MonitorStatusEvent {
  channel_id?: number
  channel_login?: string
  is_monitoring?: boolean
  active_channel_ids?: number[]
  status?: string
  error?: string
}

export type LiveSocketEvent =
  | { type: 'hype_tick'; payload: HypeTickEvent }
  | { type: 'chat_burst'; payload: ChatBurstEvent }
  | { type: 'channel_metadata'; payload: ChannelMetadataEvent }
  | { type: 'clip_created'; payload: ClipItem | { clip: ClipItem } }
  | { type: 'render_status'; payload: RenderStatusEvent }
  | { type: 'vod_progress'; payload: VodProgressEvent }
  | { type: 'monitor_status'; payload: MonitorStatusEvent }
