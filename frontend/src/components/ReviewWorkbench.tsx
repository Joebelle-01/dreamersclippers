import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BookmarkPlus,
  Captions,
  ChevronDown,
  ChevronUp,
  Download,
  Film,
  Image,
  Pause,
  Play,
  RefreshCw,
  ScanFace,
  Trash2,
  Video,
  Volume2,
} from 'lucide-react'
import {
  exportSrtUrl,
  exportThumbUrl,
  exportVideoUrl,
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
  ClipItem,
  ClipStatus,
  ClipUpdateInput,
  CropBox,
  LayoutMode,
  ThumbnailRequest,
} from '../types'

interface ReviewWorkbenchProps {
  clips: ClipItem[]
  loading: boolean
  error: string | null
  selectedClipId: string | null
  onSelectClip: (clipId: string) => void
  onRetryClips: () => void
  onUpdateClip: (clipId: string, payload: ClipUpdateInput) => Promise<ClipItem>
  onDetectWebcam: (clipId: string) => Promise<ClipItem>
  onSavePreset: (clipId: string) => Promise<void>
  onRenderClip: (clipId: string) => Promise<void>
  onGenerateCaptions: (clipId: string) => Promise<ClipItem>
  onGenerateThumbnail: (clipId: string, payload: ThumbnailRequest) => Promise<ClipItem>
  onDeleteClip: (clipId: string) => Promise<void>
  onOpenVodModal: () => void
}

type QueueFilter = 'pending_review' | 'exported' | 'all'

const DEFAULT_WEBCAM: CropBox = { x: 0.03, y: 0.62, w: 0.25, h: 0.34 }
const DEFAULT_GAMEPLAY: CropBox = { x: 0.22, y: 0.0, w: 0.56, h: 1.0 }

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
  if (status === 'pending_review') return 'Pending'
  if (status === 'rendering') return 'Rendering'
  if (status === 'exported') return 'Exported'
  if (status === 'failed') return 'Failed'
  return 'Discarded'
}

export function ReviewWorkbench({
  clips,
  loading,
  error,
  selectedClipId,
  onSelectClip,
  onRetryClips,
  onUpdateClip,
  onDetectWebcam,
  onSavePreset,
  onRenderClip,
  onGenerateCaptions,
  onGenerateThumbnail,
  onDeleteClip,
  onOpenVodModal,
}: ReviewWorkbenchProps) {
  const [filter, setFilter] = useState<QueueFilter>('pending_review')

  const filteredClips = useMemo(() => {
    if (filter === 'all') return clips.filter((c) => c.status !== 'discarded')
    return clips.filter((c) => c.status === filter)
  }, [clips, filter])

  const selectedClip = useMemo(() => {
    const exact = clips.find((c) => c.id === selectedClipId)
    if (exact) return exact
    return filteredClips[0] ?? clips[0] ?? null
  }, [clips, filteredClips, selectedClipId])

  const [webcamBox, setWebcamBox] = useState<CropBox>(DEFAULT_WEBCAM)
  const [gameplayBox, setGameplayBox] = useState<CropBox>(DEFAULT_GAMEPLAY)
  const [layoutMode, setLayoutMode] = useState<LayoutMode>('split_top_cam')
  const [splitRatio, setSplitRatio] = useState<number>(0.35)
  const [trimStart, setTrimStart] = useState<number>(0)
  const [trimEnd, setTrimEnd] = useState<number>(10)
  const [normalizeAudio, setNormalizeAudio] = useState<boolean>(true)
  const [captionsEnabled, setCaptionsEnabled] = useState<boolean>(true)
  const [captionStyle, setCaptionStyle] = useState<CaptionStyle>('bold_pop')
  const [captionPosition, setCaptionPosition] = useState<CaptionPosition>('seam')
  const [captionsDraft, setCaptionsDraft] = useState<CaptionSegment[]>([])
  const [captionsDirty, setCaptionsDirty] = useState<boolean>(false)
  const [showCaptionEditor, setShowCaptionEditor] = useState<boolean>(false)
  const [thumbTitleOverlay, setThumbTitleOverlay] = useState<boolean>(false)
  const [thumbCacheBust, setThumbCacheBust] = useState<number>(0)
  const [previewExported, setPreviewExported] = useState<boolean>(false)

  const [isPlaying, setIsPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [videoDuration, setVideoDuration] = useState(0)
  const [videoError, setVideoError] = useState<string | null>(null)

  const [detectBusy, setDetectBusy] = useState(false)
  const [presetBusy, setPresetBusy] = useState(false)
  const [captionsBusy, setCaptionsBusy] = useState(false)
  const [captionsSaveBusy, setCaptionsSaveBusy] = useState(false)
  const [thumbBusy, setThumbBusy] = useState(false)
  const [renderBusy, setRenderBusy] = useState(false)
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
    setShowCaptionEditor(false)
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
    const bgPaper = bodyStyle.backgroundColor || styles.getPropertyValue('--color-paper').trim()
    const bgWell = styles.getPropertyValue('--color-paper-2').trim() || bgPaper
    const ruleColor = styles.getPropertyValue('--color-rule').trim() || bodyStyle.color
    const mutedColor = styles.getPropertyValue('--color-muted').trim() || bodyStyle.color
    const inkColor = styles.getPropertyValue('--color-ink').trim() || bodyStyle.color
    const accentColor = styles.getPropertyValue('--color-accent').trim() || inkColor

    const drawLiveCaptions = (seamY?: number) => {
      if (!captionsEnabled || previewExported || captionsDraft.length === 0) return
      const relTime = Math.max(0, currentTime - trimStart)
      const activeSeg = captionsDraft.find((seg) => relTime >= seg.start && relTime <= seg.end)
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
      ctx.font = `700 ${fontSize}px Barlow, sans-serif`
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
      ctx.font = '14px Barlow, sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText(videoError ? 'Source stream unavailable' : 'Load clip to inspect 9:16 stage', cw / 2, ch / 2)
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

    const camH = Math.round((ch * Math.max(0.25, Math.min(0.55, splitRatio))) / 2) * 2
    const gameH = ch - camH
    drawCoverCrop(webcamBox, 0, 0, cw, camH)
    drawCoverCrop(gameplayBox, 0, camH, cw, gameH)

    ctx.fillStyle = bgPaper
    ctx.fillRect(0, camH - 1, cw, 2)
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
  }, [drawVerticalStage, currentTime])

  useEffect(() => {
    let rafId: number | null = null
    const step = () => {
      const video = videoRef.current
      if (video && !video.paused && !video.ended) {
        if (video.currentTime >= trimEnd) {
          video.currentTime = trimStart
        }
        setCurrentTime(video.currentTime)
        drawVerticalStage()
        rafId = window.requestAnimationFrame(step)
      }
    }
    if (isPlaying) {
      rafId = window.requestAnimationFrame(step)
    }
    return () => {
      if (rafId !== null) window.cancelAnimationFrame(rafId)
    }
  }, [drawVerticalStage, isPlaying, trimEnd, trimStart])

  const commitCropPatch = useCallback(
    async (patch: ClipUpdateInput) => {
      if (!selectedClip) return
      try {
        await onUpdateClip(selectedClip.id, patch)
      } catch (err) {
        setActionFeedback({
          kind: 'error',
          text: err instanceof Error ? err.message : 'Unable to save clip edit.',
        })
      }
    },
    [onUpdateClip, selectedClip],
  )

  const handleBoxPointerDown = (
    target: 'webcam' | 'gameplay',
    mode: 'move' | 'resize',
    e: React.PointerEvent<HTMLElement>,
  ) => {
    e.stopPropagation()
    e.preventDefault()
    const current = target === 'webcam' ? webcamBox : gameplayBox
    dragStateRef.current = {
      target,
      mode,
      startX: e.clientX,
      startY: e.clientY,
      origin: current,
    }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const handleBoxPointerMove = (e: React.PointerEvent<HTMLElement>) => {
    const drag = dragStateRef.current
    const stage = stageRef.current
    if (!drag || !stage) return
    const rect = stage.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return

    const dx = (e.clientX - drag.startX) / rect.width
    const dy = (e.clientY - drag.startY) / rect.height
    const o = drag.origin

    const next =
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

  const togglePlayback = async () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      if (video.currentTime < trimStart || video.currentTime >= trimEnd) {
        video.currentTime = trimStart
      }
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
  }

  const seekTo = (sec: number) => {
    const clamped = Math.max(0, Math.min(videoDuration || 1, sec))
    setCurrentTime(clamped)
    if (videoRef.current) {
      videoRef.current.currentTime = clamped
    }
  }

  const handleLayoutPreset = async (mode: LayoutMode, ratio: number) => {
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
      await onSavePreset(selectedClip.id)
      setActionFeedback({
        kind: 'success',
        text: `Saved layout and crop preset to @${selectedClip.channel_login}.`,
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

  const handleGenerateCaptions = async () => {
    if (!selectedClip) return
    setCaptionsBusy(true)
    setActionFeedback(null)
    try {
      const updated = await onGenerateCaptions(selectedClip.id)
      const parsed = parseCaptions(updated)
      setCaptionsDraft(parsed)
      setCaptionsEnabled(updated.captions_enabled ?? true)
      setCaptionsDirty(false)
      setShowCaptionEditor(parsed.length > 0)
      setActionFeedback({
        kind: 'success',
        text:
          parsed.length > 0
            ? `Generated ${parsed.length} caption lines.`
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

  const handleSaveCaptions = async () => {
    if (!selectedClip) return
    setCaptionsSaveBusy(true)
    setActionFeedback(null)
    try {
      const updated = await onUpdateClip(selectedClip.id, {
        captions_enabled: captionsEnabled,
        caption_style: captionStyle,
        caption_position: captionPosition,
        captions: captionsDraft,
      })
      setCaptionsDraft(parseCaptions(updated))
      setCaptionsDirty(false)
      setActionFeedback({
        kind: 'success',
        text: 'Saved edited closed captions.',
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
      await onUpdateClip(selectedClip.id, {
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
      })
      setCaptionsDirty(false)
      await onRenderClip(selectedClip.id)
      setActionFeedback({
        kind: 'success',
        text: captionsEnabled
          ? 'Render queued with CC burn-in and 9:16 cover thumbnail.'
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
      pending_review: clips.filter((c) => c.status === 'pending_review').length,
      exported: clips.filter((c) => c.status === 'exported').length,
      all: clips.filter((c) => c.status !== 'discarded').length,
    }
  }, [clips])

  return (
    <section aria-labelledby="workbench-heading" className="flex-1 flex flex-col min-w-0 bg-paper">
      {/* Queue Filter Strip */}
      <div className="border-b border-rule bg-paper-2 p-4">
        <div className="flex items-center justify-between gap-4 flex-wrap mb-3">
          <div className="flex items-center gap-3">
            <Film size={17} className="text-accent shrink-0" aria-hidden="true" />
            <h2 id="workbench-heading" className="section-title text-ink">
              Review Workbench
            </h2>
          </div>

          <div role="tablist" aria-label="Clip queue filter" className="flex items-center">
            {(
              [
                { id: 'pending_review', label: `Pending Review (${counts.pending_review})` },
                { id: 'exported', label: `Exported (${counts.exported})` },
                { id: 'all', label: `All (${counts.all})` },
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

        {loading && clips.length === 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3" aria-busy="true" aria-label="Loading clips">
            {[1, 2, 3].map((n) => (
              <div key={n} className="h-20 bg-paper border border-rule p-3">
                <div className="h-4 bg-paper-3 w-1/2 mb-2" />
                <div className="h-3 bg-paper-3 w-3/4" />
              </div>
            ))}
          </div>
        ) : error && clips.length === 0 ? (
          <div className="flex items-center justify-between gap-4 p-3 border border-error bg-paper">
            <p className="text-sm text-error">{error}</p>
            <button type="button" onClick={onRetryClips} className="btn btn--quiet btn--compact">
              <RefreshCw size={14} aria-hidden="true" />
              <span>Retry</span>
            </button>
          </div>
        ) : filteredClips.length === 0 ? (
          <div className="p-4 border border-rule bg-paper flex items-center justify-between gap-4 flex-wrap">
            <div>
              <p className="text-sm font-medium text-ink">
                {filter === 'pending_review'
                  ? 'No clips waiting in the review queue.'
                  : filter === 'exported'
                    ? 'No 9:16 exports rendered yet.'
                    : 'No captured clips yet.'}
              </p>
              <p className="text-sm text-muted">
                Monitor a live Twitch channel or scan a past VOD to populate the editing bay.
              </p>
            </div>
            <button type="button" onClick={onOpenVodModal} className="btn btn--accent-outline">
              <Video size={15} aria-hidden="true" />
              <span>Scan Twitch VOD</span>
            </button>
          </div>
        ) : (
          <div className="flex items-stretch gap-3 overflow-x-auto pb-1">
            {filteredClips.map((clip) => {
              const active = selectedClip?.id === clip.id
              return (
                <button
                  key={clip.id}
                  type="button"
                  data-selected={active ? 'true' : 'false'}
                  onClick={() => onSelectClip(clip.id)}
                  className="interactive-row shrink-0 w-72 p-2 border border-rule bg-paper flex items-center gap-3"
                >
                  <div className="w-20 h-12 bg-paper-2 border border-rule shrink-0 overflow-hidden flex items-center justify-center">
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
                      <Film size={16} className="text-muted" aria-hidden="true" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-ink truncate">{clip.title}</span>
                      <span className="figure-lg text-accent shrink-0">
                        {clip.hype_score.toFixed(0)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-muted mt-1">
                      <span className="truncate">{clip.trigger_reason || clip.channel_login}</span>
                      <span className="num shrink-0 ml-2">
                        {formatTimecode(clip.duration_sec)}
                      </span>
                    </div>
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </div>

      {/* Main Signature Editing Stage */}
      {selectedClip ? (
        <div className="p-4 flex flex-col gap-4">
          {/* Clip Header & Framing Preset Bar */}
          <div className="flex items-center justify-between gap-4 flex-wrap pb-3 border-b border-rule">
            <div className="min-w-0">
              <div className="flex items-center gap-3 flex-wrap">
                <h3 className="section-title text-ink">{selectedClip.title}</h3>
                <span
                  className={`px-2 py-0.5 border text-xs font-medium ${
                    selectedClip.status === 'exported'
                      ? 'border-success text-success'
                      : selectedClip.status === 'failed'
                        ? 'border-error text-error'
                        : selectedClip.status === 'rendering'
                          ? 'border-accent text-accent'
                          : 'border-rule text-ink-2'
                  }`}
                >
                  {formatStatusLabel(selectedClip.status)}
                </span>
              </div>
              <p className="text-sm text-muted mt-1">
                Channel @{selectedClip.channel_login}, trigger: {selectedClip.trigger_reason || 'manual'}, hype score{' '}
                <span className="num text-ink">{selectedClip.hype_score.toFixed(1)}</span>
              </p>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                disabled={detectBusy}
                aria-busy={detectBusy}
                onClick={handleDetectWebcam}
                className="btn btn--quiet"
              >
                {detectBusy ? (
                  <span className="desk-spinner" aria-hidden="true" />
                ) : (
                  <ScanFace size={15} aria-hidden="true" />
                )}
                <span>{detectBusy ? 'Detecting' : 'Re-detect Webcam'}</span>
              </button>

              <button
                type="button"
                disabled={presetBusy}
                aria-busy={presetBusy}
                onClick={handleSavePreset}
                className="btn btn--quiet"
              >
                {presetBusy ? (
                  <span className="desk-spinner" aria-hidden="true" />
                ) : (
                  <BookmarkPlus size={15} aria-hidden="true" />
                )}
                <span>{presetBusy ? 'Saving' : 'Save as Channel Preset'}</span>
              </button>
            </div>
          </div>

          {/* Side-by-side 16:9 Source Crop Monitor + Synced 9:16 Vertical Canvas */}
          <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_320px] gap-4 items-start">
            {/* 16:9 Source Video + Interactive Crop Boxes */}
            <div className="flex flex-col gap-3 min-w-0">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <span className="text-sm font-medium text-ink-2">
                  16:9 Source Monitor (drag frames or use arrow keys to adjust crop)
                </span>
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
                    {previewExported ? 'Preview 16:9 Source' : 'Preview Exported 9:16'}
                  </button>
                )}
              </div>

              <div
                ref={stageRef}
                className="relative w-full aspect-video bg-paper-2 border border-rule overflow-hidden"
              >
                <video
                  ref={videoRef}
                  key={`${selectedClip.id}-${previewExported ? 'exp' : 'raw'}`}
                  src={previewExported ? exportVideoUrl(selectedClip.id) : rawVideoUrl(selectedClip.id)}
                  poster={selectedClip.thumbnail_path ? thumbImageUrl(selectedClip.id) : undefined}
                  preload="metadata"
                  playsInline
                  onLoadedMetadata={(e) => {
                    const el = e.currentTarget
                    const dur = Number.isFinite(el.duration) && el.duration > 0 ? el.duration : effectiveDur
                    setVideoDuration(dur)
                    setTrimEnd((prev) => (prev > dur || prev <= trimStart ? dur : prev))
                    el.currentTime = trimStart
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
                  className="w-full h-full object-contain bg-paper"
                />

                {!previewExported && (
                  <div className="absolute inset-0">
                    {/* Gameplay Crop Overlay (Cyan-Steel) */}
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
                      <span className="inline-block px-1.5 py-0.5 bg-paper text-signal-game text-xs font-medium pointer-events-none">
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

                    {/* Webcam Crop Overlay (Amber Safelight) */}
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
                        <span className="inline-block px-1.5 py-0.5 bg-paper text-accent text-xs font-medium pointer-events-none">
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

              {/* Playback Transport + Dual Trim Scrubber over Hype Timeline */}
              <div className="p-3 border border-rule bg-paper-2 flex flex-col gap-3">
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={togglePlayback}
                      className="btn btn--compact btn--accent-outline"
                    >
                      {isPlaying ? <Pause size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                      <span>{isPlaying ? 'Pause' : 'Play'}</span>
                    </button>
                    <span className="num text-sm text-ink">
                      {formatTimecode(currentTime)} / {formatTimecode(effectiveDur)}
                    </span>
                  </div>

                  <div className="flex items-center gap-4 text-xs text-muted">
                    <span>
                      In <strong className="num text-ink font-medium">{formatTimecode(trimStart)}</strong>
                    </span>
                    <span>
                      Out <strong className="num text-ink font-medium">{formatTimecode(trimEnd)}</strong>
                    </span>
                    <span>
                      Clip <strong className="num text-accent font-medium">{formatTimecode(trimEnd - trimStart)}</strong>
                    </span>
                  </div>
                </div>

                {/* Hype Timeline + Interactive Dual Trim Handles */}
                <div className="relative h-16 border border-rule bg-paper">
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
                      fill="var(--color-paper-3)"
                    />
                    {timelinePolyline && (
                      <polyline
                        fill="none"
                        stroke="var(--color-accent)"
                        strokeWidth={1.75}
                        points={timelinePolyline}
                      />
                    )}
                    <line
                      x1={(currentTime / effectiveDur) * 640}
                      y1={0}
                      x2={(currentTime / effectiveDur) * 640}
                      y2={64}
                      stroke="var(--color-ink)"
                      strokeWidth={1.5}
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
                    onPointerUp={() => commitCropPatch({ trim_start_sec: trimStart })}
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
                    onPointerUp={() => commitCropPatch({ trim_end_sec: trimEnd })}
                    className="trim-range absolute inset-0"
                  />
                </div>
              </div>
            </div>

            {/* Right Column: Live 9:16 Vertical Stage + Export Controls */}
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-ink-2">9:16 Vertical Stage</span>
                <span className="num text-xs text-muted">1080x1920 MP4</span>
              </div>

              <div className="border border-rule bg-paper-2 p-3 flex flex-col items-center gap-3">
                <canvas
                  ref={canvasRef}
                  width={270}
                  height={480}
                  aria-label="Synced 9:16 vertical stage preview"
                  className="w-full max-w-[270px] aspect-[9/16] border border-rule bg-paper"
                />

                {/* Split Ratio Switcher */}
                <div className="w-full">
                  <span className="block text-xs text-muted mb-1">Stage layout</span>
                  <div className="grid grid-cols-4" role="group" aria-label="Vertical split preset">
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
                  <span className="flex items-center gap-2 text-sm text-ink-2">
                    <Volume2 size={15} className="text-muted" aria-hidden="true" />
                    <span>Normalize -14 LUFS</span>
                  </span>
                  <input
                    type="checkbox"
                    checked={normalizeAudio}
                    onChange={(e) => {
                      const val = e.target.checked
                      setNormalizeAudio(val)
                      void commitCropPatch({ normalize_audio: val })
                    }}
                    className="w-4 h-4 accent-accent"
                  />
                </label>

                {/* Closed Captions (CC) Controls */}
                <div className="w-full flex flex-col gap-2 pb-2 border-b border-rule">
                  <label className="w-full flex items-center justify-between gap-2 py-1 cursor-pointer">
                    <span className="flex items-center gap-2 text-sm text-ink-2">
                      <Captions size={15} className="text-muted" aria-hidden="true" />
                      <span>Burn Closed Captions (CC)</span>
                    </span>
                    <input
                      type="checkbox"
                      checked={captionsEnabled}
                      onChange={(e) => {
                        const val = e.target.checked
                        setCaptionsEnabled(val)
                        void commitCropPatch({ captions_enabled: val })
                      }}
                      className="w-4 h-4 accent-accent"
                    />
                  </label>

                  {captionsEnabled && (
                    <>
                      <div className="grid grid-cols-3" role="group" aria-label="Caption style">
                        <button
                          type="button"
                          aria-pressed={captionStyle === 'bold_pop'}
                          onClick={() => {
                            setCaptionStyle('bold_pop')
                            void commitCropPatch({ caption_style: 'bold_pop' })
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
                            void commitCropPatch({ caption_style: 'clean_box' })
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
                            void commitCropPatch({ caption_style: 'classic_cc' })
                          }}
                          className="seg-cell"
                        >
                          Classic CC
                        </button>
                      </div>

                      {layoutMode === 'split_top_cam' && (
                        <div className="grid grid-cols-2" role="group" aria-label="Caption placement">
                          <button
                            type="button"
                            aria-pressed={captionPosition === 'seam'}
                            onClick={() => {
                              setCaptionPosition('seam')
                              void commitCropPatch({ caption_position: 'seam' })
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
                              void commitCropPatch({ caption_position: 'lower_safe' })
                            }}
                            className="seg-cell"
                          >
                            Lower Safe
                          </button>
                        </div>
                      )}

                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          disabled={captionsBusy}
                          aria-busy={captionsBusy}
                          onClick={handleGenerateCaptions}
                          className="btn btn--compact btn--quiet flex-1"
                        >
                          {captionsBusy ? (
                            <span className="desk-spinner" aria-hidden="true" />
                          ) : (
                            <Captions size={14} aria-hidden="true" />
                          )}
                          <span>
                            {captionsBusy
                              ? 'Transcribing'
                              : captionsDraft.length > 0
                                ? `Re-generate CC (${captionsDraft.length})`
                                : 'Generate CC'}
                          </span>
                        </button>

                        {captionsDraft.length > 0 && (
                          <button
                            type="button"
                            aria-expanded={showCaptionEditor}
                            onClick={() => setShowCaptionEditor((v) => !v)}
                            className="btn btn--compact btn--quiet"
                          >
                            {showCaptionEditor ? (
                              <ChevronUp size={14} aria-hidden="true" />
                            ) : (
                              <ChevronDown size={14} aria-hidden="true" />
                            )}
                            <span>Edit</span>
                          </button>
                        )}
                      </div>

                      {showCaptionEditor && captionsDraft.length > 0 && (
                        <div className="flex flex-col gap-2 pt-2 border-t border-rule">
                          <div className="max-h-40 overflow-y-auto flex flex-col gap-2 pr-1">
                            {captionsDraft.map((seg, idx) => (
                              <div key={`${seg.start}-${idx}`} className="flex items-center gap-2">
                                <span className="num text-xs text-muted w-11 shrink-0">
                                  {formatTimecode(seg.start)}
                                </span>
                                <input
                                  type="text"
                                  aria-label={`Caption line ${idx + 1} at ${formatTimecode(seg.start)}`}
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
                                  className="field-input flex-1 text-xs"
                                />
                              </div>
                            ))}
                          </div>
                          {captionsDirty && (
                            <button
                              type="button"
                              disabled={captionsSaveBusy}
                              aria-busy={captionsSaveBusy}
                              onClick={handleSaveCaptions}
                              className="btn btn--compact btn--accent-outline w-full"
                            >
                              {captionsSaveBusy && <span className="desk-spinner" aria-hidden="true" />}
                              <span>{captionsSaveBusy ? 'Saving CC' : 'Save CC edits'}</span>
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
                      ? 'Rendering 9:16'
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
                        <span>Download CC (.srt)</span>
                      </a>
                    )}
                  </div>
                )}

                {/* 9:16 Cover Thumbnail Capture & Download */}
                <div className="w-full flex flex-col gap-2 pt-2 border-t border-rule">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-2 text-xs font-medium text-ink-2">
                      <Image size={14} className="text-muted" aria-hidden="true" />
                      <span>9:16 Cover Thumbnail</span>
                    </span>
                    {selectedClip.thumb_time_sec != null && (
                      <span className="num text-xs text-muted">
                        @ {formatTimecode(selectedClip.thumb_time_sec)}
                      </span>
                    )}
                  </div>

                  <label className="w-full flex items-center justify-between gap-2 py-1 cursor-pointer">
                    <span className="text-xs text-ink-2">Stamp clip title on thumbnail</span>
                    <input
                      type="checkbox"
                      checked={thumbTitleOverlay}
                      onChange={(e) => {
                        const val = e.target.checked
                        setThumbTitleOverlay(val)
                        void commitCropPatch({ thumb_title_overlay: val })
                      }}
                      className="w-4 h-4 accent-accent"
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
                    <span>{thumbBusy ? 'Capturing cover' : 'Capture Cover Frame'}</span>
                  </button>

                  {selectedClip.export_thumb_path && (
                    <div className="flex items-center gap-3 pt-1">
                      <img
                        src={exportThumbUrl(selectedClip.id, thumbCacheBust)}
                        alt={`9:16 cover thumbnail for ${selectedClip.title}`}
                        className="w-16 aspect-[9/16] object-cover border border-rule bg-paper shrink-0"
                      />
                      <a
                        href={exportThumbUrl(selectedClip.id, thumbCacheBust)}
                        download={`${selectedClip.channel_login}_${selectedClip.id.slice(0, 8)}_9x16.jpg`}
                        className="btn btn--compact btn--accent-outline flex-1 whitespace-nowrap"
                      >
                        <Download size={14} aria-hidden="true" />
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
                        Confirm discard
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
                      className="btn btn--compact btn--quiet w-full"
                    >
                      <Trash2 size={14} aria-hidden="true" />
                      <span>Discard clip</span>
                    </button>
                  )}
                </div>
              </div>

              {chatSamples.length > 0 && (
                <div className="border border-rule bg-paper-2 p-3">
                  <p className="text-xs font-medium text-muted mb-2">Trigger chat context</p>
                  <ul className="list-none m-0 p-0 flex flex-col gap-1 max-h-28 overflow-y-auto">
                    {chatSamples.slice(0, 6).map((s, idx) => (
                      <li key={`${s.author}-${idx}`} className="text-xs leading-snug">
                        <span className="text-accent font-medium mr-1.5">{s.author}</span>
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
          <div className="desk-well grained max-w-lg w-full p-8 border border-rule flex flex-col gap-4">
            <h3 className="section-title text-ink">Stage ready for incoming hype clips</h3>
            <p className="text-sm text-ink-2">
              Start live monitoring on a channel in the left rail, trigger an instant buffer capture with Clip Now, or scan a Twitch VOD URL to detect peak moments and webcam bounds automatically.
            </p>
            <div className="flex items-center gap-3 flex-wrap">
              <button type="button" onClick={onOpenVodModal} className="btn btn--primary">
                <Video size={16} aria-hidden="true" />
                <span>Scan Twitch VOD</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
