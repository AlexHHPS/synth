'use client';
import { brand } from './brand';

import { useState } from 'react';
import Image from 'next/image';
import { ArrowLeft, ArrowRight, Check as CheckIcon, Mic, RefreshCw } from 'lucide-react';
import { Button } from './design-system/button';
import { ChecksList, PermissionRows } from './SetupParts';
import { completeSetup, readyToRecord } from './setup';
import { useChecks, usePermissions } from './useSetup';
import { message } from './api';
import './setup.css';

export type SetupExit = 'capture' | 'library' | 'voice';
type Step = 'welcome' | 'permissions' | 'mac' | 'voice' | 'done';
const STEPS: { id: Step; title: string }[] = [
  { id: 'welcome', title: 'Bienvenida' }, { id: 'permissions', title: 'Permisos de audio' },
  { id: 'mac', title: 'Comprobación del Mac' }, { id: 'voice', title: 'Perfil de voz' }, { id: 'done', title: 'Primera nota' },
];

interface SetupFlowProps { email?: string; onDone: (exit: SetupExit) => void }

export function SetupFlow({ email, onDone }: SetupFlowProps) {
  const [step, setStep] = useState<Step>('welcome');
  const [error, setError] = useState('');
  const [finishing, setFinishing] = useState(false);
  const permissions = usePermissions();
  const mac = useChecks(step === 'mac' || step === 'done');
  const index = STEPS.findIndex(s => s.id === step);
  const go = (offset: number) => { setError(''); setStep(STEPS[index + offset].id); };

  async function finish(exit: SetupExit) {
    setFinishing(true); setError('');
    try { await completeSetup(); onDone(exit); }
    catch (e) { setError(message(e)); setFinishing(false); }
  }

  const values: Record<Step, string> = {
    welcome: email ?? `Cuenta de ${brand.name}`,
    permissions: permissions.states.mic === 'granted' ? (permissions.states.system === 'granted' ? 'Micrófono y ordenador' : 'Micrófono') : 'Pendiente',
    mac: mac.running ? 'Comprobando…' : readyToRecord(mac.checks) ? 'Todo listo' : 'Pendiente',
    voice: index > 3 ? 'Más tarde' : 'Opcional', done: '',
  };

  return <div className="synth-setup">
    <aside className="synth-setup-rail" aria-label="Pasos de la configuración">
      <div className="synth-setup-brand"><Image src={brand.logo} width={26} height={26} alt="" /><div><strong>{brand.name}</strong><span>{brand.tagline}</span></div></div>
      <ol className="synth-setup-steps">
        {STEPS.map((s, i) => <li key={s.id} data-state={i < index ? 'done' : i === index ? 'current' : 'next'} aria-current={i === index ? 'step' : undefined}>
          <span className="synth-step-mark" aria-hidden>{i < index ? <CheckIcon size={12} /> : i + 1}</span>
          <div><span>{s.title}</span>{values[s.id] && <code>{values[s.id]}</code>}</div>
        </li>)}
      </ol>
      <p className="synth-setup-rail-foot">El audio no sale de este Mac.</p>
    </aside>
    <main className="synth-setup-panel">
      <div className="synth-setup-body" key={step}>
        {step === 'welcome' && <Welcome onNext={() => go(1)} />}
        {step === 'permissions' && <section>
          <p className="synth-eyebrow">Paso 2 de 5</p>
          <h1>Deja que {brand.name} escuche</h1>
          <p className="synth-lead">macOS te pedirá permiso una vez para cada fuente de audio.</p>
          <PermissionRows states={permissions.states} busy={permissions.busy} onRequest={permissions.request} onOpenSettings={permissions.openSettings} />
          <Nav onBack={() => go(-1)} next={<Button variant="fancy" disabled={permissions.states.mic !== 'granted'} onClick={() => go(1)}>Continuar <ArrowRight size={15} /></Button>}
            aside={permissions.states.mic === 'granted' && permissions.states.system !== 'granted' ? <Button variant="ghost" size="sm" onClick={() => go(1)}>Seguir sin audio del ordenador</Button> : null} />
        </section>}
        {step === 'mac' && <section>
          <p className="synth-eyebrow">Paso 3 de 5</p>
          <h1>Comprobamos este Mac</h1>
          <p className="synth-lead">La transcripción se hace aquí; las actas, en el servicio de tu organización.</p>
          <ChecksList checks={mac.checks} />
          <Nav onBack={() => go(-1)} next={<Button variant="fancy" disabled={mac.running || !readyToRecord(mac.checks)} onClick={() => go(1)}>Continuar <ArrowRight size={15} /></Button>}
            aside={!mac.running && !readyToRecord(mac.checks) ? <Button variant="outline" size="sm" onClick={mac.rerun}><RefreshCw size={14} /> Volver a comprobar</Button> : null} />
        </section>}
        {step === 'voice' && <section>
          <p className="synth-eyebrow">Paso 4 de 5 · opcional</p>
          <h1>Que tus notas sepan quién eres</h1>
          <p className="synth-lead">Graba 30 segundos hablando tú solo y tus intervenciones llevarán tu nombre.</p>
          <div className="synth-voice-sample" aria-hidden><span>Tú</span><p>Buenos días. Hoy repasamos el piloto y cerramos los próximos pasos.</p></div>
          <Nav onBack={() => go(-1)} next={<Button variant="fancy" loading={finishing} onClick={() => void finish('voice')}><Mic size={15} /> Crear mi perfil de voz</Button>}
            aside={<Button variant="ghost" size="sm" disabled={finishing} onClick={() => go(1)}>Más tarde</Button>} />
        </section>}
        {step === 'done' && <section>
          <p className="synth-eyebrow">Paso 5 de 5</p>
          <h1>Todo listo para tu primera nota</h1>
          <p className="synth-lead">Graba una reunión o habla para ti. Al terminar tendrás la transcripción y el acta.</p>
          <ChecksList checks={mac.checks} />
          <Nav onBack={() => go(-1)} next={<Button variant="fancy" loading={finishing} disabled={mac.running} onClick={() => void finish('capture')}><Mic size={15} /> Grabar mi primera nota</Button>}
            aside={<Button variant="ghost" size="sm" disabled={finishing} onClick={() => void finish('library')}>Ir a la biblioteca</Button>} />
        </section>}
        {error && <div className="synth-callout" role="alert">{error}</div>}
      </div>
    </main>
  </div>;
}

function Welcome({ onNext }: { onNext: () => void }) {
  return <section>
    <p className="synth-eyebrow">Primer arranque</p>
    <h1>Prepara {brand.name} en este Mac</h1>
    <p className="synth-lead">Tres minutos: permisos de audio, una comprobación del equipo y, si quieres, tu perfil de voz.</p>
    <dl className="synth-data-map">
      <div><dt>Audio</dt><dd>Se queda en este Mac y se borra en 24 horas.</dd></div>
      <div><dt>Transcripción</dt><dd>En este Mac, con Whisper.</dd></div>
      <div><dt>Actas</dt><dd>El texto viaja al Servicio de actas de tu organización.</dd></div>
    </dl>
    <Nav next={<Button variant="fancy" onClick={onNext}>Empezar <ArrowRight size={15} /></Button>} />
  </section>;
}

interface NavProps { onBack?: () => void; next: React.ReactNode; aside?: React.ReactNode }

function Nav({ onBack, next, aside }: NavProps) {
  return <div className="synth-setup-nav">
    {onBack ? <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={14} /> Atrás</Button> : <span />}
    <div>{aside}{next}</div>
  </div>;
}
