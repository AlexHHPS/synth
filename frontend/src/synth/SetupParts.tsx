'use client';

import { Camera, CircleAlert, CircleCheck, Loader2, Mic, Monitor, TriangleAlert } from 'lucide-react';
import { Button } from './design-system/button';
import type { Check, CheckState, Permission, PermissionState } from './setup';
import type { VisionSetup } from './useSetup';

const STATE_ICON: Record<CheckState, typeof CircleCheck> = { ok: CircleCheck, warn: TriangleAlert, error: CircleAlert, pending: Loader2 };
const STATE_NAME: Record<CheckState, string> = { ok: 'Listo', warn: 'Revisar', error: 'Falla', pending: 'Comprobando' };

interface ChecksListProps { checks: Check[]; vision?: VisionSetup }

/** The preflight ledger: one line per check, its measured value in mono, its state as icon plus word. */
export function ChecksList({ checks, vision }: ChecksListProps) {
  return <ul className="synth-ledger" aria-live="polite">
    {checks.map(check => {
      const Icon = STATE_ICON[check.state];
      return <li key={check.key} className="synth-ledger-row" data-state={check.state}>
        <Icon size={16} aria-hidden className={check.state === 'pending' ? 'synth-spin' : undefined} />
        <div className="synth-ledger-text"><span>{check.label}</span>{check.fix && <small>{check.fix}</small>}</div>
        <code>{check.value}</code>
        <span className="synth-sr">{STATE_NAME[check.state]}</span>
      </li>;
    })}
    {vision && <VisionRow vision={vision} />}
  </ul>;
}

function VisionRow({ vision }: { vision: VisionSetup }) {
  const ready = vision.state === 'ready';
  const pending = vision.state === 'checking' || vision.state === 'preparing';
  const state = ready ? 'ok' : pending ? 'pending' : 'warn';
  const Icon = STATE_ICON[state];
  const canPrepare = vision.state === 'missing' || vision.state === 'failed';
  const status = {
    checking: 'Comprobando…', ready: 'Listo', missing: 'Opcional',
    preparing: 'Preparando…', failed: 'Preparación interrumpida', unavailable: 'Estado por comprobar',
  }[vision.state];
  return <li className="synth-ledger-row synth-vision-row" data-state={state}>
    <Icon size={16} aria-hidden className={pending ? 'synth-spin' : undefined} />
    <div className="synth-ledger-text">
      <span>Modelo de análisis visual</span>
      <small>Qwen3‑VL 4B Instruct · 4 bits{vision.state === 'missing' ? ' · Descarga opcional de 3,1 GB' : ''}</small>
      {vision.state === 'failed' && <small>Comprueba la conexión y el espacio libre.</small>}
    </div>
    {canPrepare
      ? <Button size="sm" variant="outline" onClick={vision.prepare}>{vision.state === 'failed' ? 'Reintentar' : 'Preparar'}</Button>
      : <code>{status}</code>}
  </li>;
}

interface PermissionRowsProps {
  states: Record<Permission, PermissionState>;
  busy: Permission | null;
  onRequest: (kind: Permission) => void;
  onOpenSettings: (kind: Permission) => void;
  /** In Ajustes the state is unknown until asked, so the action reads as a check, not a first request. */
  checkLabel?: boolean;
}

const PERMISSIONS: { kind: Permission; icon: typeof Mic; title: string; detail: string; action: string }[] = [
  { kind: 'mic', icon: Mic, title: 'Micrófono', detail: 'Tu voz.', action: 'Permitir micrófono' },
  { kind: 'system', icon: Monitor, title: 'Audio del ordenador', detail: 'Las demás personas de una llamada.', action: 'Permitir audio del ordenador' },
  { kind: 'screen', icon: Camera, title: 'Capturas de pantalla', detail: 'Opcional. Añade regiones de pantalla al contexto del acta.', action: 'Permitir capturas' },
];

export function PermissionRows({ states, busy, onRequest, onOpenSettings, checkLabel = false }: PermissionRowsProps) {
  const denied = PERMISSIONS.find(({ kind }) => states[kind] === 'denied')?.kind;
  return <div className="synth-permissions">
    {PERMISSIONS.map(({ kind, icon: Icon, title, detail, action }) => {
      const state = states[kind];
      return <div key={kind} className="synth-permission" data-state={state}>
        <Icon size={18} aria-hidden />
        <div><strong>{title}</strong><span>{detail}</span></div>
        {state === 'granted'
          ? <span className="synth-permission-done"><CircleCheck size={15} aria-hidden /> Permitido</span>
          : <Button size="sm" variant="outline" loading={busy === kind} disabled={busy !== null} onClick={() => onRequest(kind)}>{state === 'denied' ? 'Volver a comprobar' : checkLabel ? 'Comprobar' : action}</Button>}
      </div>;
    })}
    {denied && <div className="synth-callout" role="alert"><span>macOS ha bloqueado el permiso. Actívalo en Privacidad y seguridad y vuelve a comprobarlo.</span>
      <Button size="xs" variant="ghost" onClick={() => onOpenSettings(denied)}>Abrir Ajustes del Sistema</Button></div>}
  </div>;
}
