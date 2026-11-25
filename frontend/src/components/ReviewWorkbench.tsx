import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BookmarkPlus,
  Captions,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Copy,
  Download,
  Film,
  FolderOpen,
  Image,
  Layers,
  Lock,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  ScanFace,
  Scissors,
  Sparkles,
  Trash2,
  Video,
  Volume2,
  VolumeX,
  X,
} from 'lucide-react'
import {
  exportSrtUrl,
  exportThumbUrl,
  exportVideoUrl,
  getClipFolderKey,
  parseCaptions,
  parseChatSamples,
  parseCropBox,
  parseTimeline,
  rawVideoUrl,
  thumbImageUrl,
} from '../api'
import type {
  CaptionPosition,
  CaptionSegment,
  CaptionStyle,
  ClipFolder,
  ClipItem,
  ClipStatus,
  ClipUpdateInput,
  CropBox,
  LayoutMode,
  ThumbnailRequest,
} from '../types'

interface ReviewWorkbenchProps {
  clips: ClipItem[]
  clipFolders: ClipFolder[]
  activeFolderKey: string
  onSelectFolder: (folderKey: string) => void
  loading: boolean
  error: string | null
  selectedClipId: string | null
  onSelectClip: (clipId: string) => void
  onRetryClips: () => void
  onUpdateClip: (clipId: string, payload: ClipUpdateInput) => Promise<ClipItem>
  onDuplicateClip: (clipId: string) => Promise<ClipItem>
  onDetectWebcam: (clipId: string) => Promise<ClipItem>
  onSavePreset: (clipId: string) => Promise<void>
  onRenderClip: (clipId: string) => Promise<void>
  onGenerateCaptions: (
    clipId: string,
    options?: { streamer_only?: boolean },
  ) => Promise<ClipItem>
  onGenerateThumbnail: (clipId: string, payload: ThumbnailRequest) => Promise<ClipItem>
  onDeleteClip: (clipId: string) => Promise<void>
  onOpenVodModal: () => void
}

type QueueFilter = 'all' | 'pending_review' | 'exported'

const DEFAULT_WEBCAM: CropBox = { x: 0.0, y: 0.0, w: 0.24, h: 0.28 }
const DEFAULT_GAMEPLAY: CropBox = { x: 0.24, y: 0.0, w: 0.56, h: 1.0 }
const PLAYBACK_RATES = [0.5, 1, 1.5, 2] as const

function formatTimecode(sec: number): string {
  const safe = Math.max(0, Number.isFinite(sec) ? sec : 0)
  const mins = Math.floor(safe / 60)
  const rem = safe - mins * 60
  return `${String(mins).padStart(2, '0')}:${rem.toFixed(1).padStart(4, '0')}`
}

function clampBox(box: CropBox): CropBox {
  const w = Math.max(0.05, Math.min(1.0, box.w))
  const h = Math.max(0.05, Math.min(1.0, box.h))
  const x = Math.max(0.0, Math.min(1.0 - w, box.x))
  const y = Math.max(0.0, Math.min(1.0 - h, box.y))
  return {
    x: Number(x.toFixed(4)),
    y: Number(y.toFixed(4)),
    w: Number(w.toFixed(4)),
    h: Number(h.toFixed(4)),
  }
}

function formatStatusLabel(status: ClipStatus): string {
  if (status === 'pending_review') return 'Ready to Edit'
  if (status === 'rendering') return 'Rendering...'
  if (status === 'exported') return 'Exported'
  if (status === 'failed') return 'Failed'
  return 'Discarded'
}

function formatSourceBadge(sourceType: string): string {
  if (sourceType === 'youtube_vod') return 'YouTube'
  if (sourceType === 'video_upload') return 'Upload'
  if (sourceType === 'vod') return 'VOD'
  if (sourceType === 'twitch_clip') return 'Clip'
  return 'Live'
}

export function ReviewWorkbench({
  clips,
  clipFolders,
  activeFolderKey,
  onSelectFolder,
  loading,
  error,
  selectedClipId,
  onSelectClip,
  onRetryClips,
  onUpdateClip,
  onDuplicateClip,
  onDetectWebcam,
  onSavePreset,
  onRenderClip,
  onGenerateCaptions,
  onGenerateThumbnail,
  onDeleteClip,
  onOpenVodModal,
}: ReviewWorkbenchProps) {
  /* Default to 'all' so every clip (Pending + Exported) is visible and editable */
  const [filter, setFilter] = useState<QueueFilter>('all')
  /* Default to false ("This Clip Only") so users can edit every clip independently without overwriting others */
  const [syncFolderCrop, setSyncFolderCrop] = useState<boolean>(false)

  const folderClips = useMemo(() => {
    const valid = clips.filter((c) => c.status !== 'discarded')
    if (activeFolderKey === '__all__' || !activeFolderKey) {
      return valid
    }
    const inFolder = valid.filter((c) => getClipFolderKey(c) === activeFolderKey)
    return inFolder.length > 0 ? inFolder : valid
  }, [activeFolderKey, clips])

  const filteredClips = useMemo(() => {
    if (filter === 'all') return folderClips
    return folderClips.filter((c) => c.status === filter)
  }, [folderClips, filter])

  const selectedClip = useMemo(() => {
    const exactInFiltered = filteredClips.find((c) => c.id === selectedClipId)
    if (exactInFiltered) return exactInFiltered
    const exactInFolder = folderClips.find((c) => c.id === selectedClipId)
    if (exactInFolder) return exactInFolder
    return filteredClips[0] ?? folderClips[0] ?? null
  }, [filteredClips, folderClips, selectedClipId])

  const selectedIndex = useMemo(
    () => (selectedClip ? filteredClips.findIndex((c) => c.id === selectedClip.id) : -1),
    [filteredClips, selectedClip],
  )

  const [titleDraft, setTitleDraft] = useState<string>('')
  const [titleDirty, setTitleDirty] = useState<boolean>(false)
  const [titleSaving, setTitleSaving] = useState<boolean>(false)

  const [webcamBox, setWebcamBox] = useState<CropBox>(DEFAULT_WEBCAM)
  const [gameplayBox, setGameplayBox] = useState<CropBox>(DEFAULT_GAMEPLAY)
  const [layoutMode, setLayoutMode] = useState<LayoutMode>('split_top_cam')
  const [splitRatio, setSplitRatio] = useState<number>(0.35)
  const [trimStart, setTrimStart] = useState<number>(0)
  const [trimEnd, setTrimEnd] = useState<number>(10)
  const [normalizeAudio, setNormalizeAudio] = useState<boolean>(true)
  const [captionsEnabled, setCaptionsEnabled] = useState<boolean>(true)
  const [streamerVoiceOnly, setStreamerVoiceOnly] = useState<boolean>(true)
  const [captionStyle, setCaptionStyle] = useState<CaptionStyle>('bold_pop')
  const [captionPosition, setCaptionPosition] = useState<CaptionPosition>('seam')
  const [captionsDraft, setCaptionsDraft] = useState<CaptionSegment[]>([])
  const [captionsDirty, setCaptionsDirty] = useState<boolean>(false)
  const [showCaptionEditor, setShowCaptionEditor] = useState<boolean>(false)
  const [thumbTitleOverlay, setThumbTitleOverlay] = useState<boolean>(false)
  const [thumbCacheBust, setThumbCacheBust] = useState<number>(0)
  const [previewExported, setPreviewExported] = useState<boolean>(false)

  const [isPlaying, setIsPlaying] = useState(false)
  const [isMuted, setIsMuted] = useState(false)
  const [playbackRate, setPlaybackRate] = useState<number>(1)
  const [currentTime, setCurrentTime] = useState(0)
  const [videoDuration, setVideoDuration] = useState(0)
  const [videoError, setVideoError] = useState<string | null>(null)

  const [detectBusy, setDetectBusy] = useState(false)
  const [presetBusy, setPresetBusy] = useState(false)
  const [duplicateBusy, setDuplicateBusy] = useState(false)
  const [captionsBusy, setCaptionsBusy] = useState(false)
  const [captionsSaveBusy, setCaptionsSaveBusy] = useState(false)
  const [thumbBusy, setThumbBusy] = useState(false)
  const [renderBusy, setRenderBusy] = useState(false)
  const [batchRenderBusy, setBatchRenderBusy] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [actionFeedback, setActionFeedback] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const dragStateRef = useRef<{
    target: 'webcam' | 'gameplay'
    mode: 'move' | 'resize'
    startX: number
    startY: number
    origin: CropBox
  } | null>(null)

  useEffect(() => {
    if (!selectedClip) return
    setTitleDraft(selectedClip.title)
    setTitleDirty(false)
    const wb = parseCropBox(selectedClip.webcam_box, selectedClip.webcam_box_json) ?? DEFAULT_WEBCAM
    const gb = parseCropBox(selectedClip.gameplay_box, selectedClip.gameplay_box_json) ?? DEFAULT_GAMEPLAY
    setWebcamBox(wb)
    setGameplayBox(gb)
    setLayoutMode(selectedClip.layout_mode === 'blur_no_cam' ? 'blur_no_cam' : 'split_top_cam')
    setSplitRatio(selectedClip.split_ratio || 0.35)
    const dur = Math.max(1, selectedClip.duration_sec || 10)
    const start = Math.max(0, Math.min(dur - 0.5, selectedClip.trim_start_sec || 0))
    const end = Math.max(start + 0.5, Math.min(dur, selectedClip.trim_end_sec || dur))
    setTrimStart(start)
    setTrimEnd(end)
    setNormalizeAudio(selectedClip.normalize_audio)
    setCaptionsEnabled(selectedClip.captions_enabled ?? true)
    const st = selectedClip.caption_style
    setCaptionStyle(st === 'clean_box' || st === 'classic_cc' ? st : 'bold_pop')
    setCaptionPosition(selectedClip.caption_position === 'lower_safe' ? 'lower_safe' : 'seam')
    setCaptionsDraft(parseCaptions(selectedClip))
    setCaptionsDirty(false)
    setThumbTitleOverlay(selectedClip.thumb_title_overlay ?? false)
    setThumbCacheBust((n) => n + 1)
    setPreviewExported(false)
    setIsPlaying(false)
    setCurrentTime(start)
    setVideoDuration(dur)
    setVideoError(null)
    setConfirmDiscard(false)
    setActionFeedback(null)
  }, [selectedClip?.id])

  useEffect(() => {
    if (!selectedClip || captionsDirty) return
    setCaptionsDraft(parseCaptions(selectedClip))
    setThumbCacheBust((n) => n + 1)
  }, [selectedClip?.captions_json, selectedClip?.export_thumb_path])

  const drawVerticalStage = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const cw = canvas.width
    const ch = canvas.height
    const styles = getComputedStyle(document.documentElement)
    const bodyStyle = getComputedStyle(document.body)
    const bgPaper = styles.getPropertyValue('--color-inset').trim() || bodyStyle.backgroundColor
    const bgWell = styles.getPropertyValue('--color-paper-2').trim() || bgPaper
    const ruleColor = styles.getPropertyValue('--color-rule').trim() || bodyStyle.color
    const mutedColor = styles.getPropertyValue('--color-muted').trim() || bodyStyle.color
    const inkColor = styles.getPropertyValue('--color-ink').trim() || bodyStyle.color
    const accentColor = styles.getPropertyValue('--color-accent').trim() || inkColor

    const drawLiveCaptions = (seamY?: number) => {
      if (!captionsEnabled || previewExported || captionsDraft.length === 0) return
      const relTime = Math.max(0, currentTime - trimStart)
      const activeSeg = captionsDraft.find(
        (seg) => relTime >= seg.start - 0.04 && relTime <= seg.end + 0.08,
      )
      if (!activeSeg || !activeSeg.text.trim()) return

      const centerX = cw / 2
      const baseY =
        captionPosition === 'seam' && layoutMode === 'split_top_cam' && seamY != null
          ? seamY
          : layoutMode === 'split_top_cam'
            ? Math.round(ch * 0.78)
            : Math.round(ch * 0.81)
      const fontSize = captionStyle === 'bold_pop' ? 16 : 15
      ctx.save()
      ctx.font = `700 ${fontSize}px "Plus Jakarta Sans", sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'

      const words = activeSeg.text.trim().split(/\s+/)
      const lines: string[] = []
      let currentLine = ''
      for (const word of words) {
        const candidate = currentLine ? `${currentLine} ${word}` : word
        if (ctx.measureText(candidate).width > cw - 36 && currentLine) {
          lines.push(currentLine)
          currentLine = word
        } else {
          currentLine = candidate
        }
      }
      if (currentLine) lines.push(currentLine)

      const lineHeight = fontSize + 5
      const startY = baseY - ((lines.length - 1) * lineHeight) / 2

      if (captionStyle === 'clean_box') {
        const maxWidth = Math.max(...lines.map((l) => ctx.measureText(l).width))
        const boxW = Math.min(cw - 20, maxWidth + 18)
        const boxH = lines.length * lineHeight + 10
        ctx.globalAlpha = 0.88
        ctx.fillStyle = bgPaper
        ctx.fillRect(centerX - boxW / 2, startY - lineHeight / 2 - 5, boxW, boxH)
        ctx.globalAlpha = 1
      }

      const activeWordIdx =
        captionStyle === 'bold_pop' && activeSeg.words && activeSeg.words.length > 0
          ? activeSeg.words.findIndex((w) => relTime >= w.start && relTime <= w.end)
          : -1

      let wordOffset = 0
      lines.forEach((line, lineIdx) => {
        const ly = startY + lineIdx * lineHeight
        const lineWords = line.split(/\s+/)
        if (captionStyle === 'bold_pop' && activeWordIdx >= 0) {
          const spaceW = ctx.measureText(' ').width
          const totalW =
            lineWords.reduce((sum, w) => sum + ctx.measureText(w).width, 0) +
            Math.max(0, lineWords.length - 1) * spaceW
          let cursorX = centerX - totalW / 2
          ctx.textAlign = 'left'
          lineWords.forEach((w, idx) => {
            const globalIdx = wordOffset + idx
            const wWidth = ctx.measureText(w).width
            ctx.lineWidth = 4
            ctx.strokeStyle = bgPaper
            ctx.strokeText(w, cursorX, ly)
            ctx.fillStyle = globalIdx === activeWordIdx ? accentColor : inkColor
            ctx.fillText(w, cursorX, ly)
            cursorX += wWidth + spaceW
          })
          wordOffset += lineWords.length
          ctx.textAlign = 'center'
        } else {
          if (captionStyle !== 'clean_box') {
            ctx.lineWidth = captionStyle === 'bold_pop' ? 4 : 3
            ctx.strokeStyle = bgPaper
            ctx.strokeText(line, centerX, ly)
          }
          ctx.fillStyle = inkColor
          ctx.fillText(line, centerX, ly)
        }
      })
      ctx.restore()
    }

    ctx.fillStyle = bgPaper
    ctx.fillRect(0, 0, cw, ch)

    const video = videoRef.current
    if (!video || video.readyState < 2 || video.videoWidth === 0 || video.videoHeight === 0 || videoError) {
      ctx.fillStyle = bgWell
      ctx.fillRect(16, 16, cw - 32, ch - 32)
      ctx.strokeStyle = ruleColor
      ctx.lineWidth = 1
      ctx.strokeRect(16.5, 16.5, cw - 33, ch - 33)
      ctx.fillStyle = mutedColor
      ctx.font = '13px "Plus Jakarta Sans", sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText(videoError ? 'Source stream unavailable' : 'Loading 9:16 vertical stage...', cw / 2, ch / 2)
      return
    }

    const vw = video.videoWidth
    const vh = video.videoHeight

    const drawCoverCrop = (box: CropBox, dx: number, dy: number, dw: number, dh: number) => {
      const sx = box.x * vw
      const sy = box.y * vh
      const sw = Math.max(1, box.w * vw)
      const sh = Math.max(1, box.h * vh)
      const srcAspect = sw / sh
      const dstAspect = dw / dh

      let rx = sx
      let ry = sy
      let rw = sw
      let rh = sh
      if (srcAspect > dstAspect) {
        rw = sh * dstAspect
        rx = sx + (sw - rw) / 2
      } else {
        rh = sw / dstAspect
        ry = sy + (sh - rh) / 2
      }
      ctx.drawImage(video, rx, ry, rw, rh, dx, dy, dw, dh)
    }

    if (layoutMode === 'blur_no_cam') {
      ctx.save()
      ctx.filter = 'blur(18px) brightness(0.68)'
      drawCoverCrop({ x: 0, y: 0, w: 1, h: 1 }, -18, -18, cw + 36, ch + 36)
      ctx.restore()

      const sx = gameplayBox.x * vw
      const sy = gameplayBox.y * vh
      const sw = Math.max(1, gameplayBox.w * vw)
      const sh = Math.max(1, gameplayBox.h * vh)
      const aspect = sw / sh
      let dw = cw
      let dh = cw / aspect
      if (dh > ch) {
        dh = ch
        dw = ch * aspect
      }
      const dx = (cw - dw) / 2
      const dy = (ch - dh) / 2
      ctx.drawImage(video, sx, sy, sw, sh, dx, dy, dw, dh)
      drawLiveCaptions()
      return
    }

    const camH = Math.round(ch * splitRatio)
    const gameH = ch - camH
    drawCoverCrop(webcamBox, 0, 0, cw, camH)
    drawCoverCrop(gameplayBox, 0, camH, cw, gameH)

    ctx.strokeStyle = accentColor
    ctx.lineWidth = 1.5
    ctx.beginPath()
    ctx.moveTo(0, camH + 0.5)
    ctx.lineTo(cw, camH + 0.5)
    ctx.stroke()

    drawLiveCaptions(camH)
  }, [
    captionPosition,
    captionStyle,
    captionsDraft,
    captionsEnabled,
    currentTime,
    gameplayBox,
    layoutMode,
    previewExported,
    splitRatio,
    trimStart,
    videoError,
    webcamBox,
  ])

  useEffect(() => {
    drawVerticalStage()
  }, [drawVerticalStage])

  useEffect(() => {
    let rafId = 0
    const tick = () => {
      const video = videoRef.current
      if (video && !video.paused && !video.ended) {
        const now = video.currentTime
        if (now >= trimEnd) {
          video.pause()
          video.currentTime = trimStart
          setIsPlaying(false)
          setCurrentTime(trimStart)
        } else {
          setCurrentTime(now)
        }
        drawVerticalStage()
        rafId = window.requestAnimationFrame(tick)
      }
    }
    if (isPlaying) {
      rafId = window.requestAnimationFrame(tick)
    }
    return () => {
      if (rafId) window.cancelAnimationFrame(rafId)
    }
  }, [drawVerticalStage, isPlaying, trimEnd, trimStart])

  const commitCropPatch = useCallback(
    async (patch: ClipUpdateInput) => {
      if (!selectedClip) return
      try {
        await onUpdateClip(selectedClip.id, {
          ...patch,
          sync_folder: patch.sync_folder ?? syncFolderCrop,
        })
      } catch (err) {
        setActionFeedback({
          kind: 'error',
          text: err instanceof Error ? err.message : 'Failed to save clip framing.',
        })
      }
    },
    [onUpdateClip, selectedClip, syncFolderCrop],
  )

  const handleSaveTitle = useCallback(async () => {
    if (!selectedClip) return
    const cleaned = titleDraft.trim()
    if (!cleaned || cleaned === selectedClip.title) {
      setTitleDraft(selectedClip.title)
      setTitleDirty(false)
      return
    }
    setTitleSaving(true)
    try {
      await onUpdateClip(selectedClip.id, { title: cleaned, sync_folder: false })
      setTitleDirty(false)
      setActionFeedback({ kind: 'success', text: 'Saved clip title.' })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to save clip title.',
      })
    } finally {
      setTitleSaving(false)
    }
  }, [onUpdateClip, selectedClip, titleDraft])

  const handleBoxPointerDown = (
    target: 'webcam' | 'gameplay',
    mode: 'move' | 'resize',
    e: React.PointerEvent<HTMLDivElement>,
  ) => {
    e.stopPropagation()
    e.currentTarget.setPointerCapture(e.pointerId)
    if (previewExported) setPreviewExported(false)
    dragStateRef.current = {
      target,
      mode,
      startX: e.clientX,
      startY: e.clientY,
      origin: target === 'webcam' ? { ...webcamBox } : { ...gameplayBox },
    }
  }

  const handleBoxPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStateRef.current
    const stage = stageRef.current
    if (!drag || !stage) return
    const rect = stage.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return

    const dx = (e.clientX - drag.startX) / rect.width
    const dy = (e.clientY - drag.startY) / rect.height
    const o = drag.origin

    const next: CropBox =
      drag.mode === 'move'
        ? clampBox({
            x: Math.max(0, Math.min(1 - o.w, o.x + dx)),
            y: Math.max(0, Math.min(1 - o.h, o.y + dy)),
            w: o.w,
            h: o.h,
          })
        : clampBox({
            x: o.x,
            y: o.y,
            w: Math.max(0.05, Math.min(1 - o.x, o.w + dx)),
            h: Math.max(0.05, Math.min(1 - o.y, o.h + dy)),
          })

    if (drag.target === 'webcam') setWebcamBox(next)
    else setGameplayBox(next)
  }

  const handleBoxPointerUp = async () => {
    const drag = dragStateRef.current
    if (!drag) return
    dragStateRef.current = null
    if (drag.target === 'webcam') {
      await commitCropPatch({ webcam_box: webcamBox })
    } else {
      await commitCropPatch({ gameplay_box: gameplayBox })
    }
  }

  const handleBoxKeyDown = async (
    target: 'webcam' | 'gameplay',
    e: React.KeyboardEvent<HTMLDivElement>,
  ) => {
    const step = 0.02
    const current = target === 'webcam' ? webcamBox : gameplayBox
    let next: CropBox | null = null

    if (e.key === 'ArrowLeft') {
      next = e.shiftKey
        ? clampBox({ ...current, w: current.w - step })
        : clampBox({ ...current, x: current.x - step })
    } else if (e.key === 'ArrowRight') {
      next = e.shiftKey
        ? clampBox({ ...current, w: current.w + step })
        : clampBox({ ...current, x: current.x + step })
    } else if (e.key === 'ArrowUp') {
      next = e.shiftKey
        ? clampBox({ ...current, h: current.h - step })
        : clampBox({ ...current, y: current.y - step })
    } else if (e.key === 'ArrowDown') {
      next = e.shiftKey
        ? clampBox({ ...current, h: current.h + step })
        : clampBox({ ...current, y: current.y + step })
    }

    if (!next) return
    e.preventDefault()
    if (target === 'webcam') {
      setWebcamBox(next)
      await commitCropPatch({ webcam_box: next })
    } else {
      setGameplayBox(next)
      await commitCropPatch({ gameplay_box: next })
    }
  }

  const togglePlayback = useCallback(async () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      if (video.currentTime < trimStart || video.currentTime >= trimEnd) {
        video.currentTime = trimStart
      }
      video.playbackRate = playbackRate
      try {
        await video.play()
        setIsPlaying(true)
      } catch {
        setIsPlaying(false)
      }
    } else {
      video.pause()
      setIsPlaying(false)
    }
  }, [playbackRate, trimEnd, trimStart])

  const seekTo = useCallback(
    (sec: number) => {
      const dur = Math.max(1, videoDuration || selectedClip?.duration_sec || 10)
      const clamped = Math.max(0, Math.min(dur, sec))
      setCurrentTime(clamped)
      if (videoRef.current) {
        videoRef.current.currentTime = clamped
      }
    },
    [selectedClip?.duration_sec, videoDuration],
  )

  const handleSetInAtPlayhead = useCallback(() => {
    const nextIn = Math.max(0, Math.min(currentTime, trimEnd - 0.5))
    setTrimStart(Number(nextIn.toFixed(2)))
    void commitCropPatch({ trim_start_sec: Number(nextIn.toFixed(2)), sync_folder: false })
  }, [commitCropPatch, currentTime, trimEnd])

  const handleSetOutAtPlayhead = useCallback(() => {
    const dur = Math.max(1, videoDuration || selectedClip?.duration_sec || 10)
    const nextOut = Math.min(dur, Math.max(currentTime, trimStart + 0.5))
    setTrimEnd(Number(nextOut.toFixed(2)))
    void commitCropPatch({ trim_end_sec: Number(nextOut.toFixed(2)), sync_folder: false })
  }, [commitCropPatch, currentTime, selectedClip?.duration_sec, trimStart, videoDuration])

  const handleResetTrim = () => {
    const dur = Math.max(1, videoDuration || selectedClip?.duration_sec || 10)
    setTrimStart(0)
    setTrimEnd(dur)
    seekTo(0)
    void commitCropPatch({ trim_start_sec: 0, trim_end_sec: dur, sync_folder: false })
  }

  /* Global NLE Keyboard Shortcuts (Space, [, ], J, L) when not typing in an input */
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      if (
        !el ||
        el.tagName === 'INPUT' ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'SELECT' ||
        el.isContentEditable
      ) {
        return
      }
      if (e.code === 'Space') {
        e.preventDefault()
        void togglePlayback()
      } else if (e.key === '[') {
        e.preventDefault()
        handleSetInAtPlayhead()
      } else if (e.key === ']') {
        e.preventDefault()
        handleSetOutAtPlayhead()
      } else if (e.key === 'j' || e.key === 'J') {
        e.preventDefault()
        seekTo(currentTime - 1)
      } else if (e.key === 'l' || e.key === 'L') {
        e.preventDefault()
        seekTo(currentTime + 1)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [currentTime, handleSetInAtPlayhead, handleSetOutAtPlayhead, seekTo, togglePlayback])

  const handleLayoutPreset = async (mode: LayoutMode, ratio: number) => {
    if (previewExported) setPreviewExported(false)
    setLayoutMode(mode)
    setSplitRatio(ratio)
    await commitCropPatch({ layout_mode: mode, split_ratio: ratio })
  }

  const handleDetectWebcam = async () => {
    if (!selectedClip) return
    setDetectBusy(true)
    setActionFeedback(null)
    try {
      const updated = await onDetectWebcam(selectedClip.id)
      const wb = parseCropBox(updated.webcam_box, updated.webcam_box_json)
      const gb = parseCropBox(updated.gameplay_box, updated.gameplay_box_json)
      if (wb) setWebcamBox(wb)
      if (gb) setGameplayBox(gb)
      setLayoutMode(updated.layout_mode === 'blur_no_cam' ? 'blur_no_cam' : 'split_top_cam')
      setPreviewExported(false)
      setActionFeedback({
        kind: 'success',
        text:
          updated.layout_mode === 'blur_no_cam'
            ? 'No webcam detected. Switched to Blur No-Cam framing.'
            : `Webcam detected (${Math.round((updated.webcam_confidence || 0) * 100)}% confidence).`,
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Webcam detection failed.',
      })
    } finally {
      setDetectBusy(false)
    }
  }

  const handleSavePreset = async () => {
    if (!selectedClip) return
    setPresetBusy(true)
    setActionFeedback(null)
    try {
      await onUpdateClip(selectedClip.id, {
        layout_mode: layoutMode,
        split_ratio: splitRatio,
        webcam_box: layoutMode === 'split_top_cam' ? webcamBox : null,
        gameplay_box: gameplayBox,
        sync_folder: true,
      })
      await onSavePreset(selectedClip.id)
      setActionFeedback({
        kind: 'success',
        text: `Applied framing to all pending clips in this folder and saved preset for @${selectedClip.channel_login}.`,
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to save channel preset.',
      })
    } finally {
      setPresetBusy(false)
    }
  }

  const handleDuplicate = async () => {
    if (!selectedClip) return
    setDuplicateBusy(true)
    setActionFeedback(null)
    try {
      const dup = await onDuplicateClip(selectedClip.id)
      setActionFeedback({
        kind: 'success',
        text: `Created independent duplicate "${dup.title}" — trim or crop it freely.`,
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to duplicate clip.',
      })
    } finally {
      setDuplicateBusy(false)
    }
  }

  const handleGenerateCaptions = async () => {
    if (!selectedClip) return
    setCaptionsBusy(true)
    setActionFeedback(null)
    try {
      const updated = await onGenerateCaptions(selectedClip.id, {
        streamer_only: streamerVoiceOnly,
      })
      const parsed = parseCaptions(updated)
      setCaptionsDraft(parsed)
      setCaptionsEnabled(updated.captions_enabled ?? true)
      setCaptionsDirty(false)
      setShowCaptionEditor(parsed.length > 0)
      setActionFeedback({
        kind: 'success',
        text:
          parsed.length > 0
            ? streamerVoiceOnly
              ? `Generated ${parsed.length} streamer-only caption lines (game NPCs filtered).`
              : `Generated ${parsed.length} caption lines.`
            : streamerVoiceOnly
              ? 'No streamer speech detected (only gameplay/NPC audio found).'
              : 'No spoken speech detected in this clip.',
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Caption generation failed.',
      })
    } finally {
      setCaptionsBusy(false)
    }
  }

  const handleAddCaptionLine = () => {
    const relNow = Math.max(0, Number((currentTime - trimStart).toFixed(1)))
    const maxRel = Math.max(1, Number((trimEnd - trimStart).toFixed(1)))
    const start = Math.min(maxRel - 0.5, relNow)
    const end = Math.min(maxRel, Number((start + 2.0).toFixed(1)))
    setCaptionsDraft((prev) =>
      [...prev, { start, end, text: 'New caption line', words: [] }].sort(
        (a, b) => a.start - b.start,
      ),
    )
    setCaptionsDirty(true)
    setShowCaptionEditor(true)
  }

  const handleDeleteCaptionLine = (index: number) => {
    setCaptionsDraft((prev) => prev.filter((_, idx) => idx !== index))
    setCaptionsDirty(true)
  }

  const handleSaveCaptions = async () => {
    if (!selectedClip) return
    setCaptionsSaveBusy(true)
    setActionFeedback(null)
    try {
      const sanitized = captionsDraft
        .filter((s) => s.text.trim().length > 0)
        .map((s) => ({
          ...s,
          start: Math.max(0, Number(s.start) || 0),
          end: Math.max((Number(s.start) || 0) + 0.2, Number(s.end) || (Number(s.start) || 0) + 1.5),
          text: s.text.trim(),
        }))
        .sort((a, b) => a.start - b.start)
      const updated = await onUpdateClip(selectedClip.id, {
        captions_enabled: captionsEnabled,
        caption_style: captionStyle,
        caption_position: captionPosition,
        captions: sanitized,
        sync_folder: false,
      })
      setCaptionsDraft(parseCaptions(updated))
      setCaptionsDirty(false)
      setActionFeedback({
        kind: 'success',
        text: 'Saved closed caption edits for this clip.',
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to save captions.',
      })
    } finally {
      setCaptionsSaveBusy(false)
    }
  }

  const handleCaptureThumbnail = async () => {
    if (!selectedClip) return
    setThumbBusy(true)
    setActionFeedback(null)
    try {
      const playhead = videoRef.current?.currentTime ?? currentTime
      const targetAt = Number.isFinite(playhead) ? Math.max(0, playhead) : (trimStart + trimEnd) / 2
      await onGenerateThumbnail(selectedClip.id, {
        at_sec: targetAt,
        title_overlay: thumbTitleOverlay,
      })
      setThumbCacheBust((n) => n + 1)
      setActionFeedback({
        kind: 'success',
        text: `Captured 9:16 cover thumbnail at ${formatTimecode(targetAt)}.`,
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Thumbnail capture failed.',
      })
    } finally {
      setThumbBusy(false)
    }
  }

  const handleRender = async () => {
    if (!selectedClip) return
    setRenderBusy(true)
    setActionFeedback(null)
    try {
      const cleanedTitle = titleDraft.trim() || selectedClip.title
      await onUpdateClip(selectedClip.id, {
        title: cleanedTitle,
        trim_start_sec: trimStart,
        trim_end_sec: trimEnd,
        layout_mode: layoutMode,
        split_ratio: splitRatio,
        webcam_box: layoutMode === 'split_top_cam' ? webcamBox : null,
        gameplay_box: gameplayBox,
        normalize_audio: normalizeAudio,
        captions_enabled: captionsEnabled,
        caption_style: captionStyle,
        caption_position: captionPosition,
        ...(captionsDirty ? { captions: captionsDraft } : {}),
        thumb_title_overlay: thumbTitleOverlay,
        sync_folder: syncFolderCrop,
      })
      setTitleDirty(false)
      setCaptionsDirty(false)
      await onRenderClip(selectedClip.id)
      setActionFeedback({
        kind: 'success',
        text: captionsEnabled
          ? '9:16 render queued with CC burn-in and cover thumbnail.'
          : 'Vertical 9:16 MP4 render queued.',
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Render request failed.',
      })
    } finally {
      setRenderBusy(false)
    }
  }

  const handleBatchRenderFolder = async () => {
    const pendingInFolder = folderClips.filter((c) => c.status === 'pending_review')
    if (pendingInFolder.length === 0) return
    setBatchRenderBusy(true)
    setActionFeedback(null)
    try {
      for (const item of pendingInFolder) {
        await onRenderClip(item.id)
      }
      setActionFeedback({
        kind: 'success',
        text: `Queued 9:16 MP4 rendering for ${pendingInFolder.length} clip(s) in this folder.`,
      })
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Batch render failed.',
      })
    } finally {
      setBatchRenderBusy(false)
    }
  }

  const handleDiscard = async () => {
    if (!selectedClip) return
    setDeleteBusy(true)
    setActionFeedback(null)
    try {
      await onDeleteClip(selectedClip.id)
      setConfirmDiscard(false)
    } catch (err) {
      setActionFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to discard clip.',
      })
    } finally {
      setDeleteBusy(false)
    }
  }

  const timelinePoints = useMemo(() => (selectedClip ? parseTimeline(selectedClip) : []), [selectedClip])
  const chatSamples = useMemo(() => (selectedClip ? parseChatSamples(selectedClip) : []), [selectedClip])
  const effectiveDur = Math.max(1, videoDuration || selectedClip?.duration_sec || 10)
  const timelinePolyline = useMemo(() => {
    if (timelinePoints.length === 0) return ''
    const w = 640
    const h = 64
    return timelinePoints
      .map((pt, idx) => {
        const x = timelinePoints.length > 1 ? (idx / (timelinePoints.length - 1)) * w : w / 2
        const y = h - 6 - (Math.max(0, Math.min(100, pt.fused)) / 100) * (h - 12)
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')
  }, [timelinePoints])

  const counts = useMemo(() => {
    return {
      pending_review: folderClips.filter((c) => c.status === 'pending_review').length,
      exported: folderClips.filter((c) => c.status === 'exported').length,
      all: folderClips.length,
    }
  }, [folderClips])

  const activeFolderObj = useMemo(
    () => clipFolders.find((f) => f.key === activeFolderKey) ?? null,
    [activeFolderKey, clipFolders],
  )

  return (
    <section aria-labelledby="workbench-heading" className="flex-1 flex flex-col min-w-0 bg-paper">
      {/* Studio Workbench Toolbar */}
      <div className="border-b border-rule bg-paper-2/75 px-4 py-2.5 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <Film size={16} className="text-accent shrink-0" aria-hidden="true" />
            <h2 id="workbench-heading" className="section-title !text-base">
              Clip Studio
            </h2>
          </div>

          {clipFolders.length > 0 && (
            <div className="flex items-center gap-2 pl-3 border-l border-rule">
              <FolderOpen size={14} className="text-accent shrink-0" aria-hidden="true" />
              <select
                aria-label="Active video folder"
                value={activeFolderKey}
                onChange={(e) => onSelectFolder(e.target.value)}
                className="field-select !min-h-[34px] text-xs font-medium min-w-[210px]"
              >
                {clipFolders.map((folder) => (
                  <option key={folder.key} value={folder.key}>
                    {folder.label} ({folder.clipCount} clips)
                  </option>
                ))}
                {clipFolders.length > 1 && (
                  <option value="__all__">
                    All Video Folders ({clips.filter((c) => c.status !== 'discarded').length} clips)
                  </option>
                )}
              </select>
            </div>
          )}

          {counts.pending_review > 1 && (
            <button
              type="button"
              disabled={batchRenderBusy}
              aria-busy={batchRenderBusy}
              onClick={handleBatchRenderFolder}
              className="btn btn--compact btn--accent-outline"
            >
              {batchRenderBusy ? (
                <span className="desk-spinner" aria-hidden="true" />
              ) : (
                <Sparkles size={13} aria-hidden="true" />
              )}
              <span>Render All Pending ({counts.pending_review})</span>
            </button>
          )}
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          {/* Prev / Next Clip Stepper so you can edit every clip sequentially */}
          {filteredClips.length > 1 && (
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                disabled={selectedIndex <= 0}
                onClick={() => {
                  const prevClip = filteredClips[selectedIndex - 1]
                  if (prevClip) onSelectClip(prevClip.id)
                }}
                className="btn btn--compact btn--quiet !px-2"
                aria-label="Previous clip in folder"
              >
                <ChevronLeft size={14} aria-hidden="true" />
                <span>Prev</span>
              </button>
              <span className="num text-xs text-muted px-1">
                {selectedIndex + 1} / {filteredClips.length}
              </span>
              <button
                type="button"
                disabled={selectedIndex < 0 || selectedIndex >= filteredClips.length - 1}
                onClick={() => {
                  const nextClip = filteredClips[selectedIndex + 1]
                  if (nextClip) onSelectClip(nextClip.id)
                }}
                className="btn btn--compact btn--quiet !px-2"
                aria-label="Next clip in folder"
              >
                <span>Next</span>
                <ChevronRight size={14} aria-hidden="true" />
              </button>
            </div>
          )}

          <div role="tablist" aria-label="Clip queue filter" className="seg-group grid-cols-3">
            {(
              [
                { id: 'all', label: `All (${counts.all})` },
                { id: 'pending_review', label: `Pending (${counts.pending_review})` },
                { id: 'exported', label: `Exported (${counts.exported})` },
              ] as const
            ).map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={filter === tab.id}
                onClick={() => setFilter(tab.id)}
                className="seg-cell"
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Split Body: Left Vertical Clip Bin + Right Dual-Monitor NLE Stage */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-[276px_minmax(0,1fr)] min-h-0">
        {/* Left Vertical Clip Stack */}
        <aside
          aria-label="Active video folder clips"
          className="border-b lg:border-b-0 lg:border-r border-rule bg-paper p-3 flex flex-col gap-2.5"
        >
          <div className="flex items-center justify-between gap-2 pb-2 border-b border-rule">
            <span className="eyebrow-label !text-ink truncate">
              {activeFolderObj ? activeFolderObj.label : 'All Clips'}
            </span>
            <span className="studio-pill studio-pill--accent !py-0 !px-2 num">
              {filteredClips.length} clip{filteredClips.length === 1 ? '' : 's'}
            </span>
          </div>

          {loading && clips.length === 0 ? (
            <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading clips">
              {[1, 2, 3].map((n) => (
                <div key={n} className="studio-card h-16 p-2.5">
                  <div className="h-3 rounded bg-paper-3 w-1/2 mb-2" />
                  <div className="h-2 rounded bg-paper-3 w-3/4" />
                </div>
              ))}
            </div>
          ) : error && clips.length === 0 ? (
            <div className="studio-card p-3 border-error flex flex-col gap-2">
              <p className="text-xs text-error">{error}</p>
              <button type="button" onClick={onRetryClips} className="btn btn--quiet btn--compact">
                <RefreshCw size={13} aria-hidden="true" />
                <span>Retry</span>
              </button>
            </div>
          ) : filteredClips.length === 0 ? (
            <div className="studio-card p-3.5 flex flex-col gap-2.5">
              <p className="text-xs font-semibold text-ink">
                {filter === 'pending_review'
                  ? 'All clips in this folder are exported!'
                  : filter === 'exported'
                    ? 'No exported clips in this folder yet.'
                    : 'No clips in this folder yet.'}
              </p>
              {filter !== 'all' && folderClips.length > 0 ? (
                <button
                  type="button"
                  onClick={() => setFilter('all')}
                  className="btn btn--compact btn--accent-outline w-full"
                >
                  Show All {folderClips.length} Clips to Edit
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onOpenVodModal}
                  className="btn btn--compact btn--primary w-full"
                >
                  <Video size={14} aria-hidden="true" />
                  <span>Analyze Video</span>
                </button>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-2 max-h-[820px] overflow-y-auto pr-0.5">
              {filteredClips.map((clip, idx) => {
                const active = selectedClip?.id === clip.id
                const clipDur = Math.max(
                  0.5,
                  (clip.trim_end_sec || clip.duration_sec) - (clip.trim_start_sec || 0),
                )
                return (
                  <button
                    key={clip.id}
                    type="button"
                    data-selected={active ? 'true' : 'false'}
                    onClick={() => onSelectClip(clip.id)}
                    className="interactive-row w-full p-2.5 flex items-center gap-2.5 text-left"
                  >
                    <div className="relative w-16 h-11 rounded-md bg-inset border border-rule shrink-0 overflow-hidden flex items-center justify-center">
                      {clip.thumbnail_path ? (
                        <img
                          src={thumbImageUrl(clip.id)}
                          alt=""
                          width={160}
                          height={90}
                          onError={(e) => {
                            e.currentTarget.hidden = true
                          }}
                          className="w-full h-full object-cover"
                        />
                      ) : (
                        <Film size={15} className="text-muted" aria-hidden="true" />
                      )}
                      <span className="absolute bottom-0.5 right-0.5 px-1 rounded bg-black/75 text-[10px] num text-white">
                        #{idx + 1}
                      </span>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-1.5">
                        <span className="text-xs font-semibold text-ink truncate">
                          {clip.title}
                        </span>
                        <span className="num text-xs font-semibold text-accent shrink-0">
                          {clip.hype_score.toFixed(0)}
                        </span>
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-muted mt-1">
                        <span
                          className={
                            clip.status === 'exported'
                              ? 'text-emerald-400 font-medium'
                              : clip.status === 'rendering'
                                ? 'text-accent font-medium'
                                : 'text-muted'
                          }
                        >
                          {formatStatusLabel(clip.status)}
                        </span>
                        <span className="num shrink-0 ml-1">{formatTimecode(clipDur)}</span>
                      </div>
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </aside>

        {/* Right Dual-Stage NLE Editor */}
        {selectedClip ? (
          <div className="p-4 lg:p-5 flex flex-col gap-4 min-w-0">
            {/* Editable Clip Title & Per-Clip Framing Mode Bar */}
            <div className="studio-card p-3.5 flex items-center justify-between gap-4 flex-wrap">
              <div className="flex-1 min-w-[240px] flex flex-col gap-1.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <input
                    type="text"
                    aria-label="Editable clip title"
                    value={titleDraft}
                    onChange={(e) => {
                      setTitleDraft(e.target.value)
                      setTitleDirty(true)
                    }}
                    onBlur={() => void handleSaveTitle()}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.currentTarget.blur()
                      }
                    }}
                    className="field-input !min-h-[36px] !text-base font-semibold max-w-xl"
                  />
                  {titleDirty && (
                    <button
                      type="button"
                      disabled={titleSaving}
                      onClick={() => void handleSaveTitle()}
                      className="btn btn--compact btn--accent-outline"
                    >
                      <Check size={13} aria-hidden="true" />
                      <span>{titleSaving ? 'Saving' : 'Save Title'}</span>
                    </button>
                  )}
                  <span className="studio-pill">{formatSourceBadge(selectedClip.source_type)}</span>
                  <span
                    className={`studio-pill ${
                      selectedClip.status === 'exported'
                        ? 'studio-pill--success'
                        : selectedClip.status === 'failed'
                          ? 'studio-pill--danger'
                          : selectedClip.status === 'rendering'
                            ? 'studio-pill--accent'
                            : ''
                    }`}
                  >
                    {formatStatusLabel(selectedClip.status)}
                  </span>
                </div>

                <p className="text-xs text-muted">
                  @{selectedClip.channel_login} · {selectedClip.trigger_reason || 'manual cut'} · score{' '}
                  <strong className="num text-ink-2">{selectedClip.hype_score.toFixed(1)}</strong> ·
                  Shortcuts: <code className="num text-ink-2">Space</code> Play,{' '}
                  <code className="num text-ink-2">[</code> Set In,{' '}
                  <code className="num text-ink-2">]</code> Set Out
                </p>
              </div>

              <div className="flex items-center gap-2 flex-wrap">
                {/* Per-Clip Crop Lock vs Sync Folder Toggle */}
                <div
                  className="seg-group grid-cols-2"
                  role="group"
                  aria-label="Crop edit scope"
                  title="Choose whether crop changes apply only to this clip or sync across the folder"
                >
                  <button
                    type="button"
                    aria-pressed={!syncFolderCrop}
                    onClick={() => setSyncFolderCrop(false)}
                    className="seg-cell flex items-center gap-1.5"
                  >
                    <Lock size={12} aria-hidden="true" />
                    <span>This Clip Only</span>
                  </button>
                  <button
                    type="button"
                    aria-pressed={syncFolderCrop}
                    onClick={() => setSyncFolderCrop(true)}
                    className="seg-cell flex items-center gap-1.5"
                  >
                    <Layers size={12} aria-hidden="true" />
                    <span>Sync Folder</span>
                  </button>
                </div>

                <button
                  type="button"
                  disabled={duplicateBusy}
                  aria-busy={duplicateBusy}
                  onClick={handleDuplicate}
                  title="Clone this clip to create another cut from the same video"
                  className="btn btn--compact btn--quiet"
                >
                  {duplicateBusy ? (
                    <span className="desk-spinner" aria-hidden="true" />
                  ) : (
                    <Copy size={13} aria-hidden="true" />
                  )}
                  <span>Duplicate Cut</span>
                </button>

                <button
                  type="button"
                  disabled={detectBusy}
                  aria-busy={detectBusy}
                  onClick={handleDetectWebcam}
                  className="btn btn--compact btn--quiet"
                >
                  {detectBusy ? (
                    <span className="desk-spinner" aria-hidden="true" />
                  ) : (
                    <ScanFace size={14} aria-hidden="true" />
                  )}
                  <span>{detectBusy ? 'Detecting' : 'Auto-Cam'}</span>
                </button>

                <button
                  type="button"
                  disabled={presetBusy}
                  aria-busy={presetBusy}
                  onClick={handleSavePreset}
                  className="btn btn--compact btn--quiet"
                >
                  {presetBusy ? (
                    <span className="desk-spinner" aria-hidden="true" />
                  ) : (
                    <BookmarkPlus size={14} aria-hidden="true" />
                  )}
                  <span>{presetBusy ? 'Applying' : 'Apply Crop to Folder'}</span>
                </button>
              </div>
            </div>

            {/* Dual-Monitor Stage: 16:9 Source Crop Monitor + Synced 9:16 Vertical Inspector */}
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_336px] gap-4 items-start">
              {/* Left Monitor Column: 16:9 Source Video + NLE Timeline Scrubber */}
              <div className="flex flex-col gap-3 min-w-0">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-2">
                    <span className="eyebrow-label !text-ink-2">
                      16:9 Source Canvas
                    </span>
                    <span className="text-xs text-muted">
                      (drag boxes or use arrow keys · editing{' '}
                      <strong className="text-accent">
                        {syncFolderCrop ? 'all pending clips in folder' : 'this clip individually'}
                      </strong>
                      )
                    </span>
                  </div>

                  {selectedClip.status === 'exported' && selectedClip.export_video_path && (
                    <button
                      type="button"
                      aria-pressed={previewExported}
                      onClick={() => {
                        setPreviewExported((v) => !v)
                        setIsPlaying(false)
                        setVideoError(null)
                      }}
                      className="btn btn--compact btn--accent-outline"
                    >
                      {previewExported ? 'Edit 16:9 Crop & Trim' : 'Preview Rendered 9:16'}
                    </button>
                  )}
                </div>

                <div
                  ref={stageRef}
                  className="relative w-full aspect-video rounded-xl bg-inset border border-rule overflow-hidden shadow-lg"
                >
                  <video
                    ref={videoRef}
                    key={`${selectedClip.id}-${previewExported ? 'exp' : 'raw'}`}
                    src={previewExported ? exportVideoUrl(selectedClip.id) : rawVideoUrl(selectedClip.id)}
                    poster={selectedClip.thumbnail_path ? thumbImageUrl(selectedClip.id) : undefined}
                    preload="metadata"
                    playsInline
                    muted={isMuted}
                    onLoadedMetadata={(e) => {
                      const el = e.currentTarget
                      const dur = Number.isFinite(el.duration) && el.duration > 0 ? el.duration : effectiveDur
                      setVideoDuration(dur)
                      setTrimEnd((prev) => (prev > dur || prev <= trimStart ? dur : prev))
                      el.currentTime = trimStart
                      el.playbackRate = playbackRate
                      drawVerticalStage()
                    }}
                    onLoadedData={drawVerticalStage}
                    onSeeked={() => {
                      if (videoRef.current) setCurrentTime(videoRef.current.currentTime)
                      drawVerticalStage()
                    }}
                    onEnded={() => setIsPlaying(false)}
                    onError={() => {
                      setVideoError('Media stream failed to load.')
                      drawVerticalStage()
                    }}
                    className="w-full h-full object-contain bg-inset"
                  />

                  {!previewExported && (
                    <div className="absolute inset-0">
                      {/* Gameplay Crop Overlay (Cyan) */}
                      <div
                        role="region"
                        tabIndex={0}
                        aria-label="Gameplay crop region. Use arrow keys to move, Shift plus arrow keys to resize."
                        onPointerDown={(e) => handleBoxPointerDown('gameplay', 'move', e)}
                        onPointerMove={handleBoxPointerMove}
                        onPointerUp={handleBoxPointerUp}
                        onKeyDown={(e) => handleBoxKeyDown('gameplay', e)}
                        className="crop-box-game absolute cursor-move focus-visible:outline-2"
                        style={{
                          left: `${gameplayBox.x * 100}%`,
                          top: `${gameplayBox.y * 100}%`,
                          width: `${gameplayBox.w * 100}%`,
                          height: `${gameplayBox.h * 100}%`,
                        }}
                      >
                        <span className="inline-block px-2 py-0.5 rounded-br bg-inset/90 text-signal-game text-[11px] font-semibold pointer-events-none">
                          Gameplay
                        </span>
                        <div
                          aria-hidden="true"
                          onPointerDown={(e) => handleBoxPointerDown('gameplay', 'resize', e)}
                          onPointerMove={handleBoxPointerMove}
                          onPointerUp={handleBoxPointerUp}
                          className="crop-handle crop-handle--game"
                        />
                      </div>

                      {/* Webcam Crop Overlay (Electric Violet) */}
                      {layoutMode === 'split_top_cam' && (
                        <div
                          role="region"
                          tabIndex={0}
                          aria-label="Webcam crop region. Use arrow keys to move, Shift plus arrow keys to resize."
                          onPointerDown={(e) => handleBoxPointerDown('webcam', 'move', e)}
                          onPointerMove={handleBoxPointerMove}
                          onPointerUp={handleBoxPointerUp}
                          onKeyDown={(e) => handleBoxKeyDown('webcam', e)}
                          className="crop-box-webcam absolute cursor-move focus-visible:outline-2"
                          style={{
                            left: `${webcamBox.x * 100}%`,
                            top: `${webcamBox.y * 100}%`,
                            width: `${webcamBox.w * 100}%`,
                            height: `${webcamBox.h * 100}%`,
                          }}
                        >
                          <span className="inline-block px-2 py-0.5 rounded-br bg-accent text-white text-[11px] font-semibold pointer-events-none">
                            Webcam
                          </span>
                          <div
                            aria-hidden="true"
                            onPointerDown={(e) => handleBoxPointerDown('webcam', 'resize', e)}
                            onPointerMove={handleBoxPointerMove}
                            onPointerUp={handleBoxPointerUp}
                            className="crop-handle crop-handle--webcam"
                          />
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* Precision NLE Transport + Dual Trim Scrubber */}
                <div className="studio-card p-3.5 flex flex-col gap-3">
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-2 flex-wrap">
                      <button
                        type="button"
                        onClick={() => void togglePlayback()}
                        className="btn btn--compact btn--primary !min-h-[34px] !px-3.5"
                      >
                        {isPlaying ? <Pause size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                        <span>{isPlaying ? 'Pause' : 'Play'}</span>
                      </button>

                      <button
                        type="button"
                        onClick={() => seekTo(currentTime - 1)}
                        className="btn btn--compact btn--quiet !px-2 num"
                        title="Step back 1 second (J)"
                      >
                        -1s
                      </button>
                      <button
                        type="button"
                        onClick={() => seekTo(currentTime + 1)}
                        className="btn btn--compact btn--quiet !px-2 num"
                        title="Step forward 1 second (L)"
                      >
                        +1s
                      </button>

                      <button
                        type="button"
                        onClick={() => {
                          setIsMuted((m) => {
                            const next = !m
                            if (videoRef.current) videoRef.current.muted = next
                            return next
                          })
                        }}
                        aria-label={isMuted ? 'Unmute audio' : 'Mute audio'}
                        className="btn btn--compact btn--quiet btn--icon"
                      >
                        {isMuted ? <VolumeX size={14} aria-hidden="true" /> : <Volume2 size={14} aria-hidden="true" />}
                      </button>

                      <div className="seg-group grid-cols-4" role="group" aria-label="Playback speed">
                        {PLAYBACK_RATES.map((rate) => (
                          <button
                            key={rate}
                            type="button"
                            aria-pressed={playbackRate === rate}
                            onClick={() => {
                              setPlaybackRate(rate)
                              if (videoRef.current) videoRef.current.playbackRate = rate
                            }}
                            className="seg-cell !min-h-[26px] !px-2 num"
                          >
                            {rate}x
                          </button>
                        ))}
                      </div>

                      <span className="num text-xs text-ink-2 pl-1">
                        {formatTimecode(currentTime)} / {formatTimecode(effectiveDur)}
                      </span>
                    </div>

                    {/* In / Out Razor Trim Controls */}
                    <div className="flex items-center gap-2 flex-wrap text-xs">
                      <button
                        type="button"
                        onClick={handleSetInAtPlayhead}
                        className="btn btn--compact btn--quiet"
                        title="Set In point to current playhead ([)"
                      >
                        <Scissors size={12} aria-hidden="true" />
                        <span>[ In:</span>
                        <strong className="num text-ink">{formatTimecode(trimStart)}</strong>
                      </button>

                      <button
                        type="button"
                        onClick={handleSetOutAtPlayhead}
                        className="btn btn--compact btn--quiet"
                        title="Set Out point to current playhead (])"
                      >
                        <span>Out ]:</span>
                        <strong className="num text-ink">{formatTimecode(trimEnd)}</strong>
                      </button>

                      <span className="studio-pill studio-pill--accent num">
                        Cut {formatTimecode(trimEnd - trimStart)}
                      </span>

                      {(trimStart > 0.1 || trimEnd < effectiveDur - 0.1) && (
                        <button
                          type="button"
                          onClick={handleResetTrim}
                          className="btn btn--compact btn--ghost !px-2"
                          title="Reset trim to full clip duration"
                        >
                          <RotateCcw size={12} aria-hidden="true" />
                          <span>Reset</span>
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Waveform / Hype Timeline + Dual Trim Handles */}
                  <div className="relative h-14 rounded-lg border border-rule bg-inset overflow-hidden">
                    <svg
                      viewBox="0 0 640 64"
                      role="img"
                      aria-label="Clip hype timeline and trim range"
                      className="w-full h-full cursor-pointer"
                      preserveAspectRatio="none"
                      onClick={(e) => {
                        const rect = e.currentTarget.getBoundingClientRect()
                        const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
                        seekTo(frac * effectiveDur)
                      }}
                    >
                      <rect
                        x={(trimStart / effectiveDur) * 640}
                        y={0}
                        width={Math.max(4, ((trimEnd - trimStart) / effectiveDur) * 640)}
                        height={64}
                        fill="rgba(124, 92, 252, 0.18)"
                      />
                      {timelinePolyline && (
                        <polyline
                          fill="none"
                          stroke="var(--color-accent)"
                          strokeWidth={2}
                          points={timelinePolyline}
                        />
                      )}
                      <line
                        x1={(currentTime / effectiveDur) * 640}
                        y1={0}
                        x2={(currentTime / effectiveDur) * 640}
                        y2={64}
                        stroke="#ffffff"
                        strokeWidth={2}
                      />
                    </svg>

                    <input
                      type="range"
                      aria-label="Trim start time in seconds"
                      min={0}
                      max={effectiveDur}
                      step={0.1}
                      value={trimStart}
                      onChange={(e) => {
                        const val = Math.min(Number(e.target.value), trimEnd - 0.5)
                        setTrimStart(Math.max(0, val))
                        seekTo(Math.max(0, val))
                      }}
                      onPointerUp={() => commitCropPatch({ trim_start_sec: trimStart, sync_folder: false })}
                      className="trim-range absolute inset-0"
                    />
                    <input
                      type="range"
                      aria-label="Trim end time in seconds"
                      min={0}
                      max={effectiveDur}
                      step={0.1}
                      value={trimEnd}
                      onChange={(e) => {
                        const val = Math.max(Number(e.target.value), trimStart + 0.5)
                        setTrimEnd(Math.min(effectiveDur, val))
                        seekTo(Math.min(effectiveDur, val))
                      }}
                      onPointerUp={() => commitCropPatch({ trim_end_sec: trimEnd, sync_folder: false })}
                      className="trim-range absolute inset-0"
                    />
                  </div>
                </div>
              </div>

              {/* Right Inspector Column: Live 9:16 Vertical Stage + CC Editor + Export */}
              <div className="flex flex-col gap-3">
                <div className="flex items-center justify-between">
                  <span className="eyebrow-label !text-ink-2">9:16 Vertical Master</span>
                  <span className="studio-pill num">1080×1920 MP4</span>
                </div>

                <div className="studio-card p-3.5 flex flex-col items-center gap-3.5">
                  <canvas
                    ref={canvasRef}
                    width={270}
                    height={480}
                    aria-label="Synced 9:16 vertical stage preview"
                    className="w-full max-w-[270px] aspect-[9/16] rounded-xl border border-rule bg-inset shadow-lg"
                  />

                  {/* Stage Layout Split Switcher */}
                  <div className="w-full">
                    <span className="block eyebrow-label mb-1.5">Stage Layout</span>
                    <div className="seg-group grid-cols-4 w-full" role="group" aria-label="Vertical split preset">
                      <button
                        type="button"
                        aria-pressed={layoutMode === 'split_top_cam' && Math.abs(splitRatio - 0.35) < 0.02}
                        onClick={() => handleLayoutPreset('split_top_cam', 0.35)}
                        className="seg-cell"
                      >
                        35/65
                      </button>
                      <button
                        type="button"
                        aria-pressed={layoutMode === 'split_top_cam' && Math.abs(splitRatio - 0.4) < 0.02}
                        onClick={() => handleLayoutPreset('split_top_cam', 0.4)}
                        className="seg-cell"
                      >
                        40/60
                      </button>
                      <button
                        type="button"
                        aria-pressed={layoutMode === 'split_top_cam' && Math.abs(splitRatio - 0.5) < 0.02}
                        onClick={() => handleLayoutPreset('split_top_cam', 0.5)}
                        className="seg-cell"
                      >
                        50/50
                      </button>
                      <button
                        type="button"
                        aria-pressed={layoutMode === 'blur_no_cam'}
                        onClick={() => handleLayoutPreset('blur_no_cam', splitRatio)}
                        className="seg-cell"
                      >
                        Blur
                      </button>
                    </div>
                  </div>

                  {/* Audio Normalization Toggle */}
                  <label className="w-full flex items-center justify-between gap-2 py-2 border-t border-b border-rule cursor-pointer">
                    <span className="flex items-center gap-2 text-xs font-medium text-ink-2">
                      <Volume2 size={14} className="text-accent" aria-hidden="true" />
                      <span>Master Audio (-14 LUFS)</span>
                    </span>
                    <input
                      type="checkbox"
                      checked={normalizeAudio}
                      onChange={(e) => {
                        const val = e.target.checked
                        setNormalizeAudio(val)
                        void commitCropPatch({ normalize_audio: val, sync_folder: false })
                      }}
                      className="w-4 h-4 accent-accent rounded"
                    />
                  </label>

                  {/* Closed Captions (CC) Full Editor */}
                  <div className="w-full flex flex-col gap-2.5 pb-2.5 border-b border-rule">
                    <label className="w-full flex items-center justify-between gap-2 cursor-pointer">
                      <span className="flex items-center gap-2 text-xs font-medium text-ink-2">
                        <Captions size={14} className="text-accent" aria-hidden="true" />
                        <span>Burn Closed Captions (CC)</span>
                      </span>
                      <input
                        type="checkbox"
                        checked={captionsEnabled}
                        onChange={(e) => {
                          const val = e.target.checked
                          setCaptionsEnabled(val)
                          void commitCropPatch({ captions_enabled: val, sync_folder: false })
                        }}
                        className="w-4 h-4 accent-accent rounded"
                      />
                    </label>

                    {captionsEnabled && (
                      <>
                        <div className="seg-group grid-cols-3 w-full" role="group" aria-label="Caption style">
                          <button
                            type="button"
                            aria-pressed={captionStyle === 'bold_pop'}
                            onClick={() => {
                              setCaptionStyle('bold_pop')
                              void commitCropPatch({ caption_style: 'bold_pop', sync_folder: false })
                            }}
                            className="seg-cell"
                          >
                            Bold Pop
                          </button>
                          <button
                            type="button"
                            aria-pressed={captionStyle === 'clean_box'}
                            onClick={() => {
                              setCaptionStyle('clean_box')
                              void commitCropPatch({ caption_style: 'clean_box', sync_folder: false })
                            }}
                            className="seg-cell"
                          >
                            Clean Box
                          </button>
                          <button
                            type="button"
                            aria-pressed={captionStyle === 'classic_cc'}
                            onClick={() => {
                              setCaptionStyle('classic_cc')
                              void commitCropPatch({ caption_style: 'classic_cc', sync_folder: false })
                            }}
                            className="seg-cell"
                          >
                            Classic CC
                          </button>
                        </div>

                        {layoutMode === 'split_top_cam' && (
                          <div className="seg-group grid-cols-2 w-full" role="group" aria-label="Caption placement">
                            <button
                              type="button"
                              aria-pressed={captionPosition === 'seam'}
                              onClick={() => {
                                setCaptionPosition('seam')
                                void commitCropPatch({ caption_position: 'seam', sync_folder: false })
                              }}
                              className="seg-cell"
                            >
                              Split Seam
                            </button>
                            <button
                              type="button"
                              aria-pressed={captionPosition === 'lower_safe'}
                              onClick={() => {
                                setCaptionPosition('lower_safe')
                                void commitCropPatch({ caption_position: 'lower_safe', sync_folder: false })
                              }}
                              className="seg-cell"
                            >
                              Lower Safe
                            </button>
                          </div>
                        )}

                        <label className="w-full flex items-center justify-between gap-2 py-0.5 cursor-pointer">
                          <span className="text-xs text-ink-2">
                            Streamer Voice Only (filter game NPCs &amp; subtitles)
                          </span>
                          <input
                            type="checkbox"
                            checked={streamerVoiceOnly}
                            onChange={(e) => setStreamerVoiceOnly(e.target.checked)}
                            className="w-4 h-4 accent-accent rounded"
                          />
                        </label>

                        <div className="flex items-center gap-1.5">
                          <button
                            type="button"
                            disabled={captionsBusy}
                            aria-busy={captionsBusy}
                            onClick={handleGenerateCaptions}
                            className="btn btn--compact btn--accent-outline flex-1"
                          >
                            {captionsBusy ? (
                              <span className="desk-spinner" aria-hidden="true" />
                            ) : (
                              <Captions size={13} aria-hidden="true" />
                            )}
                            <span>
                              {captionsBusy
                                ? 'Transcribing...'
                                : captionsDraft.length > 0
                                  ? `Re-generate CC (${captionsDraft.length})`
                                  : 'Generate CC'}
                            </span>
                          </button>

                          <button
                            type="button"
                            onClick={handleAddCaptionLine}
                            title="Add custom caption line at current playhead"
                            className="btn btn--compact btn--quiet"
                          >
                            <Plus size={13} aria-hidden="true" />
                            <span>Line</span>
                          </button>

                          {captionsDraft.length > 0 && (
                            <button
                              type="button"
                              aria-expanded={showCaptionEditor}
                              onClick={() => setShowCaptionEditor((v) => !v)}
                              className="btn btn--compact btn--quiet"
                            >
                              {showCaptionEditor ? (
                                <ChevronUp size={13} aria-hidden="true" />
                              ) : (
                                <ChevronDown size={13} aria-hidden="true" />
                              )}
                              <span>Edit</span>
                            </button>
                          )}
                        </div>

                        {showCaptionEditor && (
                          <div className="flex flex-col gap-2 pt-2 border-t border-rule">
                            {captionsDraft.length === 0 ? (
                              <p className="text-xs text-muted">
                                No caption lines yet. Click + Line or Generate CC.
                              </p>
                            ) : (
                              <div className="max-h-48 overflow-y-auto flex flex-col gap-1.5 pr-1">
                                {captionsDraft.map((seg, idx) => (
                                  <div
                                    key={`${idx}-${seg.start}`}
                                    className="p-2 rounded-lg bg-inset border border-rule flex flex-col gap-1.5"
                                  >
                                    <div className="flex items-center justify-between gap-1.5">
                                      <div className="flex items-center gap-1 text-[11px] text-muted">
                                        <span>In</span>
                                        <input
                                          type="number"
                                          step="0.1"
                                          min="0"
                                          aria-label={`Caption ${idx + 1} start seconds`}
                                          value={seg.start}
                                          onChange={(e) => {
                                            const val = Math.max(0, Number(e.target.value) || 0)
                                            setCaptionsDraft((prev) =>
                                              prev.map((item, pos) =>
                                                pos === idx ? { ...item, start: val } : item,
                                              ),
                                            )
                                            setCaptionsDirty(true)
                                          }}
                                          className="field-input num !min-h-[26px] !px-1.5 !text-[11px] w-14"
                                        />
                                        <span>Out</span>
                                        <input
                                          type="number"
                                          step="0.1"
                                          min="0.1"
                                          aria-label={`Caption ${idx + 1} end seconds`}
                                          value={seg.end}
                                          onChange={(e) => {
                                            const val = Math.max(0.1, Number(e.target.value) || 0.5)
                                            setCaptionsDraft((prev) =>
                                              prev.map((item, pos) =>
                                                pos === idx ? { ...item, end: val } : item,
                                              ),
                                            )
                                            setCaptionsDirty(true)
                                          }}
                                          className="field-input num !min-h-[26px] !px-1.5 !text-[11px] w-14"
                                        />
                                      </div>

                                      <button
                                        type="button"
                                        onClick={() => handleDeleteCaptionLine(idx)}
                                        aria-label={`Delete caption line ${idx + 1}`}
                                        className="btn btn--compact btn--ghost !min-h-[24px] !w-6 !p-0"
                                      >
                                        <X size={12} aria-hidden="true" />
                                      </button>
                                    </div>

                                    <input
                                      type="text"
                                      aria-label={`Caption line ${idx + 1} text`}
                                      value={seg.text}
                                      onChange={(e) => {
                                        const nextText = e.target.value
                                        setCaptionsDraft((prev) =>
                                          prev.map((item, pos) =>
                                            pos === idx ? { ...item, text: nextText, words: [] } : item,
                                          ),
                                        )
                                        setCaptionsDirty(true)
                                      }}
                                      className="field-input !min-h-[30px] text-xs"
                                    />
                                  </div>
                                ))}
                              </div>
                            )}

                            {captionsDirty && (
                              <button
                                type="button"
                                disabled={captionsSaveBusy}
                                aria-busy={captionsSaveBusy}
                                onClick={handleSaveCaptions}
                                className="btn btn--compact btn--primary w-full"
                              >
                                {captionsSaveBusy && <span className="desk-spinner" aria-hidden="true" />}
                                <span>{captionsSaveBusy ? 'Saving CC...' : 'Save CC Timeline'}</span>
                              </button>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>

                  {/* Primary Render Action + Export Downloads */}
                  <button
                    type="button"
                    disabled={renderBusy || selectedClip.status === 'rendering'}
                    aria-busy={renderBusy || selectedClip.status === 'rendering'}
                    onClick={handleRender}
                    className="btn btn--primary w-full"
                  >
                    {(renderBusy || selectedClip.status === 'rendering') && (
                      <span className="desk-spinner" aria-hidden="true" />
                    )}
                    <span>
                      {selectedClip.status === 'rendering' || renderBusy
                        ? 'Rendering 9:16 MP4...'
                        : selectedClip.status === 'exported'
                          ? 'Re-Render 9:16 MP4'
                          : 'Render 9:16 MP4'}
                    </span>
                  </button>

                  {selectedClip.status === 'exported' && selectedClip.export_video_path && (
                    <div className="w-full flex flex-col gap-2">
                      <a
                        href={exportVideoUrl(selectedClip.id)}
                        download={`${selectedClip.channel_login}_${selectedClip.id.slice(0, 8)}.mp4`}
                        className="btn btn--accent-outline w-full"
                      >
                        <Download size={15} aria-hidden="true" />
                        <span>
                          Download MP4{selectedClip.encoder_used ? ` (${selectedClip.encoder_used})` : ''}
                        </span>
                      </a>
                      {selectedClip.export_srt_path && (
                        <a
                          href={exportSrtUrl(selectedClip.id)}
                          download={`${selectedClip.channel_login}_${selectedClip.id.slice(0, 8)}.srt`}
                          className="btn btn--compact btn--quiet w-full"
                        >
                          <Captions size={14} aria-hidden="true" />
                          <span>Download Subtitles (.srt)</span>
                        </a>
                      )}
                    </div>
                  )}

                  {/* 9:16 Cover Thumbnail Capture & Download */}
                  <div className="w-full flex flex-col gap-2 pt-2 border-t border-rule">
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex items-center gap-2 text-xs font-medium text-ink-2">
                        <Image size={14} className="text-accent" aria-hidden="true" />
                        <span>9:16 Cover Frame</span>
                      </span>
                      {selectedClip.thumb_time_sec != null && (
                        <span className="num text-xs text-muted">
                          @ {formatTimecode(selectedClip.thumb_time_sec)}
                        </span>
                      )}
                    </div>

                    <label className="w-full flex items-center justify-between gap-2 py-0.5 cursor-pointer">
                      <span className="text-xs text-ink-2">Stamp clip title on cover</span>
                      <input
                        type="checkbox"
                        checked={thumbTitleOverlay}
                        onChange={(e) => {
                          const val = e.target.checked
                          setThumbTitleOverlay(val)
                          void commitCropPatch({ thumb_title_overlay: val, sync_folder: false })
                        }}
                        className="w-4 h-4 accent-accent rounded"
                      />
                    </label>

                    <button
                      type="button"
                      disabled={thumbBusy}
                      aria-busy={thumbBusy}
                      onClick={handleCaptureThumbnail}
                      className="btn btn--compact btn--quiet w-full whitespace-nowrap"
                    >
                      {thumbBusy ? (
                        <span className="desk-spinner" aria-hidden="true" />
                      ) : (
                        <Image size={14} aria-hidden="true" />
                      )}
                      <span>{thumbBusy ? 'Capturing cover...' : 'Capture Cover Frame'}</span>
                    </button>

                    {selectedClip.export_thumb_path && (
                      <div className="flex items-center gap-3 pt-1">
                        <img
                          src={exportThumbUrl(selectedClip.id, thumbCacheBust)}
                          alt={`9:16 cover thumbnail for ${selectedClip.title}`}
                          className="w-14 aspect-[9/16] rounded-md object-cover border border-rule bg-inset shrink-0"
                        />
                        <a
                          href={exportThumbUrl(selectedClip.id, thumbCacheBust)}
                          download={`${selectedClip.channel_login}_${selectedClip.id.slice(0, 8)}_9x16.jpg`}
                          className="btn btn--compact btn--accent-outline flex-1 whitespace-nowrap"
                        >
                          <Download size={13} aria-hidden="true" />
                          <span>Cover (.jpg)</span>
                        </a>
                      </div>
                    )}
                  </div>

                  {selectedClip.error_message && (
                    <p className="text-xs text-error w-full" role="alert">
                      {selectedClip.error_message}
                    </p>
                  )}

                  {actionFeedback && (
                    <p
                      role="status"
                      aria-live="polite"
                      className={`text-xs w-full ${
                        actionFeedback.kind === 'error' ? 'text-error' : 'text-success'
                      }`}
                    >
                      {actionFeedback.text}
                    </p>
                  )}

                  <div className="w-full pt-2 border-t border-rule flex items-center justify-between gap-2">
                    {confirmDiscard ? (
                      <>
                        <button
                          type="button"
                          disabled={deleteBusy}
                          aria-busy={deleteBusy}
                          onClick={handleDiscard}
                          className="btn btn--compact btn--danger flex-1"
                        >
                          Confirm Delete
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmDiscard(false)}
                          className="btn btn--compact btn--quiet flex-1"
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirmDiscard(true)}
                        className="btn btn--compact btn--ghost w-full"
                      >
                        <Trash2 size={13} aria-hidden="true" />
                        <span>Delete Clip</span>
                      </button>
                    )}
                  </div>
                </div>

                {chatSamples.length > 0 && (
                  <div className="studio-card p-3">
                    <p className="eyebrow-label mb-2">Trigger Chat Context</p>
                    <ul className="list-none m-0 p-0 flex flex-col gap-1 max-h-28 overflow-y-auto">
                      {chatSamples.slice(0, 6).map((s, idx) => (
                        <li key={`${s.author}-${idx}`} className="text-xs leading-snug">
                          <span className="text-accent font-semibold mr-1.5">{s.author}</span>
                          <span className="text-ink-2">{s.text}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex-1 p-8 flex items-center justify-center">
            <div className="studio-card max-w-lg w-full p-8 flex flex-col gap-4">
              <h3 className="section-title">Studio Ready for Clips</h3>
              <p className="text-sm text-ink-2">
                Start live monitoring on a channel in the left sidebar, capture an instant buffer with Clip Now, or analyze any YouTube URL / Twitch VOD / local video file to extract viral moments automatically.
              </p>
              <div className="flex items-center gap-3 flex-wrap">
                <button type="button" onClick={onOpenVodModal} className="btn btn--primary">
                  <Video size={16} aria-hidden="true" />
                  <span>New Video / VOD</span>
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
