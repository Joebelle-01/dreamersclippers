import { useEffect, useRef, useState } from 'react'
import { Download, Film, KeyRound, Link2, Radio, Unlink, X } from 'lucide-react'
import { extractTwitchLogin, isValidTwitchClipUrl } from '../api'
import type { Channel, ClipPeriod, TwitchClipSummary, TwitchStatus } from '../types'

interface TwitchSettingsModalProps {
  open: boolean
  channels: Channel[]
  twitchStatus: TwitchStatus | null
  statusLoading: boolean
  onClose: () => void
  onRefreshStatus: () => Promise<void>
  onSaveConfig: (clientId: string, clientSecret: string) => Promise<void>
  onStartOAuth: (mode: 'viewer' | 'broadcaster') => Promise<string>
  onDisconnectOAuth: () => Promise<void>
  onFetchTopClips: (login: string, period: ClipPeriod) => Promise<TwitchClipSummary[]>
  onImportClip: (clipUrl: string, broadcasterLogin?: string | null, preferPortrait?: boolean) => Promise<void>
}

export function TwitchSettingsModal({
  open,
  channels,
  twitchStatus,
  statusLoading,
  onClose,
  onRefreshStatus,
  onSaveConfig,
  onStartOAuth,
  onDisconnectOAuth,
  onFetchTopClips,
  onImportClip,
}: TwitchSettingsModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null)
  const clientIdRef = useRef<HTMLInputElement | null>(null)

  const [activeTab, setActiveTab] = useState<'api' | 'clips'>('api')

  // API & OAuth form state
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [configBusy, setConfigBusy] = useState(false)
  const [oauthBusy, setOauthBusy] = useState<'viewer' | 'broadcaster' | 'disconnect' | null>(null)
  const [apiFeedback, setApiFeedback] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  // Top Clips Importer state
  const [selectedLoginOption, setSelectedLoginOption] = useState<string>('')
  const [customLogin, setCustomLogin] = useState('')
  const [period, setPeriod] = useState<ClipPeriod>('24h')
  const [preferPortrait, setPreferPortrait] = useState(false)
  const [fetchingClips, setFetchingClips] = useState(false)
  const [topClips, setTopClips] = useState<TwitchClipSummary[]>([])
  const [clipsError, setClipsError] = useState<string | null>(null)
  const [clipsFetchedOnce, setClipsFetchedOnce] = useState(false)
  const [directClipUrl, setDirectClipUrl] = useState('')
  const [importingKey, setImportingKey] = useState<string | null>(null)
  const [importFeedback, setImportFeedback] = useState<{ kind: 'error' | 'success'; text: string } | null>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (open && !dialog.open) {
      dialog.showModal()
      void onRefreshStatus()
      if (!selectedLoginOption && channels.length > 0) {
        setSelectedLoginOption(channels[0].channel_login)
      }
      window.requestAnimationFrame(() => clientIdRef.current?.focus())
    } else if (!open && dialog.open) {
      dialog.close()
    }
  }, [channels, onRefreshStatus, open, selectedLoginOption])

  useEffect(() => {
    if (!open) return
    const handleFocus = () => {
      void onRefreshStatus()
    }
    const handleMessage = (event: MessageEvent) => {
      if (typeof event.data === 'object' && event.data !== null) {
        const msg = event.data as { type?: unknown }
        if (msg.type === 'twitch_oauth_complete') {
          void onRefreshStatus()
          setApiFeedback({ kind: 'success', text: 'Twitch account connected.' })
        }
      }
    }
    window.addEventListener('focus', handleFocus)
    window.addEventListener('message', handleMessage)
    return () => {
      window.removeEventListener('focus', handleFocus)
      window.removeEventListener('message', handleMessage)
    }
  }, [onRefreshStatus, open])

  const handleSaveCredentials = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setApiFeedback(null)
    const trimmedId = clientId.trim()
    const trimmedSecret = clientSecret.trim()
    if (!trimmedId || !trimmedSecret) {
      setApiFeedback({ kind: 'error', text: 'Enter both Twitch Client ID and Client Secret.' })
      return
    }
    setConfigBusy(true)
    try {
      await onSaveConfig(trimmedId, trimmedSecret)
      setClientSecret('')
      setApiFeedback({ kind: 'success', text: 'Twitch app credentials verified and saved.' })
    } catch (err) {
      setApiFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to verify Twitch credentials.',
      })
    } finally {
      setConfigBusy(false)
    }
  }

  const handleLaunchOAuth = async (mode: 'viewer' | 'broadcaster') => {
    setApiFeedback(null)
    setOauthBusy(mode)
    try {
      const authorizeUrl = await onStartOAuth(mode)
      const popup = window.open(authorizeUrl, 'twitch_oauth', 'width=560,height=720')
      if (!popup) {
        window.location.assign(authorizeUrl)
      } else {
        setApiFeedback({
          kind: 'success',
          text: 'Complete authorization in the Twitch popup window.',
        })
      }
    } catch (err) {
      setApiFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to start Twitch OAuth.',
      })
    } finally {
      setOauthBusy(null)
    }
  }

  const handleDisconnect = async () => {
    setApiFeedback(null)
    setOauthBusy('disconnect')
    try {
      await onDisconnectOAuth()
      setApiFeedback({ kind: 'success', text: 'Twitch user OAuth disconnected.' })
    } catch (err) {
      setApiFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to disconnect Twitch account.',
      })
    } finally {
      setOauthBusy(null)
    }
  }

  const targetLogin =
    selectedLoginOption === '__custom__' || channels.length === 0
      ? extractTwitchLogin(customLogin)
      : selectedLoginOption

  const handleFetchTopClips = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    setClipsError(null)
    setImportFeedback(null)
    if (!targetLogin) {
      setClipsError('Enter a valid Twitch channel login or URL.')
      return
    }
    setFetchingClips(true)
    try {
      const items = await onFetchTopClips(targetLogin, period)
      setTopClips(items)
      setClipsFetchedOnce(true)
    } catch (err) {
      setClipsError(err instanceof Error ? err.message : 'Failed to fetch Twitch clips.')
    } finally {
      setFetchingClips(false)
    }
  }

  const handleImportOne = async (
    key: string,
    clipUrl: string,
    broadcasterLogin?: string | null,
  ) => {
    setImportingKey(key)
    setImportFeedback(null)
    try {
      const usePortrait = Boolean(twitchStatus?.has_broadcaster_scopes && preferPortrait)
      await onImportClip(clipUrl, broadcasterLogin ?? targetLogin, usePortrait)
      setImportFeedback({
        kind: 'success',
        text: 'Clip imported into Review Workbench.',
      })
    } catch (err) {
      setImportFeedback({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Unable to import clip.',
      })
    } finally {
      setImportingKey(null)
    }
  }

  const handleDirectClipSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const cleaned = directClipUrl.trim()
    if (!isValidTwitchClipUrl(cleaned)) {
      setImportFeedback({
        kind: 'error',
        text: 'Enter a valid https://clips.twitch.tv/... or https://twitch.tv/user/clip/... URL.',
      })
      return
    }
    await handleImportOne('direct-url', cleaned, targetLogin)
    setDirectClipUrl('')
  }

  const broadcasterAllowed = Boolean(twitchStatus?.has_broadcaster_scopes)

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="twitch-modal-title"
      onCancel={(e) => {
        e.preventDefault()
        onClose()
      }}
      onClick={(e) => {
        if (e.target === dialogRef.current) onClose()
      }}
      className="darkroom-dialog darkroom-dialog--wide"
    >
      <div className="p-5 flex flex-col gap-4">
        <div className="flex items-center justify-between gap-4 pb-3 border-b border-rule">
          <div className="flex items-center gap-2">
            <KeyRound size={18} className="text-accent" aria-hidden="true" />
            <h2 id="twitch-modal-title" className="section-title text-ink">
              Twitch API and Clips
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close Twitch settings"
            className="btn btn--compact btn--quiet"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div role="tablist" aria-label="Twitch settings sections" className="grid grid-cols-2">
          <button
            type="button"
            role="tab"
            id="twitch-tab-api"
            aria-selected={activeTab === 'api'}
            onClick={() => setActiveTab('api')}
            className="seg-cell"
          >
            API and OAuth
          </button>
          <button
            type="button"
            role="tab"
            id="twitch-tab-clips"
            aria-selected={activeTab === 'clips'}
            onClick={() => setActiveTab('clips')}
            className="seg-cell"
          >
            Import Top Clips
          </button>
        </div>

        {activeTab === 'api' ? (
          <div role="tabpanel" aria-labelledby="twitch-tab-api" className="flex flex-col gap-4">
            <div className="p-3 border border-rule bg-paper flex flex-col gap-2 text-sm">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2">
                  <span className="text-muted">App token</span>
                  <span
                    className={`font-medium ${
                      twitchStatus?.app_token_ready ? 'text-success' : 'text-ink-2'
                    }`}
                  >
                    {statusLoading && !twitchStatus
                      ? 'Checking'
                      : twitchStatus?.app_token_ready
                        ? `Ready (${twitchStatus.client_id_masked ?? 'saved'})`
                        : twitchStatus?.configured
                          ? `Saved (${twitchStatus.client_id_masked ?? ''})`
                          : 'Not configured'}
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <Radio size={14} className={twitchStatus?.eventsub_connected ? 'text-success' : 'text-muted'} aria-hidden="true" />
                  <span
                    className={`font-medium ${
                      twitchStatus?.eventsub_connected ? 'text-success' : 'text-muted'
                    }`}
                  >
                    {twitchStatus?.eventsub_connected ? 'EventSub Live' : 'EventSub Standby'}
                  </span>
                </div>
              </div>

              <div className="flex items-center justify-between gap-3 flex-wrap pt-2 border-t border-rule">
                <div className="flex items-center gap-2">
                  <span className="text-muted">User OAuth</span>
                  <span
                    className={`font-medium ${
                      twitchStatus?.user_connected ? 'text-accent' : 'text-muted'
                    }`}
                  >
                    {twitchStatus?.user_connected
                      ? `@${twitchStatus.user_login ?? 'connected'} (${
                          twitchStatus.has_broadcaster_scopes ? 'Broadcaster' : 'Viewer'
                        })`
                      : 'Disconnected'}
                  </span>
                </div>
                {twitchStatus?.user_connected && (
                  <button
                    type="button"
                    disabled={oauthBusy !== null}
                    aria-busy={oauthBusy === 'disconnect'}
                    onClick={handleDisconnect}
                    className="btn btn--compact btn--danger"
                  >
                    <Unlink size={13} aria-hidden="true" />
                    <span>Disconnect</span>
                  </button>
                )}
              </div>

              <div className="pt-2 border-t border-rule text-xs text-muted flex flex-col gap-1">
                <span>OAuth Redirect URI for Twitch Developer Console:</span>
                <code className="text-ink-2 break-all">
                  {twitchStatus?.redirect_uri || 'http://127.0.0.1:8430/api/twitch/oauth/callback'}
                </code>
              </div>
            </div>

            <form onSubmit={handleSaveCredentials} noValidate className="flex flex-col gap-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label htmlFor="twitch-client-id" className="block text-sm text-ink-2 mb-1">
                    Twitch Client ID
                  </label>
                  <input
                    ref={clientIdRef}
                    id="twitch-client-id"
                    name="client_id"
                    type="text"
                    autoComplete="off"
                    placeholder={twitchStatus?.client_id_masked || 'Enter Twitch Client ID'}
                    value={clientId}
                    onChange={(e) => {
                      setClientId(e.target.value)
                      setApiFeedback(null)
                    }}
                    disabled={configBusy}
                    className="field-input"
                  />
                </div>

                <div>
                  <label htmlFor="twitch-client-secret" className="block text-sm text-ink-2 mb-1">
                    Twitch Client Secret
                  </label>
                  <input
                    id="twitch-client-secret"
                    name="client_secret"
                    type="password"
                    autoComplete="new-password"
                    placeholder={twitchStatus?.configured ? 'Saved on server (enter to replace)' : 'Enter Client Secret'}
                    value={clientSecret}
                    onChange={(e) => {
                      setClientSecret(e.target.value)
                      setApiFeedback(null)
                    }}
                    disabled={configBusy}
                    className="field-input"
                  />
                </div>
              </div>

              <p
                aria-live="polite"
                className={`field-help ${
                  apiFeedback?.kind === 'error'
                    ? 'field-help--error'
                    : apiFeedback?.kind === 'success'
                      ? 'field-help--success'
                      : ''
                }`}
              >
                {apiFeedback?.text ??
                  'Credentials are verified with Twitch OAuth and stored locally without returning secrets.'}
              </p>

              <div className="flex items-center justify-between gap-2 flex-wrap pt-2 border-t border-rule">
                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    type="button"
                    disabled={!twitchStatus?.configured || oauthBusy !== null}
                    aria-busy={oauthBusy === 'viewer'}
                    onClick={() => handleLaunchOAuth('viewer')}
                    className="btn btn--accent-outline whitespace-nowrap"
                  >
                    <Link2 size={14} aria-hidden="true" />
                    <span>Connect Viewer OAuth</span>
                  </button>
                  <button
                    type="button"
                    disabled={!twitchStatus?.configured || oauthBusy !== null}
                    aria-busy={oauthBusy === 'broadcaster'}
                    onClick={() => handleLaunchOAuth('broadcaster')}
                    className="btn btn--quiet whitespace-nowrap"
                  >
                    <Link2 size={14} aria-hidden="true" />
                    <span>Connect Broadcaster</span>
                  </button>
                </div>

                <button
                  type="submit"
                  disabled={configBusy}
                  aria-busy={configBusy}
                  className="btn btn--primary"
                >
                  {configBusy && <span className="desk-spinner" aria-hidden="true" />}
                  <span>{configBusy ? 'Verifying' : 'Save Credentials'}</span>
                </button>
              </div>
              <p className="text-xs text-muted">
                Viewer enables clips:edit; Broadcaster adds clip downloads, VOD clips, EventSub Hype Train, and markers.
              </p>
            </form>
          </div>
        ) : (
          <div role="tabpanel" aria-labelledby="twitch-tab-clips" className="flex flex-col gap-4">
            <form onSubmit={handleFetchTopClips} className="flex flex-col gap-3">
              <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_auto] gap-3 items-end">
                <div>
                  <label htmlFor="top-clips-channel" className="block text-sm text-ink-2 mb-1">
                    Channel
                  </label>
                  {channels.length > 0 ? (
                    <select
                      id="top-clips-channel"
                      value={selectedLoginOption}
                      onChange={(e) => setSelectedLoginOption(e.target.value)}
                      disabled={fetchingClips}
                      className="field-select"
                    >
                      {channels.map((ch) => (
                        <option key={ch.id} value={ch.channel_login}>
                          {ch.display_name || ch.channel_login} (@{ch.channel_login})
                        </option>
                      ))}
                      <option value="__custom__">Other Twitch channel</option>
                    </select>
                  ) : (
                    <input
                      id="top-clips-channel"
                      type="text"
                      placeholder="shroud"
                      value={customLogin}
                      onChange={(e) => setCustomLogin(e.target.value)}
                      disabled={fetchingClips}
                      className="field-input"
                    />
                  )}
                </div>

                <div>
                  <span className="block text-sm text-ink-2 mb-1">Period</span>
                  <div className="grid grid-cols-3" role="group" aria-label="Clip time window">
                    {(['24h', '7d', '30d'] as const).map((p) => (
                      <button
                        key={p}
                        type="button"
                        aria-pressed={period === p}
                        onClick={() => setPeriod(p)}
                        className="seg-cell"
                      >
                        {p}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {(selectedLoginOption === '__custom__' || channels.length === 0) && channels.length > 0 && (
                <div>
                  <label htmlFor="top-clips-custom-login" className="block text-sm text-ink-2 mb-1">
                    Custom channel login or URL
                  </label>
                  <input
                    id="top-clips-custom-login"
                    type="text"
                    placeholder="caseoh_ or https://twitch.tv/caseoh_"
                    value={customLogin}
                    onChange={(e) => setCustomLogin(e.target.value)}
                    disabled={fetchingClips}
                    className="field-input"
                  />
                </div>
              )}

              <div className="flex items-center justify-between gap-3 flex-wrap">
                <label
                  className={`flex items-center gap-2 text-sm ${
                    broadcasterAllowed ? 'text-ink-2 cursor-pointer' : 'text-muted cursor-not-allowed opacity-55'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={preferPortrait}
                    disabled={!broadcasterAllowed}
                    onChange={(e) => setPreferPortrait(e.target.checked)}
                    className="w-4 h-4 accent-accent"
                  />
                  <span>
                    Prefer Portrait{broadcasterAllowed ? '' : ' (requires Broadcaster OAuth)'}
                  </span>
                </label>

                <button
                  type="submit"
                  disabled={fetchingClips || !twitchStatus?.configured}
                  aria-busy={fetchingClips}
                  className="btn btn--accent-outline whitespace-nowrap"
                >
                  {fetchingClips && <span className="desk-spinner" aria-hidden="true" />}
                  <span>{fetchingClips ? 'Fetching' : 'Fetch Clips'}</span>
                </button>
              </div>
            </form>

            {clipsError && (
              <p className="text-sm text-error" role="alert">
                {clipsError}
              </p>
            )}

            {importFeedback && (
              <p
                role="status"
                aria-live="polite"
                className={`text-sm ${importFeedback.kind === 'error' ? 'text-error' : 'text-success'}`}
              >
                {importFeedback.text}
              </p>
            )}

            <div className="border border-rule bg-paper max-h-64 overflow-y-auto">
              {fetchingClips ? (
                <div className="p-3 flex flex-col gap-2" aria-busy="true" aria-label="Loading top Twitch clips">
                  {[1, 2, 3].map((slot) => (
                    <div key={slot} className="h-14 bg-paper-2 border border-rule" />
                  ))}
                </div>
              ) : topClips.length === 0 ? (
                <div className="p-4 text-sm text-muted">
                  {clipsFetchedOnce
                    ? 'No clips returned for this channel and period.'
                    : twitchStatus?.configured
                      ? 'Choose a channel and click Fetch Clips to browse top moments.'
                      : 'Configure Twitch Client ID and Secret in API and OAuth first.'}
                </div>
              ) : (
                <ul className="list-none m-0 p-0 divide-y divide-rule">
                  {topClips.map((clip) => {
                    const busy = importingKey === clip.id
                    return (
                      <li key={clip.id} className="p-3 flex items-center gap-3">
                        <div className="w-24 h-14 bg-paper-2 border border-rule shrink-0 overflow-hidden flex items-center justify-center">
                          {clip.thumbnail_url ? (
                            <img
                              src={clip.thumbnail_url}
                              alt=""
                              width={96}
                              height={54}
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
                          <p className="text-sm font-medium text-ink truncate">{clip.title}</p>
                          <div className="flex items-center gap-3 text-xs text-muted mt-1 num">
                            <span>{clip.creator_name || clip.broadcaster_name || 'Twitch'}</span>
                            <span>{clip.view_count.toLocaleString()} views</span>
                            <span>{clip.duration.toFixed(1)}s</span>
                          </div>
                        </div>

                        <button
                          type="button"
                          disabled={importingKey !== null}
                          aria-busy={busy}
                          onClick={() => handleImportOne(clip.id, clip.url, targetLogin)}
                          className="btn btn--compact btn--accent-outline whitespace-nowrap shrink-0"
                        >
                          {busy ? (
                            <span className="desk-spinner" aria-hidden="true" />
                          ) : (
                            <Download size={14} aria-hidden="true" />
                          )}
                          <span>{busy ? 'Importing' : 'Import'}</span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>

            <form onSubmit={handleDirectClipSubmit} className="pt-2 border-t border-rule flex items-end gap-2">
              <div className="flex-1 min-w-0">
                <label htmlFor="direct-clip-url" className="block text-xs text-muted mb-1">
                  Or paste a Twitch clip URL directly
                </label>
                <input
                  id="direct-clip-url"
                  type="url"
                  placeholder="https://clips.twitch.tv/ClipSlug"
                  value={directClipUrl}
                  onChange={(e) => setDirectClipUrl(e.target.value)}
                  disabled={importingKey !== null}
                  className="field-input"
                />
              </div>
              <button
                type="submit"
                disabled={importingKey !== null}
                aria-busy={importingKey === 'direct-url'}
                className="btn btn--quiet whitespace-nowrap"
              >
                Import URL
              </button>
            </form>
          </div>
        )}

        <div className="flex items-center justify-end pt-2 border-t border-rule">
          <button type="button" onClick={onClose} className="btn btn--quiet">
            Close
          </button>
        </div>
      </div>
    </dialog>
  )
}
