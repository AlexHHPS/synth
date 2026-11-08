'use client';

import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import dynamic from 'next/dynamic';
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover';
import { AudioLines, CalendarDays, ChevronDown, FolderPlus, Mic, Monitor, Pause, Play, Settings2, Square, Users } from 'lucide-react';
import { useConfig } from '@/contexts/ConfigContext';
import { useRecordingState, RecordingStatus } from '@/contexts/RecordingStateContext';
import { useLiveCapture } from './useLiveCapture';
import { useLiveVoices } from './useLiveVoices';
import { LiveVoices, voiceForSegment } from './LiveVoices';
import { recordingService } from '@/services/recordingService';
import { Button } from './design-system/button';
import { MeetingPanel } from './MeetingPanel';
const MeetingNotes = dynamic(() => import('./MeetingNotes'), { ssr: false });
import { Folder, message, pipelineRequest, time } from './api';
import { CAPTURE_MODE, writeCapture, handOffCapture, readCapture, isCaptureHandoffReady } from './capture';
import type { CalendarPreparation } from './CalendarHome';

interface Device { name: string; device_type: 'Input' | 'Output' }

async function prepareContext(eventId: string | undefined, folderId: string) {
  let timer: ReturnType<typeof setTimeout>;
  const operation = (async () => {
    await pipelineRequest('POST', '/v1/capture/context', { calendar_event_id: eventId ?? null, folder_id: folderId || null });
  })();
  await Promise.race([operation.catch(() => undefined), new Promise<void>(resolve => { timer = setTimeout(resolve, 2000); })]);
  clearTimeout(timer!);
}

export function CaptureView({ folders, onQueued, prepared }: { folders: Folder[]; onQueued: () => void; prepared?: CalendarPreparation | null }) {
  const pending = readCapture();
  const [title, setTitle] = useState(pending?.title ?? prepared?.event.title ?? 'Nueva nota');
  const [folder, setFolder] = useState(pending?.folder_id ?? prepared?.folder_id ?? '');
  const calendarEventId = pending?.calendar_event_id ?? (pending ? undefined : prepared?.event_id);
  const [notes, setNotes] = useState(pending?.notes ?? '');
  const [consent, setConsent] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const latch = useRef(false);
  const [panel, setPanel] = useState<'transcript' | 'voices' | null>(null);
  const [audioOptions, setAudioOptions] = useState(false);
  const { selectedDevices, setSelectedDevices } = useConfig();
  const { clear: clearTranscripts, transcripts, captureId, error: liveError, waiting: waitingForAudio } = useLiveCapture();
  const voices = useLiveVoices(captureId);
  const recording = useRecordingState();
  const locked = busy || recording.isRecording || recording.isStartingRecording || recording.isStopping || recording.isSaving;

  useEffect(() => {
    let active = true;
    void invoke<Device[]>('get_audio_devices').then(result => { if (active) setDevices(result); })
      .catch(() => { if (active) setError('No se pudieron consultar los dispositivos de audio. Revisa sus permisos.'); });
    const queued = () => onQueued();
    window.addEventListener('synth-capture-queued', queued);
    return () => { active = false; window.removeEventListener('synth-capture-queued', queued); };
  }, [onQueued]);

  useEffect(() => {
    if (!recording.isRecording) return;
    const options = readCapture();
    if (options) writeCapture({ ...options, notes });
  }, [notes, recording.isRecording]);

  async function start() {
    if (latch.current || locked || !consent || !title.trim()) return;
    if (readCapture()?.folder_path) { setError('Procesa o revisa la captura anterior antes de empezar otra.'); return; }
    latch.current = true; setBusy(true); setError('');
    try {
      if (!isCaptureHandoffReady()) throw new Error('El cierre de captura todavía no está preparado. Espera un momento o reinicia la app.');
      const status=await pipelineRequest<{audio_setup?:{state:string}}>('GET', '/v1/status');
      if(status.audio_setup?.state!=='ready')throw new Error('Espera a que termine la preparación automática del audio.');
      const preferences = await invoke<{ auto_save: boolean }>('get_recording_preferences');
      if (!preferences.auto_save) throw new Error('Activa el guardado de audio de la app para procesar esta reunión.');
      localStorage.setItem(CAPTURE_MODE, 'synth');
      // Optional network context has a short budget; recording is always local.
      await prepareContext(calendarEventId, folder);
      writeCapture({ title: title.trim(), folder_id: folder || null, notes, consent_confirmed: true, calendar_event_id: calendarEventId });
      clearTranscripts();
      recording.setStatus(RecordingStatus.STARTING, 'Preparando micrófono y audio del ordenador…');
      await recordingService.startRecordingWithDevices(selectedDevices.micDevice, selectedDevices.systemDevice, title.trim());
    } catch (e) { setError(message(e)); recording.setStatus(RecordingStatus.ERROR, message(e)); }
    finally { latch.current = false; setBusy(false); }
  }

  async function control(name: 'stop' | 'pause' | 'resume' | 'retry') {
    if (latch.current) return;
    latch.current = true; setBusy(true); setError('');
    try {
      if (name === 'stop') { recording.setStatus(RecordingStatus.STOPPING, 'Cerrando y comprobando el audio…'); await recordingService.stopRecording(''); }
      else if (name === 'pause') await recordingService.pauseRecording();
      else if (name === 'resume') await recordingService.resumeRecording();
      else await handOffCapture();
    } catch (e) { setError(message(e)); recording.setStatus(RecordingStatus.ERROR, message(e)); }
    finally { latch.current = false; setBusy(false); }
  }

  const elapsed = time((recording.recordingDuration ?? 0) * 1000);
  const toggle = (next: 'transcript' | 'voices') => setPanel(before => before === next ? null : next);
  const currentFolder = folders.find(item => item.id === folder);
  return <div className="meeting-editor voice-capture-page" data-slot="meeting-editor">
    {(error || liveError) && <div className="voice-alert" role="alert">{error || liveError}</div>}
    <header className="meeting-document-head">
      <input className="meeting-title-input" aria-label="Título de la nota" value={title} maxLength={300} disabled={locked} onChange={event => setTitle(event.target.value)} />
      <div className="meeting-metadata">
        <span className="meeting-chip"><CalendarDays size={17} /> Hoy</span>
        <button type="button" className="meeting-chip" aria-label="Voces" aria-controls="capture-voices" aria-expanded={panel === 'voices'} onClick={() => toggle('voices')}><Users size={17} /> Voces</button>
        <Popover><PopoverTrigger asChild><button type="button" className="meeting-chip" disabled={locked}><FolderPlus size={17} />{currentFolder?.name ?? 'Añadir a carpeta'}</button></PopoverTrigger>
          <PopoverContent className="meeting-popover" align="start"><label>Guardar en<select className="voice-input" aria-label="Carpeta de la nota" value={folder} disabled={locked} onChange={event => setFolder(event.target.value)}>
            <option value="">Privada · Sin carpeta</option>{folders.filter(item => item.can_edit).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label></PopoverContent></Popover>
      </div>
      {prepared && calendarEventId && (prepared.folder_id ?? '') === folder && <details className="calendar-capture-context"><summary>Briefing · {prepared.expected_attendees.length} invitados · {prepared.glossary.length} términos</summary>
        <p>{prepared.event.description || 'Sin agenda en la invitación.'}</p><p>{prepared.expected_attendees.map(a => a.name || a.email).join(', ')}</p>
      </details>}
    </header>
    <MeetingNotes value={notes} onChange={setNotes} />
    <div className="meeting-bottom" data-slot="meeting-controls">
      <div className="meeting-bottom-status" role="status">{waitingForAudio ? 'Grabando · transcripción en espera' : recording.statusMessage}</div>
      <div className="meeting-dock">
        <div className="meeting-record-pill">
          {recording.isRecording ? <>
            <button type="button" className="meeting-record-status" aria-controls="capture-transcript" aria-expanded={panel === 'transcript'} onClick={() => toggle('transcript')}><AudioLines size={23} /><span>{recording.isPaused ? 'En pausa' : elapsed}</span></button>
            <Button variant="ghost" disabled={busy || recording.isStopping} aria-label={recording.isPaused ? 'Reanudar grabación' : 'Pausar grabación'} onClick={() => void control(recording.isPaused ? 'resume' : 'pause')}>{recording.isPaused ? <Play size={17} /> : <Pause size={17} />}</Button>
            <Button variant="ghost" loading={busy} disabled={recording.isStopping} aria-label="Finalizar y generar acta" onClick={() => void control('stop')}><Square size={17} /></Button>
          </> : <Button loading={busy || recording.isStartingRecording} disabled={locked || !!pending?.folder_path} onClick={() => { if (!consent) setAudioOptions(true); else void start(); }}><Mic size={18} /> Grabar</Button>}
          <Popover open={audioOptions} onOpenChange={setAudioOptions}><PopoverTrigger asChild><Button variant="ghost" aria-label="Configurar grabación"><ChevronDown size={17} /></Button></PopoverTrigger>
            <PopoverContent className="meeting-popover meeting-audio-options" side="top" align="start">
              <h2><Settings2 size={17} /> Grabación</h2>
              <label><span><Mic size={15} /> Micrófono</span><select className="voice-input" disabled={locked} value={selectedDevices.micDevice ?? ''} onChange={event => setSelectedDevices({ ...selectedDevices, micDevice: event.target.value || null })}><option value="">Predeterminado del sistema</option>{devices.filter(device => device.device_type === 'Input').map(device => <option key={device.name} value={device.name}>{device.name}</option>)}</select></label>
              <label><span><Monitor size={15} /> Audio del ordenador</span><select className="voice-input" disabled={locked} value={selectedDevices.systemDevice ?? ''} onChange={event => setSelectedDevices({ ...selectedDevices, systemDevice: event.target.value || null })}><option value="">Predeterminado del sistema</option>{devices.filter(device => device.device_type === 'Output').map(device => <option key={device.name} value={device.name}>{device.name}</option>)}</select></label>
              <p>Audio en este Mac. Texto y notas en el servicio de tu organización.</p>
              {!recording.isRecording && <><label className="meeting-consent"><input type="checkbox" checked={consent} disabled={locked} onChange={event => setConsent(event.target.checked)} /><span>Acepto grabar y procesar esta nota. Las personas que participan han sido informadas y han aceptado.</span></label>
                <Button disabled={locked || !consent || !title.trim() || !!pending?.folder_path} loading={busy} onClick={() => { setAudioOptions(false); void start(); }}>Empezar grabación</Button></>}
            </PopoverContent></Popover>
        </div>
        <div className="meeting-capture-panels"><button type="button" className="meeting-chip" aria-label="Transcripción" aria-controls="capture-transcript" aria-expanded={panel === 'transcript'} onClick={() => toggle('transcript')}><AudioLines size={18} /><span>Transcripción</span></button><button type="button" className="meeting-chip" aria-label="Voces" aria-controls="capture-voices" aria-expanded={panel === 'voices'} onClick={() => toggle('voices')}><Users size={18} /><span>Voces</span></button></div>
        <div className="meeting-ask-disabled"><span>Pregunta sobre esta reunión al terminar</span></div>
      </div>
      {!recording.isRecording && pending?.folder_path && <Button variant="outline" loading={busy} onClick={() => void control('retry')}>Reintentar envío al procesamiento</Button>}
    </div>
    <MeetingPanel id="capture-transcript" title="Transcripción en directo" open={panel === 'transcript'} onClose={() => setPanel(null)} footer={waitingForAudio ? 'Transcripción en espera' : recording.isRecording ? `Grabando · ${elapsed}` : 'Transcripción'}>
      <div className="meeting-transcript" role="log" aria-live="polite" aria-relevant="additions text">{transcripts.length ? transcripts.map(segment => {
        const voice = voiceForSegment(segment, voices.status?.matches ?? []);
        return <article key={segment.id} className={`meeting-transcript-turn ${segment.source === 'microphone' ? 'is-microphone' : ''}`}><header><strong>{voice?.candidate_name ? `${voice.candidate_name}${voice.confirmed ? '' : ' · sugerida'}` : segment.source === 'microphone' ? 'Micrófono' : 'Ordenador'}</strong><time>{time((segment.audio_start_time ?? 0)*1000)}</time></header><p>{segment.text}</p></article>;
      }) : <p className="meeting-empty">{waitingForAudio ? 'El audio se está guardando. El texto aparecerá cuando el motor quede libre.' : recording.isRecording ? 'Escuchando…' : 'La transcripción aparecerá al comenzar la grabación.'}</p>}</div>
    </MeetingPanel>
    <MeetingPanel id="capture-voices" title="Voces" open={panel === 'voices'} onClose={() => setPanel(null)} footer="Las sugerencias se confirman contigo">
      <LiveVoices {...voices} transcripts={transcripts} expectedParticipants={prepared?.event_id === calendarEventId ? prepared?.expected_attendees.filter(a => a.response !== 'declined').map(a => a.principal_id).filter((id): id is string => !!id) : []} />
    </MeetingPanel>
  </div>;
}
