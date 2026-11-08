'use client';
import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import Image from 'next/image';
import { ChevronDown, Clock3, VolumeX, X } from 'lucide-react';
import './theme.css';
import './call-reminder.css';

type Hint = {id: string; app_id: string; label: string};
type Status = {pending: Hint[]; settings: {enabled: boolean; excluded_apps: string[]}};

export function CallReminder() {
  const [status, setStatus] = useState<Status | null>(null);
  const [menu, setMenu] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const hint = status?.pending[0];
  useEffect(() => {
    let active = true;
    const load = async () => {
      try { const next = await invoke<Status>('synth_detection_status'); if(active)setStatus(next); }
      catch { if(active)setError('No se pudo consultar el aviso.'); }
    };
    void load(); const timer = setInterval(() => void load(), 2000);
    return () => {active=false;clearInterval(timer);};
  }, []);
  useEffect(() => {
    setMenu(false); setError('');
    void invoke('synth_detection_widget_expand', {expanded:false}).catch(() => {});
  }, [hint?.id]);
  useEffect(() => {
    if(!hint || menu || busy)return;
    const timer=setTimeout(() => {void invoke('synth_detection_action', {id:hint.id,action:'dismiss'}).catch(() => {});},15000);
    return () => clearTimeout(timer);
  }, [hint?.id,menu,busy]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if(event.key==='Escape') {
        if(!menu&&hint)void invoke('synth_detection_action',{id:hint.id,action:'dismiss'}).catch(() => {});
        setMenu(false);void invoke('synth_detection_widget_expand',{expanded:false}).catch(() => {});
      }
    };
    window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape);
  }, [menu,hint?.id]);
  async function action(name: 'prepare' | 'snooze' | 'dismiss' | 'silence') {
    if(!hint || !status || busy)return;
    setBusy(true);setError('');
    try {
      if(name==='silence')await invoke('synth_detection_settings',{settings:{...status.settings,excluded_apps:[...new Set([...status.settings.excluded_apps,hint.app_id])]}});
      else await invoke('synth_detection_action',{id:hint.id,action:name});
      setMenu(false);setStatus(await invoke<Status>('synth_detection_status'));
    } catch {
      setError('No se pudo completar. Vuelve a intentarlo.');setMenu(true);
      void invoke('synth_detection_widget_expand',{expanded:true}).catch(() => {});
    }
    finally {setBusy(false);}
  }
  async function toggle() {
    const expanded=!menu;
    try {await invoke('synth_detection_widget_expand',{expanded});setMenu(expanded);}
    catch {setError('No se pudo abrir el menú.');}
  }
  if(!hint)return <div className="voice-call-widget-shell" data-slot="call-reminder"><span className="voice-widget-empty">Sin llamadas pendientes</span></div>;
  return <div className="voice-call-widget-shell" data-slot="call-reminder">
    <section className="voice-call-widget" aria-label="Aviso de reunión">
      <div className="voice-widget-label" data-tauri-drag-region><strong data-tauri-drag-region>Reunión detectada</strong><span data-tauri-drag-region>{hint.label}</span></div>
      <div className="voice-widget-actions">
        <button className="voice-widget-take" disabled={busy} onClick={()=>void action('prepare')}><Image src="/synth-mark.png" alt="" width={22} height={22}/>Tomar notas</button>
        <button className="voice-widget-more" disabled={busy} onClick={()=>void toggle()} aria-label="Opciones del aviso" aria-expanded={menu} aria-controls="call-reminder-options"><ChevronDown size={18}/></button>
      </div>
    </section>
    {menu&&<div className="voice-widget-menu" id="call-reminder-options" aria-label="Opciones del aviso">
      <button disabled={busy} onClick={()=>void action('snooze')}><Clock3 size={16}/>Recordar en un minuto</button>
      <button disabled={busy} onClick={()=>void action('silence')}><VolumeX size={16}/>Silenciar esta app</button>
      <button disabled={busy} onClick={()=>void action('dismiss')}><X size={16}/>Descartar aviso</button>
    </div>}
    {error&&<p className="voice-widget-error" role="alert">{error}</p>}
  </div>;
}
