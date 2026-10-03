'use client';

import { useEffect, useState } from 'react';
import { Activity, ArrowUpRight, CircleCheck, RefreshCw, FileAudio, CalendarDays, FolderClosed, LoaderCircle } from 'lucide-react';
import { Button } from './design-system/button';
import { CaptureTask, Folder, Meeting, Page, message, pipelineRequest } from './api';

const states: Record<string, string> = {
  queued: 'En cola', running: 'Procesando', cancel_requested: 'Deteniendo',
  cancelled: 'Cancelado', failed: 'Requiere atención', succeeded: 'Acta disponible', unavailable: 'Estado por comprobar',
};
const stages: Record<string, string> = {
  queued: 'Esperando turno', normalized: 'Audio preparado', asr: 'Transcripción completada',
  diarization: 'Voces separadas', canonical: 'Transcripción preparada', identified: 'Reconocimiento de voces completado', uploaded: 'Generación del acta',
};
const errors: Record<string, string> = {
  api_unavailable: 'No se puede conectar con el servicio de reuniones. Comprueba que está iniciado.',
  document_job_failed: 'No se pudo generar el acta. Puedes reintentar sin volver a transcribir.',
  document_wait_timeout: 'La generación del acta tarda más de lo esperado. Puedes reintentar para consultar el resultado.',
  asr_failed: 'No se pudo transcribir el audio. Revisa la grabación antes de reintentar.',
  batch_transcription_repetitive: 'La retranscripción contiene demasiadas repeticiones y no hay una copia en directo válida. No se ha publicado como transcripción definitiva.',
  capture_file_changed: 'La grabación ha cambiado desde que se guardó. No se ha enviado al procesamiento.',
  capture_file_unavailable: 'La grabación ya no está disponible. Puede haber vencido su conservación temporal.',
  speaker_inference_failed: 'No se pudo separar las voces de la grabación.',
  document_invalid_citation: 'El acta no pasó la comprobación de sus referencias. La transcripción se conserva; puedes reintentar la versión actual.',
  document_item_schema: 'El modelo no devolvió el formato esperado. La transcripción se conserva.',
  current_document_job_missing: 'No se ha encontrado un análisis para la transcripción actual. Abre la reunión para comprobarla.',
};

export function OperationsView({ refreshToken, onOpen, folders = [], meetings = [] }: { refreshToken: number; onOpen: (id: string) => void; folders?: Folder[]; meetings?: Meeting[] }) {
  const [tasks, setTasks] = useState<CaptureTask[]>([]);
  const [worker, setWorker] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const [status, page] = await Promise.all([
          pipelineRequest<{ worker: { state: string } }>('GET', '/v1/status'),
          pipelineRequest<Page<CaptureTask>>('GET', '/v1/tasks'),
        ]);
        if (!active) return;
        setWorker(status.worker.state); setTasks(page.items); setError('');
      } catch (e) { if (active) setError(message(e)); }
      finally {
        if (active) { setLoading(false); timer = setTimeout(() => void load(), 3000); }
      }
    }
    void load();
    return () => { active = false; clearTimeout(timer); };
  }, [refreshToken, reload]);

  async function action(task: CaptureTask, name: 'retry' | 'cancel') {
    setBusy(task.id); setError('');
    try {
      await pipelineRequest('POST', `/v1/tasks/${task.id}/${name}`);
      setConfirm(null); setReload(old => old + 1);
    } catch (e) { setError(message(e)); } finally { setBusy(null); }
  }

  const activeStates = ['queued', 'running', 'cancel_requested'];
  const ordered = [...tasks].sort((a, b) => Number(activeStates.includes(b.state)) - Number(activeStates.includes(a.state)) || b.created_at - a.created_at);
  const counts = { active: tasks.filter(t => activeStates.includes(t.state)).length, ready: tasks.filter(t => t.state === 'succeeded').length, failed: tasks.filter(t => t.state === 'failed').length };
  return <div className="voice-library">
    <div className="voice-page-heading"><div><p className="voice-eyebrow">TU SISTEMA</p><h1>Procesamiento</h1><p>Consulta el progreso y recupera conversaciones interrumpidas.</p></div><Activity size={22} /></div>
    <div className="voice-system-summary"><div><strong>Audio en este Mac</strong><p>Transcripción y separación de voces locales. El acta usa OmniRoute y su proveedor configurado.</p></div><span className="voice-state">{worker === 'idle' ? 'Disponible' : worker === 'running' ? 'Trabajando' : worker ? 'Recuperando servicio' : 'Conectando…'}</span></div>
    <div className="voice-processing-counts"><span><LoaderCircle size={14} /> {counts.active} en curso</span><span><CircleCheck size={14} /> {counts.ready} disponibles</span>{counts.failed > 0 && <span>{counts.failed} requieren atención</span>}</div>
    <details className="voice-processing-info"><summary>Audio y perfiles de voz</summary><p className="voice-notice">Si has creado tu perfil, las transcripciones muestran coincidencias locales de piloto. La identificación de empleados requiere completar la calibración. El audio temporal se conserva como máximo 24 horas.</p></details>
    {error && <div role="alert" className="voice-alert">{error}</div>}
    {loading && <p role="status">Consultando el procesamiento…</p>}
    {!loading && !tasks.length && !error && <div className="voice-empty"><CircleCheck size={26} /><h2>No hay trabajos pendientes</h2><p>Las grabaciones aparecerán aquí cuando se envíen a procesar.</p></div>}
    <div className="voice-processing-list">{ordered.map(task => {
      const meeting = meetings.find(m => m.id === task.meeting_id);
      const title = meeting?.title || task.title || 'Nota sin título';
      const folderName = folders.find(f => f.id === (meeting?.folder_id ?? task.folder_id))?.name;
      const date = new Date(task.created_at * 1000);
      return <article key={task.id} className="voice-processing-task" data-state={task.state}>
      <div className="voice-processing-heading"><div className="voice-processing-identity"><div className="voice-row-icon"><FileAudio size={19} /></div><div><h2>{title}</h2><div className="voice-processing-meta"><span><CalendarDays size={13} /><time dateTime={date.toISOString()}>{date.toLocaleDateString('es-ES', { day: 'numeric', month: 'short', year: 'numeric' })} · {date.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })}</time></span><span><FolderClosed size={13} />{folderName || 'Privada · Sin carpeta'}</span></div></div></div><span className="voice-state" data-state={task.state}>{states[task.state] ?? 'Estado desconocido'}</span></div>
      <p>{task.state === 'succeeded' ? 'Transcripción y acta guardadas en tu biblioteca.' : stages[task.stage] ?? 'Consultando etapa'}{task.transcript_version ? ` · Transcripción v${task.transcript_version}` : ''} · Intento {task.attempts}</p>
      {task.error_code && <p className="voice-alert">{errors[task.error_code] ?? 'El procesamiento se ha detenido. Comprueba el servicio y reintenta cuando esté disponible.'}</p>}
      <div className="flex flex-wrap gap-2">
        {task.meeting_id && <Button size="sm" variant="outline" onClick={() => onOpen(task.meeting_id!)}>Abrir reunión <ArrowUpRight size={14} /></Button>}
        {task.state === 'failed' && <Button size="sm" variant="outline" loading={busy === task.id} disabled={busy !== null} onClick={() => void action(task, 'retry')}><RefreshCw size={14} /> Reintentar</Button>}
        {['queued', 'running'].includes(task.state) && <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setConfirm(task.id)}>Detener</Button>}
      </div>
      {confirm === task.id && <div className="voice-processing-confirm"><p>¿Detener el procesamiento pendiente? Las actas ya generadas se conservan.</p><div className="flex gap-2"><Button size="sm" variant="outline" loading={busy === task.id} onClick={() => void action(task, 'cancel')}>Confirmar</Button><Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setConfirm(null)}>Volver</Button></div></div>}
    </article>; })}</div>
  </div>;
}
