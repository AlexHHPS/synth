'use client';
import { brand } from './brand';

import { useEffect, useState } from 'react';
import { getVersion } from '@tauri-apps/api/app';
import { LogOut, RefreshCw, Settings2, UserRound } from 'lucide-react';
import { Button } from './design-system/button';
import { ChecksList, PermissionRows } from './SetupParts';
import { restartSetup } from './setup';
import { useChecks, usePermissions } from './useSetup';
import { message, pipelineRequest } from './api';
import './setup.css';

interface SettingsViewProps { email?: string; onOpenVoice: () => void }

export function SettingsView({ email, onOpenVoice }: SettingsViewProps) {
  const mac = useChecks(true);
  const permissions = usePermissions();
  const [version, setVersion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { void getVersion().then(setVersion).catch(() => setVersion('')); }, []);

  async function act(action: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await action(); window.location.reload(); }
    catch (e) { setError(message(e)); setBusy(false); }
  }

  return <div className="voice-library synth-settings">
    <div className="voice-page-heading"><div><p className="voice-eyebrow">TU MAC</p><h1>Ajustes</h1><p>Estado de este Mac, permisos y tu cuenta.</p></div><Settings2 size={22} /></div>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    <section className="synth-settings-section">
      <header><h2>Este Mac</h2><Button size="sm" variant="outline" disabled={mac.running} onClick={mac.rerun}><RefreshCw size={14} /> Volver a comprobar</Button></header>
      <ChecksList checks={mac.checks} />
    </section>
    <section className="synth-settings-section">
      <header><h2>Permisos de audio</h2></header>
      <PermissionRows states={permissions.states} busy={permissions.busy} onRequest={permissions.request} onOpenSettings={permissions.openSettings} checkLabel />
    </section>
    <section className="synth-settings-section">
      <header><h2>Perfil de voz</h2><Button size="sm" variant="outline" onClick={onOpenVoice}><UserRound size={14} /> Abrir perfil de voz</Button></header>
      <p className="synth-settings-note">Pone tu nombre en tus intervenciones de las transcripciones.</p>
    </section>
    <section className="synth-settings-section">
      <header><h2>Cuenta</h2><Button size="sm" variant="outline" loading={busy} onClick={() => void act(() => pipelineRequest('POST', '/v1/auth/logout'))}><LogOut size={14} /> Cerrar sesión</Button></header>
      <p className="synth-settings-note">{email ?? `Cuenta de ${brand.name}`}</p>
    </section>
    <section className="synth-settings-section">
      <header><h2>Acerca de</h2><Button size="sm" variant="ghost" disabled={busy} onClick={() => void act(restartSetup)}>Repetir la configuración inicial</Button></header>
      <p className="synth-settings-note"><code>{brand.name} {version}</code> · audio y transcripción en este Mac, actas con el servicio de actas de tu organización.</p>
    </section>
  </div>;
}
