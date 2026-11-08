'use client';
import { brand } from './brand';

import Image from 'next/image';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { Button } from './design-system/button';
import { ChecksList } from './SetupParts';
import type { Check } from './setup';
import './setup.css';

export interface AuthState { mode: 'keys' | 'supabase'; signed_in: boolean; principal_id?: string; email?: string; allowed_domains?: string[]; flow_state?: string; error_code?: string }

/** After this many failed polls (2.5 s apart) the service is treated as stuck rather than still starting. */
export const STUCK_AFTER = 8;

interface SignInViewProps {
  session: AuthState | null;
  error: string;
  attempts: number;
  busy: boolean;
  onSignIn: () => void;
  onRetry: () => void;
}

function serviceCheck(session: AuthState | null, attempts: number): Check {
  const base = { key: 'service' as const, label: 'Servicio de este Mac' };
  if (session) return { ...base, state: 'ok', value: 'Conectado' };
  if (attempts < STUCK_AFTER) return { ...base, state: 'pending', value: 'Arrancando…' };
  return { ...base, state: 'error', value: 'No responde', fix: `Cierra ${brand.name} y vuelve a abrirlo. Si sigue igual, contacta con soporte.` };
}

function accountCheck(session: AuthState | null): Check {
  const base = { key: 'synth' as const, label: `Cuenta de ${brand.name}` };
  if (!session) return { ...base, state: 'pending', value: 'Esperando al servicio' };
  if (session.flow_state === 'waiting') return { ...base, state: 'pending', value: 'Esperando a tu navegador…' };
  if (session.flow_state === 'failed') return { ...base, state: 'error', value: 'Acceso no completado', fix: 'Elige tu cuenta corporativa en el navegador y vuelve a intentarlo.' };
  return { ...base, state: 'warn', value: 'Sin iniciar sesión' };
}

/** The sign-in gate: the same rail and ledger as the first-run setup, so signing in reads as its step zero. */
export function SignInView({ session, error, attempts, busy, onSignIn, onRetry }: SignInViewProps) {
  const stuck = !session && attempts >= STUCK_AFTER;
  const waiting = session?.flow_state === 'waiting';
  const domains = session?.allowed_domains?.map(d => '@' + d).join(', ');

  return <div className="synth-setup">
    <aside className="synth-setup-rail" aria-label={brand.name}>
      <div className="synth-setup-brand"><Image src={brand.logo} width={26} height={26} alt="" /><div><strong>{brand.name}</strong><span>{brand.tagline}</span></div></div>
      <dl className="synth-data-map synth-signin-map">
        <div><dt>Audio</dt><dd>Se graba y se transcribe en este Mac.</dd></div>
        <div><dt>Actas</dt><dd>Solo el texto viaja a el servicio de tu organización.</dd></div>
        <div><dt>Reuniones</dt><dd>Privadas hasta que las compartas.</dd></div>
      </dl>
      <p className="synth-setup-rail-foot">Acceso con tu cuenta corporativa de Google.</p>
    </aside>
    <main className="synth-setup-panel">
      <div className="synth-setup-body">
        <section>
          <p className="synth-eyebrow">{session ? 'Iniciar sesión' : stuck ? 'El servicio no arranca' : `Abriendo ${brand.name}`}</p>
          <h1>{waiting ? 'Termina en tu navegador' : 'Tus reuniones, transcritas y con acta'}</h1>
          <p className="synth-lead">{waiting
            ? 'Hemos abierto Google en tu navegador. Elige tu cuenta y esta ventana seguirá sola.'
            : `Entra con tu cuenta de ${brand.name} para ver tus notas, tareas y decisiones.`}</p>
          <ChecksList checks={[serviceCheck(session, attempts), accountCheck(session)]} />
          {stuck && error && <p className="synth-signin-detail"><code>{error}</code></p>}
          {session && error && <div className="synth-callout" role="alert">{error}</div>}
          {(session || stuck) && <div className="synth-setup-nav">
            {domains ? <span className="synth-signin-domain">Para cuentas <code>{domains}</code></span> : <span />}
            <div>
              {stuck && <Button variant="outline" onClick={onRetry} loading={busy}><RefreshCw size={14} /> Volver a intentar</Button>}
              {session && <Button variant={waiting ? 'outline' : 'fancy'} loading={busy} onClick={onSignIn}>
                {waiting ? 'Abrir Google otra vez' : 'Continuar con Google'} <ArrowUpRight size={15} />
              </Button>}
            </div>
          </div>}
        </section>
      </div>
    </main>
  </div>;
}
