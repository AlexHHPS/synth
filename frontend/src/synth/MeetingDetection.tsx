'use client';
import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Phone, Settings2, X, AudioLines } from 'lucide-react';
import { CardHeader } from './design-system/card-header';
import { Button } from './design-system/button';
import { message } from './api';
type Settings = { enabled: boolean; excluded_apps: string[] };
type Hint = { id: string; app_id: string; label: string };
type Status = { settings: Settings; available: boolean; error: string | null; pending: Hint[] };
export function MeetingDetection({ onPrepare }: { onPrepare: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState(false);
  const [excluded, setExcluded] = useState('');
  useEffect(() => {
    let active = true;
    const load = async () => { try { const s = await invoke<Status>('synth_detection_status'); if (active) setStatus(s); } catch (e) { if (active) setError(message(e)); } };
    void load(); const timer = setInterval(() => void load(), 2000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  async function action(hint: Hint, action: 'prepare' | 'dismiss' | 'snooze') {
    setBusy(true); setError('');
    try { await invoke('synth_detection_action', { id: hint.id, action }); setStatus(await invoke<Status>('synth_detection_status')); if (action === 'prepare') onPrepare(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function save(settings: Settings) {
    setBusy(true); setError('');
    try { await invoke('synth_detection_settings', { settings }); setStatus(await invoke<Status>('synth_detection_status')); setEdit(false); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  const enabled = status?.settings.enabled ?? false;
  const label = !status ? 'Comprobando' : !enabled ? 'Desactivada' : status.available ? 'Activa' : 'No disponible';
  return <section className="voice-call-detector" aria-label="Detección de llamadas">
    <div className="voice-detector-toolbar"><div className="voice-detector-status"><Phone size={14} aria-hidden="true" /><span>Detección de llamadas</span><span className="voice-status-dot" data-active={enabled && status?.available} /><small>{label}</small></div><Button variant="ghost" size="xs" aria-expanded={edit} aria-controls="voice-detector-settings" onClick={() => { setExcluded(status?.settings.excluded_apps.join('\n') ?? ''); setEdit(!edit); }}><Settings2 size={14} /> Ajustes</Button></div>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {status?.error && <div className="voice-alert" role="status">{status.error}</div>}
    {edit && status && <div id="voice-detector-settings" className="voice-detector-settings">
      <CardHeader title="Avisos de llamadas" description="Te avisamos cuando una aplicación empieza a usar audio. Tú decides cuándo grabar." icon={Phone} actions={<button type="button" className="voice-icon-button" aria-label="Cerrar ajustes" onClick={() => setEdit(false)}><X size={16} /></button>} />
      <div className="voice-settings-row"><div><strong>Detectar posibles llamadas</strong><p>Puedes crear una nota manualmente en cualquier momento.</p></div><button type="button" role="switch" aria-checked={enabled} aria-label="Detectar posibles llamadas" className="voice-switch" disabled={busy} onClick={() => void save({ ...status.settings, enabled: !enabled })}><span /></button></div>
      <label className="voice-capture-field" htmlFor="voice-excluded-apps">Aplicaciones excluidas<textarea id="voice-excluded-apps" className="voice-input" rows={3} value={excluded} onChange={e => setExcluded(e.target.value)} placeholder="Un identificador de aplicación por línea" /></label>
      <div className="voice-section-actions"><p className="voice-notice">Las llamadas sin actividad de audio pueden no detectarse.</p><Button size="sm" variant="outline" loading={busy} onClick={() => void save({ ...status.settings, excluded_apps: [...new Set(excluded.split('\n').map(s => s.trim()).filter(Boolean))] })}>Guardar ajustes</Button></div>
    </div>}
    {status?.pending.map(hint => <div className="voice-call-hint" key={hint.id} role="status"><CardHeader title={`¿Estás en una llamada con ${hint.label}?`} description="Crea una nota y comienza a grabar cuando estén todos listos." icon={AudioLines} actions={<button type="button" className="voice-icon-button" disabled={busy} aria-label="Descartar aviso" onClick={() => void action(hint, 'dismiss')}><X size={16} /></button>} /><div className="voice-call-actions"><Button size="sm" disabled={busy} onClick={() => void action(hint, 'prepare')}>Preparar nota</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void action(hint, 'snooze')}>Recordar en un minuto</Button><Button size="sm" variant="ghost" disabled={busy} onClick={() => void save({ ...status.settings, excluded_apps: [...status.settings.excluded_apps, hint.app_id] })}>Silenciar esta app</Button></div></div>)}
  </section>;
}
