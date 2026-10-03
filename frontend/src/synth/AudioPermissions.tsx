'use client';
import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Mic, Volume2, Check, ShieldCheck } from 'lucide-react';
import { CardHeader } from './design-system/card-header';
import { Button } from './design-system/button';
import { message } from './api';

export function AudioPermissions() {
  const [visible, setVisible] = useState(false);
  const [mic, setMic] = useState(false);
  const [system, setSystem] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { setVisible(localStorage.getItem('synth_audio_permissions') !== 'confirmed'); }, []);
  async function allow(kind: 'mic' | 'system') {
    setBusy(true); setError('');
    try {
      const granted = await invoke<boolean>(kind === 'mic' ? 'trigger_microphone_permission' : 'trigger_system_audio_permission_command');
      if (!granted) { setError('Activa el permiso en Ajustes del Sistema y vuelve a comprobarlo.'); return; }
      if (kind === 'mic') setMic(true); else setSystem(true);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  if (!visible) return null;
  return <section className="voice-audio-permissions"><CardHeader icon={ShieldCheck} title="Prepara el audio de este Mac" description="El micrófono captura tu voz. El audio del sistema captura a los demás en una llamada." />
    <div className="voice-permission-actions"><Button size="sm" variant="outline" disabled={busy || mic} onClick={() => void allow('mic')}>{mic ? <Check size={14} /> : <Mic size={14} />}{mic ? 'Micrófono permitido' : 'Permitir micrófono'}</Button><Button size="sm" variant="outline" disabled={busy || system} onClick={() => void allow('system')}>{system ? <Check size={14} /> : <Volume2 size={14} />}{system ? 'Audio del sistema permitido' : 'Permitir audio del sistema'}</Button><Button size="sm" disabled={!mic || !system} onClick={() => { localStorage.setItem('synth_audio_permissions', 'confirmed'); setVisible(false); }}>Continuar</Button><Button size="sm" variant="ghost" onClick={() => setVisible(false)}>Más tarde</Button></div>
    {error && <p className="voice-alert" role="alert">{error}<Button size="xs" variant="ghost" onClick={() => void invoke('open_system_settings')}>Abrir ajustes</Button></p>}
  </section>;
}
