'use client';
import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Mic, ShieldCheck, Trash2, AudioLines, RefreshCw, Users, CheckCircle2 } from 'lucide-react';
import { CardHeader } from './design-system/card-header';
import { Button } from './design-system/button';
import { message, pipelineRequest, time } from './api';
type Profile = { id: string; created_at: number; revoked: number; model: string };
type VoiceStatus = { employee_name: string; profiles: Profile[]; identification_state: string; sync_state?: string; corporate_profiles?: { id: string; name: string; employee_id: string }[] };
type Job = { id: string; state: string; operation: string; error_code: string | null; result: null | { profile_id?: string; speakers_detected: number; audio_deleted: boolean; quality?: { clean_speech_seconds: number }; comparisons?: { speaker_id: string; clean_seconds: number; similarity: number | null; reason: string }[]; turns?: { speaker_id: string; start_ms: number; end_ms: number }[] } };
const jobKey = 'synth_voice_onboarding_job';
const errors: Record<string, string> = { enrollment_requires_one_voice: 'Para crear tu perfil necesitamos una sola voz. Graba tú solo en un lugar tranquilo.', enrollment_low_quality: 'Necesitamos al menos 15 segundos de habla limpia, sin saturación. Acerca el micrófono y vuelve a grabar.', enrollment_inconsistent_speaker: 'Las muestras no son suficientemente consistentes. Repite hablando de forma natural.', voice_busy: 'Hay otra operación de voz en curso. Espera a que termine.', acoustic_worker_already_running: 'Se está procesando una reunión. Vuelve a intentarlo cuando termine.', no_active_profiles: 'Crea tu perfil de voz antes de probar la comparación.', speaker_inference_failed: 'No se pudo analizar esta muestra. Revisa el micrófono y vuelve a grabar.' };
export function VoiceOnboarding() {
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [consent, setConsent] = useState(false);
  const [phase, setPhase] = useState<'idle' | 'recording' | 'processing'>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [shareConsent, setShareConsent] = useState(false);
  const [formMode, setFormMode] = useState<'enroll' | 'test' | null>(null);
  const [shareEditor, setShareEditor] = useState(false);
  const mounted = useRef(true);
  const latch = useRef(false);
  const refresh = async () => { const s = await pipelineRequest<VoiceStatus>('GET', '/v1/voice'); if (mounted.current) setStatus(s); };
  useEffect(() => {
    mounted.current = true;
    void refresh().catch(e => { if (mounted.current) setError(message(e)); });
    const id = localStorage.getItem(jobKey); if (id) { setJob({ id, state: 'running', operation: '', error_code: null, result: null }); setPhase('processing'); }
    return () => { mounted.current = false; void invoke('synth_voice_cancel'); };
  }, []);
  useEffect(() => {
    if (phase !== 'recording') return;
    const start = Date.now(); const timer = setInterval(() => setElapsed(Math.min(30, Math.floor((Date.now() - start) / 1000))), 250);
    return () => clearInterval(timer);
  }, [phase]);
  useEffect(() => {
    if (!job?.id || phase !== 'processing') return;
    let active = true; let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await pipelineRequest<Job>('GET', `/v1/voice/jobs/${job!.id}`);
        if (!active) return; setJob(next);
        if (next.state !== 'running') { localStorage.removeItem(jobKey); setPhase('idle'); latch.current = false; if (next.error_code) setError(errors[next.error_code] || 'No se pudo completar el análisis. Vuelve a intentarlo.'); else { setFormMode(null); setConsent(false); } await refresh(); return; }
      } catch (e) { if (active) setError(message(e)); }
      if (active) timer = setTimeout(() => void poll(), 2000);
    }
    void poll(); return () => { active = false; clearTimeout(timer); };
  }, [job?.id, phase]);
  async function record(operation: 'enroll' | 'test') {
    if (latch.current || !consent || deleting) return;
    latch.current = true; setError(''); setJob(null); setElapsed(0); setPhase('recording');
    let recordingId: string | null = null; let submitted = false;
    try {
      recordingId = await invoke<string>('synth_voice_record', { consentConfirmed: true });
      if (!mounted.current) return;
      setPhase('processing');
      const next = await pipelineRequest<{ id: string }>('POST', `/v1/voice/${operation}`, { recording_id: recordingId, consent_confirmed: true });
      submitted = true; localStorage.setItem(jobKey, next.id);
      if (mounted.current) setJob({ ...next, state: 'running', operation, error_code: null, result: null });
    } catch (e) { if (mounted.current) { const m = message(e); setError(Object.entries(errors).find(([code]) => m.includes(code))?.[1] ?? m); setPhase('idle'); } }
    finally { if (recordingId && !submitted) await invoke('synth_voice_discard', { recordingId }).catch(() => { if (mounted.current) setError('No se pudo borrar la muestra. Revisa el servicio local.'); }); if (!submitted) latch.current = false; }
  }
  async function remove(id: string) {
    setDeleting(true); setError('');
    try { await pipelineRequest('POST', '/v1/voice/delete', { profile_id: id }); setConfirm(null); await refresh(); }
    catch (e) { setError(message(e)); } finally { setDeleting(false); }
  }
  async function share(id: string) {
    setSharing(true); setError('');
    try { await pipelineRequest('POST', '/v1/voice/share', { profile_id: id, consent_confirmed: shareConsent }); await refresh(); setShareEditor(false); setShareConsent(false); }
    catch (e) { setError(message(e)); } finally { setSharing(false); }
  }
  const busy = phase !== 'idle';
  const profiles = status?.profiles.filter(p => !p.revoked).sort((a, b) => b.created_at - a.created_at) ?? [];
  const synced = status?.sync_state === 'synced';
  const sharedIds = new Set(status?.corporate_profiles?.map(p => p.id) ?? []);
  const shared = synced && profiles.some(p => sharedIds.has(p.id));
  const latestShared = synced && !!profiles[0] && sharedIds.has(profiles[0].id);
  const showRecorder = !!status && (!profiles.length || formMode !== null || busy);
  const showShareForm = synced && (shareEditor || !shared);
  function openRecorder(mode: 'enroll' | 'test') { setFormMode(mode); setConsent(false); setJob(null); setError(''); }
  function profileRow(p: Profile) { return <div className="voice-profile-row" key={p.id}><div><strong>{status?.employee_name}</strong><p className="voice-notice">Creado el {new Date(p.created_at * 1000).toLocaleString('es-ES')}{synced && sharedIds.has(p.id) ? ' · Compartido' : ' · En este Mac'}</p></div><Button size="sm" variant="ghost" disabled={busy || deleting || sharing} onClick={() => setConfirm(p.id)}><Trash2 size={14} /> Borrar perfil</Button>{confirm === p.id && <div className="voice-profile-confirm"><p>Se borrará también del registro compartido. Las reuniones ya procesadas conservarán sus notas.</p><Button size="sm" variant="outline" loading={deleting} onClick={() => void remove(p.id)}>Confirmar borrado</Button><Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Volver</Button></div>}</div>; }
  return <div className="voice-library"><div className="voice-page-heading"><div><p className="voice-eyebrow">TU VOZ</p><h1>Perfil de voz</h1><p>Una muestra tuya para explorar quién habla, con el audio siempre en este Mac.</p></div><ShieldCheck size={24} /></div>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {!status && !error && <p role="status">Cargando tu perfil…</p>}
    {!!profiles.length && <section className="voice-processing-task"><CardHeader title="Tu perfil guardado" description="Cifrado en este Mac; su clave está protegida por Keychain." icon={ShieldCheck} actions={<div className="voice-profile-actions"><Button size="sm" variant="outline" disabled={busy || deleting || sharing} onClick={() => openRecorder('test')}><AudioLines size={14} /> Probar mi voz</Button><Button size="sm" disabled={busy || deleting || sharing} onClick={() => openRecorder('enroll')}><RefreshCw size={14} /> Grabar nueva muestra</Button></div>} />{profileRow(profiles[0])}{profiles.length > 1 && <details className="voice-older-profiles"><summary>Otras muestras guardadas ({profiles.length - 1})</summary>{profiles.slice(1).map(profileRow)}</details>}<p className="voice-notice">La identificación sigue en piloto. Grabar una nueva muestra conserva las anteriores.</p></section>}
    {showRecorder && <section className="voice-processing-task"><CardHeader title={formMode === 'test' ? 'Probar mi voz' : profiles.length ? 'Grabar una nueva muestra' : 'Crea tu perfil de voz'} description={status?.employee_name} icon={Mic} actions={profiles.length ? <Button size="xs" variant="ghost" disabled={busy} onClick={() => { setFormMode(null); setConsent(false); }}>Cerrar</Button> : undefined} /><p>Graba 30 segundos hablando de forma natural y sin otras voces. Usamos solo el micrófono predeterminado; el audio del ordenador queda fuera de esta muestra.</p><blockquote className="voice-enrollment-script">Estoy preparando mi perfil de voz para las notas de mi organización. Quiero poder repasar ideas, organizar decisiones y encontrar lo que hablamos en cada reunión. Hoy voy a explicar cómo trabajo, qué proyectos tengo en marcha y qué me gustaría mejorar durante las próximas semanas. También puedo añadir mis propias palabras y hablar con tranquilidad, a mi ritmo habitual.</blockquote><p className="voice-notice">El perfil se guarda cifrado localmente y puedes borrarlo. La muestra se borra al terminar el análisis, también si falla. La identificación automática sigue pendiente de calibración con grabaciones distintas.</p>
    <label className="voice-capture-consent"><input type="checkbox" checked={consent} disabled={busy} onChange={e => setConsent(e.target.checked)} /><span>Acepto grabar mi voz y crear o probar mi perfil local. Si participan otras personas en la prueba, han aceptado.</span></label>
    {phase === 'recording' ? <div className="voice-capture-controls"><strong role="status">Grabando muestra · {elapsed} / 30 s</strong><progress max={30} value={elapsed} aria-label="Duración de la muestra" /><Button variant="outline" onClick={() => void invoke('synth_voice_cancel')}>Cancelar grabación</Button></div> : <div className="flex flex-wrap gap-2"><Button disabled={!consent || busy || deleting} loading={phase === 'processing'} onClick={() => void record(formMode === 'test' ? 'test' : 'enroll')}><Mic size={15} /> {formMode === 'test' ? 'Grabar prueba' : profiles.length ? 'Grabar nueva muestra' : 'Grabar mi perfil'}</Button></div>}
    {phase === 'processing' && <p role="status">Analizando la muestra en este Mac. Puedes volver después: el resultado se conserva.</p>}</section>}
    {job?.result && job.operation === 'test' && <section className="voice-processing-task"><h2>Resultado de la prueba</h2><p>{job.result.speakers_detected} {job.result.speakers_detected === 1 ? 'voz detectada' : 'voces detectadas'} · Audio de la muestra eliminado.</p>{job.result.quality && <p>{job.result.quality.clean_speech_seconds.toFixed(1)} segundos de habla limpia.</p>}{job.result.comparisons && <><p className="voice-notice">Similitud acústica con tu perfil. Estos valores son exploratorios: todavía no confirman una identidad. Para probar diarización presencial, alternad dos voces sin hablar a la vez durante la muestra.</p><div className="voice-meeting-list">{job.result.comparisons.map(c => <div className="voice-result" key={c.speaker_id}><strong>Voz {c.speaker_id}</strong><p>{c.clean_seconds.toFixed(1)} s de habla · Similitud: {c.similarity === null ? 'No hay habla suficiente' : c.similarity.toFixed(3)}</p></div>)}</div></>}{job.result.turns && <details><summary>Ver turnos de voz</summary>{job.result.turns.map((t,i) => <p key={i}>{time(t.start_ms)}–{time(t.end_ms)} · Voz {t.speaker_id}</p>)}</details>}</section>}
    {!!profiles.length && <section className="voice-processing-task voice-corporate-profiles"><CardHeader title="Reconocimiento entre equipos" description={shared ? 'Tu perfil ya está disponible para los empleados autorizados del piloto.' : synced ? 'Comparte tu perfil para que los Macs del piloto puedan reconocer tu voz.' : 'No podemos comprobar el registro compartido en este momento.'} icon={Users} actions={shared ? <span className="voice-profile-shared-status" role="status"><CheckCircle2 size={18} /> Perfil compartido</span> : <Button size="xs" variant="ghost" disabled={sharing} onClick={() => void refresh().catch(e => setError(message(e)))}><RefreshCw size={14} /> Actualizar</Button>} />
      {shared && !latestShared && !shareEditor && <div className="voice-section-actions"><span className="voice-notice">Tu nueva muestra todavía está solo en este Mac.</span><Button size="sm" variant="outline" disabled={busy || deleting || sharing} onClick={() => { setShareEditor(true); setShareConsent(false); }}>Actualizar perfil compartido</Button></div>}
      {showShareForm && <><p>Solo se sincroniza el embedding de voz, cifrado en Supabase. El audio se borra tras el análisis y no se sube.</p><label className="voice-capture-consent"><input type="checkbox" checked={shareConsent} disabled={sharing} onChange={e => setShareConsent(e.target.checked)} /><span>Acepto compartir mi perfil de voz con los empleados autorizados de mi organización para este piloto.</span></label><div className="voice-section-actions"><span className="voice-notice">Registro conectado</span><div className="voice-profile-actions">{shareEditor && <Button size="sm" variant="ghost" disabled={sharing} onClick={() => setShareEditor(false)}>Cancelar</Button>}<Button size="sm" loading={sharing} disabled={!shareConsent || busy || deleting} onClick={() => void share(profiles[0].id)}>Compartir mi perfil</Button></div></div></>}
      {!synced && <p className="voice-notice">El perfil local sigue disponible. Vuelve a comprobar la conexión para confirmar si está compartido.</p>}
    </section>}
  </div>;
}
