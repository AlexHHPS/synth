'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import * as Dialog from '@radix-ui/react-dialog';
import { ArrowRight, ChevronDown, Cloud, FileVideo, HardDrive, Paperclip, Play, X } from 'lucide-react';
import { Button } from './design-system/button';
import { MeetingPanel } from './MeetingPanel';
import { DriveBrowser } from './DriveBrowser';
import { message, pipelineRequest, type Folder } from './api';
import './media-import.css';

interface SelectedMedia { id: string; name: string; video: boolean }
interface DriveState { state: string; error_code?: string }

const driveErrors: Record<string, string> = {
  drive_connection_required: 'Conecta Google Drive con la misma cuenta de Synth.',
  drive_permission_required: 'La cuenta de Google no concedió permiso de lectura.',
  drive_link_invalid: 'Introduce un enlace a un archivo de Google Drive.',
  drive_access_denied: 'Google Drive no permite descargar este archivo con la cuenta conectada.',
  drive_download_denied: 'Google Drive ha desactivado la descarga de este archivo.',
  drive_type_unsupported: 'El formato del archivo no coincide con el tipo seleccionado.',
  drive_file_too_large: 'El archivo supera el tamaño permitido.',
  drive_download_timeout: 'La descarga ha tardado demasiado. Inténtalo de nuevo.',
  auth_not_enabled: 'La conexión directa a Drive requiere iniciar sesión con Google en Synth.',
  auth_google_denied: 'Se ha cancelado la conexión a Google Drive.',
  auth_identity_changed: 'Conecta Google Drive con la misma cuenta de Synth.',
};

type MediaImportProps = {
  open: boolean;
  onClose: () => void;
} & (
  | { kind: 'attachments'; captureId: string }
  | { kind: 'video'; folders: Folder[]; initialFolderId: string; onQueued: () => void }
);

function videoTitle(name: string) {
  return name.replace(/\.[^.]+$/, '').trim() || 'Reunión importada';
}

export function LibraryVideoImport({ folders, folderId, onQueued }: {
  folders: Folder[]; folderId: string; onQueued: () => void;
}) {
  const [open, setOpen] = useState(false);
  const initialFolderId = folders.some(folder => folder.id === folderId && folder.can_edit) ? folderId : '';
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild>
      <Button size="sm" variant="outline"><FileVideo size={16} /> Importar vídeo</Button>
    </Dialog.Trigger>
    <MediaImport kind="video" folders={folders} initialFolderId={initialFolderId}
      open={open} onClose={() => setOpen(false)} onQueued={onQueued} />
  </Dialog.Root>;
}

export function MediaImport(props: MediaImportProps) {
  const { open, onClose } = props;
  const live = props.kind === 'attachments';
  const [title, setTitle] = useState('');
  const [notes, setNotes] = useState('');
  const [folderId, setFolderId] = useState(props.kind === 'video' ? props.initialFolderId : '');
  const [video, setVideo] = useState<SelectedMedia | null>(null);
  const [choosingSource, setChoosingSource] = useState(true);
  const [sourceDrive, setSourceDrive] = useState(false);
  const [attachmentDrive, setAttachmentDrive] = useState(false);
  const titleInput = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const [attachments, setAttachments] = useState<SelectedMedia[]>([]);
  const [drive, setDrive] = useState('disconnected');
  const driveExpired = useCallback(() => setDrive('disconnected'), []);
  const [consent, setConsent] = useState(false);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const receipt = useRef<{ key: string; id: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    let mounted = true;
    async function refreshConnection() {
      try {
        const result = await pipelineRequest<DriveState>('GET', '/v1/auth/drive');
        if (!mounted) return;
        setDrive(result.state);
        if (result.state === 'failed') setError(driveErrors[result.error_code ?? ''] ?? 'No se pudo conectar Google Drive.');
      } catch {
        if (mounted) setDrive('disconnected');
      }
    }
    void refreshConnection();
    if (drive !== 'waiting' && drive !== 'completing') return () => { mounted = false; };
    const timer = window.setInterval(() => void refreshConnection(), 2000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, [open, drive]);

  useEffect(() => {
    if (open && !choosingSource) titleInput.current?.focus();
  }, [open, choosingSource]);

  async function run(label: string, action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(label);
    setError('');
    try { await action(); }
    catch (error) {
      const code = message(error);
      if (code === 'drive_connection_required' || code === 'auth_identity_changed') driveExpired();
      setError(driveErrors[code] ?? code);
    } finally {
      inFlight.current = false;
      setPending('');
    }
  }

  async function select(file: SelectedMedia | null) {
    if (!file) return;
    if (props.kind === 'attachments') await invoke('synth_attach_capture_media', { id: file.id, captureId: props.captureId });
    if (file.video) {
      setTitle(current => !current.trim() || (video && current === videoTitle(video.name)) ? videoTitle(file.name) : current);
      setVideo(file);
      setConsent(false);
      setChoosingSource(false);
    } else {
      setAttachments(current => [...current, file]);
      setConsent(false);
    }
  }

  function pick(isVideo: boolean) {
    void run(isVideo ? 'Preparando vídeo…' : 'Preparando adjunto…', async () => {
      await select(await invoke<SelectedMedia | null>('synth_pick_media', { video: isVideo }));
    });
  }

  function download(link: string, isVideo: boolean) {
    void run('Descargando de Google Drive…', async () => {
      await select(await invoke<SelectedMedia>('synth_download_drive_media', { link, video: isVideo }));
    });
  }

  function connectDrive() {
    void run('Abriendo Google…', async () => {
      await pipelineRequest('POST', '/v1/auth/drive');
      setDrive('waiting');
    });
  }

  function changeSource() {
    setChoosingSource(true);
    setSourceDrive(false);
    setError('');
  }

  function queue() {
    if (props.kind !== 'video' || !video || !consent || !title.trim()) return;
    const { onQueued } = props;
    void run('Enviando al procesamiento…', async () => {
      const attachmentIds = attachments.map(item => item.id);
      const input = { videoId: video.id, attachmentIds, title: title.trim(), folderId: folderId || null, notes, consentConfirmed: true };
      const key = JSON.stringify(input);
      if (receipt.current?.key !== key) receipt.current = { key, id: crypto.randomUUID() };
      await invoke('synth_enqueue_video', { id: receipt.current.id, ...input });
      onQueued();
    });
  }

  const driveWaiting = drive === 'waiting' || drive === 'completing';
  const submitting = pending === 'Enviando al procesamiento…';
  const feedback = <>{error && <p className="voice-alert" role="alert">{error}</p>}{pending && <p className="media-import-status" role="status">{pending}</p>}</>;
  const drivePicker = (isVideo: boolean) => drive === 'connected'
    ? <DriveBrowser video={isVideo} disabled={!!pending || (!isVideo && attachments.length >= 6)}
        onSelect={link => download(link, isVideo)} onConnectionExpired={driveExpired} />
    : <div className="media-drive-connect">
        {drive === 'unavailable'
          ? <p>La conexión de Google Drive requiere iniciar sesión con Google en este despliegue.</p>
          : <Button variant="outline" disabled={!!pending || driveWaiting} onClick={connectDrive}>Conectar Google Drive</Button>}
        {driveWaiting && <p role="status">Completa el permiso de lectura en el navegador.</p>}
      </div>;

  const attachmentList = attachments.length > 0 && <ul className="media-attachment-list">
    {attachments.map(file => <li key={file.id}>
      <Paperclip size={15} aria-hidden />
      <span>{file.name}</span>
      {!live && <Button size="icon" variant="ghost" disabled={!!pending} aria-label={`Quitar ${file.name}`}
        onClick={() => { setAttachments(items => items.filter(item => item.id !== file.id)); setConsent(false); }}><X size={16} /></Button>}
    </li>)}
  </ul>;

  if (props.kind === 'attachments') {
    return <MeetingPanel id="capture-media" title="Adjuntos de la reunión" open={open} onClose={onClose}>
      <div className="meeting-voices-list">
        {feedback}
        <section aria-label="Adjuntos">
          {attachmentList}
          <Button size="sm" variant="outline" disabled={!!pending || attachments.length >= 6} onClick={() => pick(false)}>
            <Paperclip size={16} /> Elegir adjunto
          </Button>
          <p className="voice-notice">PDF, JPG, PNG, TXT, Markdown o CSV. Hasta 6 archivos de 20 MB.</p>
        </section>
        <section aria-label="Google Drive"><h3>Google Drive</h3>{drivePicker(false)}</section>
      </div>
    </MeetingPanel>;
  }

  const choosing = choosingSource || !video;
  return <Dialog.Portal>
    <Dialog.Overlay className="media-import-overlay bg-black/45" />
    <Dialog.Content className="media-import-dialog" data-slot="media-import-dialog"
      onOpenAutoFocus={event => { event.preventDefault(); heading.current?.focus(); }}
      onEscapeKeyDown={event => { if (submitting) event.preventDefault(); }}
      onPointerDownOutside={event => { if (submitting) event.preventDefault(); }}>
      <header className="media-import-header">
        <div className="media-import-mark"><FileVideo size={22} aria-hidden /></div>
        <div className="media-import-heading">
          <Dialog.Title ref={heading} tabIndex={-1}>Importar vídeo</Dialog.Title>
          <Dialog.Description>{choosing ? 'Elige el vídeo de la reunión.' : 'Revisa los datos del acta antes de procesar.'}</Dialog.Description>
        </div>
        <Dialog.Close asChild>
          <Button size="icon" variant="ghost" disabled={submitting} aria-label="Cerrar importación"><X size={20} /></Button>
        </Dialog.Close>
      </header>

      <div className="media-import-body">
        {feedback}
        {choosing ? <>
          <div className="media-source-options">
            <button type="button" className="media-source" disabled={!!pending} onClick={() => pick(true)}>
              <span className="media-source-icon"><HardDrive size={21} aria-hidden /></span>
              <strong>Archivo del Mac</strong>
              <ArrowRight size={17} aria-hidden />
            </button>
            <button type="button" className="media-source" aria-expanded={sourceDrive} aria-controls="media-video-drive" disabled={!!pending}
              onClick={() => { setSourceDrive(value => !value); setError(''); }}>
              <span className="media-source-icon"><Cloud size={21} aria-hidden /></span>
              <strong>Google Drive</strong>
              <ChevronDown size={17} aria-hidden />
            </button>
          </div>
          {sourceDrive && <div className="media-drive-panel" id="media-video-drive">{drivePicker(true)}</div>}
          <p className="media-import-hint">MP4, MOV, M4V, WebM o MKV con audio. Hasta 2 GB y 4 horas.</p>
          {video && <Button variant="ghost" disabled={!!pending} onClick={() => { setChoosingSource(false); setError(''); }}>
            Volver al vídeo seleccionado
          </Button>}
        </> : <>
          <div className="media-video-file">
            <span className="media-source-icon"><FileVideo size={21} aria-hidden /></span>
            <strong>{video.name}</strong>
            <div className="media-file-actions">
              <Button variant="ghost" disabled={!!pending}
                onClick={() => void run('Abriendo vídeo…', () => invoke('synth_preview_video', { id: video.id }))}>
                <Play size={16} /> Reproducir
              </Button>
              <Button variant="ghost" disabled={!!pending} onClick={changeSource}>Cambiar vídeo</Button>
            </div>
          </div>

          <div className="media-import-fields">
            <label className="media-import-field">
              <span>Título de la reunión</span>
              <input ref={titleInput} className="voice-input" value={title} maxLength={300} disabled={!!pending}
                onChange={event => setTitle(event.target.value)} />
            </label>
            <label className="media-import-field">
              <span>Carpeta</span>
              <select className="voice-input" value={folderId} disabled={!!pending} onChange={event => setFolderId(event.target.value)}>
                <option value="">Privada · Sin carpeta</option>
                {props.folders.filter(folder => folder.can_edit).map(folder => <option key={folder.id} value={folder.id}>{folder.name}</option>)}
              </select>
            </label>
          </div>

          <details className="media-import-context">
            <summary>
              <Paperclip size={17} aria-hidden />
              <strong>Notas y adjuntos</strong>
              {attachments.length > 0 && <span>{attachments.length} {attachments.length === 1 ? 'adjunto' : 'adjuntos'}</span>}
              <ChevronDown size={17} aria-hidden />
            </summary>
            <div className="media-context-body">
              <label className="media-import-field">
                <span>Notas opcionales</span>
                <textarea className="voice-input" rows={3} value={notes} maxLength={200000} disabled={!!pending}
                  onChange={event => setNotes(event.target.value)} />
              </label>
              {attachmentList}
              <div className="media-context-actions">
                <Button variant="outline" disabled={!!pending || attachments.length >= 6} onClick={() => pick(false)}>Elegir adjunto</Button>
                <Button variant="ghost" disabled={!!pending || attachments.length >= 6} aria-expanded={attachmentDrive}
                  onClick={() => { setAttachmentDrive(value => !value); setError(''); }}>Adjuntar desde Drive</Button>
              </div>
              {attachmentDrive && attachments.length < 6 && <div className="media-drive-panel">{drivePicker(false)}</div>}
              <p className="media-import-hint">PDF, JPG, PNG, TXT, Markdown o CSV. Hasta 6 archivos de 20 MB.</p>
            </div>
          </details>
        </>}
      </div>

      {!choosing && <footer className="media-import-footer">
        <label className="media-import-consent">
          <input type="checkbox" checked={consent} disabled={!!pending} onChange={event => setConsent(event.target.checked)} />
          <span>Confirmo que puedo procesar este vídeo y sus adjuntos.</span>
        </label>
        <Button variant="fancy" loading={submitting} disabled={!title.trim() || !consent || !!pending} onClick={queue}>
          Procesar y generar acta
        </Button>
      </footer>}
    </Dialog.Content>
  </Dialog.Portal>;
}
