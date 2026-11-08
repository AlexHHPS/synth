'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, Clock3, FileText, RefreshCw, Users } from 'lucide-react';
import { Button } from './design-system/button';
import { Folder, Meeting, message, pipelineRequest, request } from './api';
import { MeetingPanel } from './MeetingPanel';
import './calendar.css';

export interface CalendarEvent {
  id: string; folder_id: string | null; content_hash: string;
  content: { title: string; description: string; start: string; end: string; all_day: boolean;
    attendees: { name: string; email: string; response: string }[]; attendees_omitted?: boolean; location?: string };
}
interface CalendarState {
  configured: boolean; connected: boolean; email: string | null; synced_at: string | null; items: CalendarEvent[];
}
export interface CalendarPreparation {
  event_id: string; event_hash: string; event: CalendarEvent['content']; folder_id: string | null; folder: string | null;
  fingerprint: string; expected_attendees: { name: string; email: string; principal_id: string | null; profile_available: boolean; response: string }[];
  glossary: { id: string; term: string; definition: string }[];
  passages: { evidence_id: string; meeting_id: string; transcript_version: number; turn_id: string; meeting_title: string; text: string; time: string }[];
}

function calendarError(error: unknown) {
  const code = message(error);
  if (code.includes('calendar_client_configuration_invalid')) return 'La conexión necesita el secreto original del cliente de Google. No necesitas volver a autorizar todavía.';
  if (code.includes('calendar_api_not_enabled')) return 'Falta habilitar Google Calendar API en el proyecto de Google configurado para esta app.';
  if (code.includes('calendar_reconnect_required') || code.includes('calendar_read_permission_required')) return 'Vuelve a conectar Google y acepta el permiso de lectura del calendario.';
  if (code.includes('calendar_google_unavailable')) return 'Google Calendar no responde. Vuelve a sincronizar en unos minutos.';
  return code;
}

export function useCalendar(refreshToken = 0) {
  const [state, setState] = useState<CalendarState | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const epoch = useRef(0), mounted = useRef(true);
  const refresh = useCallback(async (sync = false, force = false) => {
    const ticket = ++epoch.current;
    setBusy(true);
    try {
      const current = await request<CalendarState>('GET', '/v1/calendar');
      if (mounted.current && ticket === epoch.current) setState(current);
      if (sync && current.connected) await request('POST', '/v1/calendar/sync', { force });
      const next = sync && current.connected ? await request<CalendarState>('GET', '/v1/calendar') : current;
      if (mounted.current && ticket === epoch.current) { setState(next); setError(''); }
    } catch (e) { if (mounted.current && ticket === epoch.current) setError(calendarError(e)); }
    finally { if (mounted.current && ticket === epoch.current) setBusy(false); }
  }, []);
  useEffect(() => {
    mounted.current = true; void refresh(true);
    const timer = setInterval(() => { if (!document.hidden) void refresh(true); }, 300000);
    return () => { mounted.current = false; epoch.current++; clearInterval(timer); };
  }, [refresh, refreshToken]);
  useEffect(() => {
    if (!connecting) return;
    const deadline = Date.now()+300000;
    let active = true, timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if(Date.now()>deadline) { setConnecting(false); setError('La autorización de Google ha caducado. Vuelve a conectar.'); return; }
      try {
        const flow = await pipelineRequest<{ flow_state?: string; error_code?: string }>('GET', '/v1/auth/state');
        if (!active) return;
        if (flow.flow_state === 'failed') { setError(flow.error_code?.startsWith('calendar_') ? calendarError(flow.error_code) : 'No se pudo conectar Google Calendar. Vuelve a intentarlo y acepta el permiso de lectura.'); setConnecting(false); return; }
        if (flow.flow_state === 'signed_in') { setConnecting(false); await refresh(true); return; }
      } catch { if (active) { setError('No se pudo comprobar la conexión con Google.'); setConnecting(false); return; } }
      if (active) timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [connecting, refresh]);
  async function connect() {
    if (connecting) return;
    setError('');
    try { await pipelineRequest('POST', '/v1/auth/calendar'); if (mounted.current) setConnecting(true); }
    catch (e) { if (mounted.current) setError(message(e)); }
  }
  async function disconnect() {
    try { await request('DELETE', '/v1/calendar/connection'); await refresh(); }
    catch (e) { if (mounted.current) setError(message(e)); }
  }
  return { state, error, busy, connecting, refresh, connect, disconnect };
}

export function GoogleCalendarConnection({ refreshToken = 0 }: { refreshToken?: number }) {
  const calendar = useCalendar(refreshToken);
  const [confirm, setConfirm] = useState(false);
  return <section className="calendar-connection" data-slot="calendar-connection" aria-label="Google Calendar">
    <CalendarDays size={24} /><div><h2>Google Calendar</h2><p>{calendar.state?.connected ? calendar.state.email : 'Prepara tus reuniones con el calendario y el contexto de tu biblioteca.'}</p>
      {calendar.state?.connected && <small>Solo lectura · {calendar.state.synced_at ? `Sincronizado ${new Date(calendar.state.synced_at).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })}` : 'Pendiente de sincronizar'}</small>}
      {calendar.error && <p className="calendar-error" role="alert">{calendar.error}</p>}
      {calendar.state && !calendar.state.configured && <p role="status">La conexión con Google está pendiente de configuración.</p>}
    </div><div className="calendar-connection-actions">{calendar.state?.connected ? <>
      <Button variant="outline" loading={calendar.busy} onClick={() => void calendar.refresh(true, true)}><RefreshCw size={16} /> Sincronizar</Button>
      {calendar.error && <Button variant="outline" loading={calendar.connecting} onClick={() => void calendar.connect()}>Volver a conectar Google</Button>}
      {confirm ? <><Button variant="destructive" onClick={() => void calendar.disconnect().then(() => setConfirm(false))}>Desconectar calendario</Button><Button variant="ghost" onClick={() => setConfirm(false)}>Cancelar</Button></> : <Button variant="ghost" onClick={() => setConfirm(true)}>Desconectar</Button>}
    </> : <Button loading={calendar.connecting || calendar.busy} disabled={!calendar.state?.configured} onClick={() => void calendar.connect()}>{calendar.connecting ? 'Esperando a Google…' : 'Conectar Google'}</Button>}</div>
  </section>;
}

function localDay(value: string) { return value.length === 10 ? value : new Date(value).toLocaleDateString('sv-SE'); }
function dateOf(value: string) { return value.length === 10 ? new Date(value + 'T12:00:00') : new Date(value); }
function hours(event: CalendarEvent['content']) {
  return event.all_day ? 'Todo el día' : `${dateOf(event.start).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })} – ${dateOf(event.end).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })}`;
}
function dateLabel(value: string) {
  const today = new Date(), yesterday = new Date(); yesterday.setDate(today.getDate()-1);
  const key = localDay(value);
  return key === localDay(today.toISOString()) ? 'Hoy' : key === localDay(yesterday.toISOString()) ? 'Ayer' : dateOf(value).toLocaleDateString('es', { weekday: 'short', day: 'numeric', month: 'short' });
}

export function CalendarHome({ folders, meetings, loading, refreshToken, onOpen, onConnect, onPrepare, onRefresh }: {
  folders: Folder[]; meetings: Meeting[]; loading: boolean; refreshToken: number;
  onOpen: (id: string) => void; onConnect: () => void; onPrepare: (pack: CalendarPreparation) => void;
  onRefresh?: () => void;
}) {
  const calendar = useCalendar(refreshToken);
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<CalendarEvent | null>(null);
  const [pack, setPack] = useState<CalendarPreparation | null>(null);
  const [error, setError] = useState(''), [preparing, setPreparing] = useState(false);
  const version = useRef(0);
  useEffect(() => () => { version.current++; }, []);
  const today = localDay(new Date().toISOString());
  const groups = new Map<string, CalendarEvent[]>();
  for (const event of calendar.state?.items ?? []) {
    const key = localDay(event.content.start);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(event);
  }
  if (!groups.has(today)) groups.set(today, []);
  const days = [...groups.keys()].sort(), lastPage = Math.max(0, Math.ceil(days.length/3)-1);
  const visibleDays = days.slice(Math.min(page, lastPage)*3, Math.min(page, lastPage)*3+3);
  const history = new Map<string, Meeting[]>();
  for (const meeting of meetings) {
    const key = meeting.created_at ? dateLabel(meeting.created_at) : 'Sin fecha';
    if (!history.has(key)) history.set(key, []);
    history.get(key)!.push(meeting);
  }
  async function openEvent(event: CalendarEvent) {
    const ticket = ++version.current;
    setSelected(event); setPack(null); setError(''); setPreparing(true);
    try { const result = await request<CalendarPreparation>('GET', `/v1/calendar/events/${event.id}/preparation`); if (ticket === version.current) setPack(result); }
    catch (e) { if (ticket === version.current) setError(message(e)); }
    finally { if (ticket === version.current) setPreparing(false); }
  }
  async function assignFolder(folderId: string) {
    if (!selected) return;
    const ticket = ++version.current;
    setPreparing(true); setPack(null); setError('');
    try {
      const next = await request<CalendarPreparation>('PUT', `/v1/calendar/events/${selected.id}/folder`, { folder_id: folderId || null });
      if (ticket === version.current) setPack(next);
      void calendar.refresh();
    } catch (e) { if (ticket === version.current) setError(message(e)); }
    finally { if (ticket === version.current) setPreparing(false); }
  }
  function close() { version.current++; setSelected(null); setPack(null); setError(''); setPreparing(false); }
  async function begin() {
    if (!selected || preparing) return;
    const ticket = ++version.current;
    setPreparing(true); setError('');
    try {
      // Recheck current event, source revisions and permissions at handoff.
      const fresh = await request<CalendarPreparation>('GET', `/v1/calendar/events/${selected.id}/preparation`);
      if (ticket !== version.current) return;
      onPrepare(fresh); close();
    } catch (e) { if (ticket === version.current) { setPack(null); setError(message(e)); setPreparing(false); } }
  }
  return <div className="calendar-home" data-slot="calendar-home">
    <section aria-labelledby="calendar-title" className="calendar-upcoming">
      <header className="calendar-section-head"><h1 id="calendar-title">Próximas reuniones</h1><div>
        <Button variant="ghost" aria-label="Actualizar calendario" loading={calendar.busy} disabled={!calendar.state?.connected} onClick={() => { onRefresh?.(); void calendar.refresh(true, true); }}><RefreshCw size={17} /></Button>
        <Button variant="ghost" aria-label="Días anteriores" disabled={page === 0} onClick={() => setPage(n => n-1)}><ChevronLeft size={19} /></Button>
        <Button variant="ghost" aria-label="Días siguientes" disabled={page >= lastPage} onClick={() => setPage(n => n+1)}><ChevronRight size={19} /></Button>
      </div></header>
      {calendar.error && <div className="voice-alert" role="alert">{calendar.error}<Button variant="ghost" onClick={() => void calendar.refresh(true)}>Reintentar</Button></div>}
      {!calendar.state ? <p role="status">{calendar.error ? 'El calendario no está disponible.' : 'Consultando tu calendario…'}</p> : !calendar.state.connected ? <div className="calendar-connect-prompt"><CalendarDays size={28} /><div><h2>Tu próxima reunión empieza aquí</h2><p>Conecta Google para ver eventos, asistentes y preparar el briefing.</p></div><Button variant="outline" onClick={onConnect}>Conectar calendario</Button></div> : <div className="calendar-agenda">
        {visibleDays.map(day => { const date = dateOf(day), events = groups.get(day)!; return <div className="calendar-day" key={day}>
          <div className="calendar-date"><strong>{date.getDate()}</strong><div><span>{date.toLocaleDateString('es', { month: 'long' })}{day === today && <span className="calendar-today"> · hoy</span>}</span><small>{date.toLocaleDateString('es', { weekday: 'long' })}</small></div></div>
          <div className="calendar-day-events">{events.length ? events.map(event => <button type="button" className="calendar-event" key={event.id} aria-controls="calendar-preparation" aria-expanded={selected?.id === event.id} onClick={() => void openEvent(event)}><span className="calendar-event-bar" /><span><strong>{event.content.title}</strong><small>{hours(event.content)}{event.content.attendees.some(a => a.response !== 'declined') ? ` · ${event.content.attendees.filter(a => a.response !== 'declined').length} invitados` : ''}</small></span><ChevronRight size={17} /></button>) : <p className="calendar-no-events">No quedan reuniones para hoy</p>}</div>
        </div>; })}
      </div>}
    </section>
    <section aria-label="Reuniones recientes" className="calendar-history">
      {loading && !meetings.length && <p role="status">Cargando tus notas…</p>}
      {!loading && !meetings.length && <p>Tus notas aparecerán aquí al terminar la primera reunión.</p>}
      {[...history].map(([day, entries]) => <section key={day}><h2>{day}</h2>{entries.map(meeting => <button type="button" className="calendar-history-row" key={meeting.id} onClick={() => onOpen(meeting.id)}><span className="calendar-history-icon"><FileText size={22} /></span><span><strong>{meeting.title}</strong><small>{folders.find(f => f.id === meeting.folder_id)?.name ?? 'Privada · Sin carpeta'}</small></span><time>{meeting.created_at ? new Date(meeting.created_at).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' }) : ''}</time></button>)}</section>)}
    </section>
    <MeetingPanel id="calendar-preparation" title="Preparar reunión" open={!!selected} onClose={close} footer={pack ? `${pack.glossary.length} términos · ${pack.passages.length} antecedentes con fuente` : 'Contexto de tu biblioteca'}>
      {selected && <div className="calendar-briefing"><h2>{selected.content.title}</h2><p><Clock3 size={16} /> {dateLabel(selected.content.start)} · {hours(selected.content)}</p>
        <label>Contexto de la carpeta<select className="voice-input" aria-label="Contexto de la carpeta" disabled={preparing} value={pack?.folder_id ?? selected.folder_id ?? ''} onChange={e => void assignFolder(e.target.value)}><option value="">Privada · Sin carpeta</option>{folders.filter(f => f.can_edit).map(f => <option key={f.id} value={f.id}>{f.name}</option>)}</select></label>
        {error && <div role="alert" className="voice-alert">{error}<Button variant="ghost" onClick={() => void openEvent(selected)}>Reintentar</Button></div>}
        {preparing && <p role="status">Preparando contexto…</p>}
        {pack && <><h3>Agenda prevista</h3><p>{pack.event.description || 'La invitación no incluye una agenda.'}</p>
          <h3><Users size={17} /> Invitados</h3><ul className="calendar-guests">{pack.expected_attendees.map((a, i) => <li key={`${a.email}:${i}`}><strong>{a.name || a.email}</strong><small>{a.response === 'declined' ? 'Ha rechazado' : a.profile_available ? 'Perfil de voz disponible' : 'Voz pendiente de identificar'}</small></li>)}</ul>{pack.event.attendees_omitted && <p>Google no ha incluido todos los invitados.</p>}
          <h3>Antecedentes en tu biblioteca</h3>{pack.passages.length ? pack.passages.map(p => <article className="calendar-context-source" key={p.evidence_id}><p>{p.text}</p><button type="button" onClick={() => { close(); onOpen(p.meeting_id); }}>{p.meeting_title} · {p.time}</button></article>) : <p>{pack.folder_id ? 'Sin antecedentes relacionados en esta carpeta.' : 'Elige una carpeta para incorporar antecedentes.'}</p>}
          {pack.glossary.length > 0 && <details><summary>Glosario preparado · {pack.glossary.length} términos</summary><ul>{pack.glossary.map(g => <li key={g.id}><strong>{g.term}</strong>{g.definition ? ` · ${g.definition}` : ''}</li>)}</ul></details>}
          <Button disabled={preparing} loading={preparing} onClick={() => void begin()}>Preparar nota</Button><small>La grabación empieza cuando tú la inicies.</small>
        </>}
      </div>}
    </MeetingPanel>
  </div>;
}
