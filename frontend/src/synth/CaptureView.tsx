'use client';

import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Field } from '@base-ui/react/field';
import { Mic, Monitor, Pause, Play, Square } from 'lucide-react';
import { useConfig } from '@/contexts/ConfigContext';
import { useRecordingState, RecordingStatus } from '@/contexts/RecordingStateContext';
import { useTranscripts } from '@/contexts/TranscriptContext';
import { recordingService } from '@/services/recordingService';
import { Button } from './design-system/button';
import { FormField } from './design-system/form-field';
import { Folder, message, pipelineRequest, time } from './api';
import { CAPTURE_MODE, CAPTURE_OPTIONS, handOffCapture, readCapture, isCaptureHandoffReady } from './capture';

interface Device { name: string; device_type: 'Input' | 'Output' }

export function CaptureView({ folders, onQueued }: { folders: Folder[]; onQueued: () => void }) {
  const pending = readCapture();
  const [title, setTitle] = useState(pending?.title ?? 'Nueva nota');
  const [folder, setFolder] = useState(pending?.folder_id ?? '');
  const [notes, setNotes] = useState(pending?.notes ?? '');
  const [consent, setConsent] = useState(false);
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const latch = useRef(false);
  const { selectedDevices, setSelectedDevices } = useConfig();
  const { clearTranscripts, setMeetingTitle, transcripts } = useTranscripts();
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
    if (options) localStorage.setItem(CAPTURE_OPTIONS, JSON.stringify({ ...options, notes }));
  }, [notes, recording.isRecording]);

  async function start() {
    if (latch.current || locked || !consent || !title.trim()) return;
    if (readCapture()?.folder_path) { setError('Procesa o revisa la captura anterior antes de empezar otra.'); return; }
    latch.current = true; setBusy(true); setError('');
    try {
      if (!isCaptureHandoffReady()) throw new Error('El cierre de captura todavía no está preparado. Espera un momento o reinicia la app.');
      await pipelineRequest('GET', '/v1/status');
      const preferences = await invoke<{ auto_save: boolean }>('get_recording_preferences');
      if (!preferences.auto_save) throw new Error('Activa el guardado de audio de la app para procesar esta reunión.');
      localStorage.setItem(CAPTURE_MODE, 'synth');
      localStorage.setItem(CAPTURE_OPTIONS, JSON.stringify({ title: title.trim(), folder_id: folder || null, notes, consent_confirmed: true }));
      clearTranscripts(); setMeetingTitle(title.trim());
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

  return <div className="voice-library voice-capture-page">
    <div className="voice-page-heading"><div><p className="voice-eyebrow">NOTAS Y CONVERSACIONES</p><h1>Nueva nota</h1><p>Habla para ti o graba una reunión. Toma tus notas mientras sucede.</p></div><Mic size={24} /></div>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    <div className="voice-capture-grid">
      <section className="voice-capture-settings"><FormField label="Título"><Field.Control className="voice-input" value={title} maxLength={300} disabled={locked} onChange={e => setTitle(e.target.value)} /></FormField>
        <details className="voice-capture-options"><summary>Carpeta y dispositivos de audio</summary><div>
        <label className="voice-capture-field">Guardar en<select className="voice-input" value={folder} disabled={locked} onChange={e => setFolder(e.target.value)}><option value="">Privada · Sin carpeta</option>{folders.filter(f => f.can_edit).map(f => <option key={f.id} value={f.id}>{f.name}</option>)}</select></label>
        <label className="voice-capture-field"><span><Mic size={14} /> Micrófono</span><select className="voice-input" disabled={locked} value={selectedDevices.micDevice ?? ''} onChange={e => setSelectedDevices({ ...selectedDevices, micDevice: e.target.value || null })}><option value="">Predeterminado del sistema</option>{devices.filter(d => d.device_type === 'Input').map(d => <option key={d.name} value={d.name}>{d.name}</option>)}</select></label>
        <label className="voice-capture-field"><span><Monitor size={14} /> Audio del ordenador</span><select className="voice-input" disabled={locked} value={selectedDevices.systemDevice ?? ''} onChange={e => setSelectedDevices({ ...selectedDevices, systemDevice: e.target.value || null })}><option value="">Predeterminado del sistema</option>{devices.filter(d => d.device_type === 'Output').map(d => <option key={d.name} value={d.name}>{d.name}</option>)}</select></label>
        </div></details>
        <p className="voice-notice">Privada por defecto. Se escucha el micrófono y el audio del ordenador, aunque hables tú solo. El audio queda en este Mac; el texto se envía a OmniRoute, que puede usar un proveedor externo.</p>
        {!recording.isRecording && <label className="voice-capture-consent"><input type="checkbox" checked={consent} disabled={locked} onChange={e => setConsent(e.target.checked)} /><span>Acepto grabar y procesar esta nota. Si participan otras personas, les he informado y han aceptado.</span></label>}
        <div className="voice-capture-controls">
          {recording.isRecording ? <><div className="voice-capture-clock" role="status">{recording.isPaused ? 'En pausa' : 'Grabando'} · {time((recording.recordingDuration ?? 0) * 1000)}</div><div className="flex gap-2"><Button variant="outline" disabled={busy || recording.isStopping} onClick={() => void control(recording.isPaused ? 'resume' : 'pause')}>{recording.isPaused ? <Play size={14} /> : <Pause size={14} />}{recording.isPaused ? 'Reanudar' : 'Pausar'}</Button><Button loading={busy} disabled={recording.isStopping} onClick={() => void control('stop')}><Square size={14} /> Finalizar y generar notas</Button></div></> : <Button loading={busy || recording.isStartingRecording} disabled={locked || !consent || !title.trim() || !!pending?.folder_path} onClick={() => void start()}><Mic size={15} /> Empezar grabación</Button>}
          {recording.statusMessage && <p role="status" className="voice-notice">{recording.statusMessage}</p>}
          {!recording.isRecording && pending?.folder_path && <Button variant="outline" loading={busy} onClick={() => void control('retry')}>Reintentar envío al procesamiento</Button>}
        </div>
      </section>
      <section className="voice-capture-notes"><FormField label="Mis notas" hint="Acompañan a la transcripción al generar las notas."><Field.Control render={<textarea />} className="voice-textarea" value={notes} onChange={e => setNotes(e.target.value)} maxLength={200000} /></FormField>
        {(recording.isRecording || transcripts.length > 0) && <section className="voice-live-transcript" aria-label="Transcripción en directo"><h2>Transcripción en directo</h2><p className="voice-notice">Se actualiza mientras hablas. La transcripción final se comprueba al terminar.</p><div className="voice-live-segments" role="log" aria-live="polite" aria-relevant="additions text">{transcripts.length ? transcripts.map((segment, index) => <article key={`${segment.id}-${index}`}><time>{time((segment.audio_start_time ?? 0) * 1000)}</time><p>{segment.text}</p></article>) : <p className="voice-notice">Escuchando… El texto aparecerá cuando haya habla.</p>}</div></section>}
        <p className="voice-notice">El audio temporal se conserva como máximo 24 horas. Los nombres de los hablantes requieren completar el onboarding y la calibración de voz.</p></section>
    </div>
  </div>;
}
