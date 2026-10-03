'use client';

import { useEffect, useRef, useState } from 'react';
import { Field } from '@base-ui/react/field';
import { Copy, KeyRound, Plug, Plus } from 'lucide-react';
import { Button } from './design-system/button';
import { FormField } from './design-system/form-field';
import { Folder, Integration, Page, message, pipelineRequest, request } from './api';

export function IntegrationsView({ folders, refreshToken, onCreateFolder }: {
  folders: Folder[]; refreshToken: number; onCreateFolder: () => void;
}) {
  const [items, setItems] = useState<Integration[]>([]);
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [fresh, setFresh] = useState<{ id: string; name: string; token: string } | null>(null);
  const [reload, setReload] = useState(0);
  const [endpoints, setEndpoints] = useState<{ api: string; mcp: string; cloud: boolean } | null>(null);
  const [endpointError, setEndpointError] = useState('');
  const latch = useRef(false);
  const owned = folders.filter(folder => folder.can_manage);

  useEffect(() => {
    let active = true;
    void pipelineRequest<{ backend_url: string }>('GET', '/v1/status')
      .then(status => {
        const base = status.backend_url;
        if (!base || !/^(https:\/\/|http:\/\/127\.0\.0\.1:18280$)/.test(base)) {
          throw new Error('No se pudo verificar el servicio de la biblioteca.');
        }
        if (active) {
          setEndpoints({ api: base + '/v1', mcp: base + '/mcp', cloud: base.startsWith('https:') });
          setEndpointError('');
        }
      })
      .catch(() => {
        if (active) { setEndpoints(null); setEndpointError('No se pudieron consultar las direcciones de integración. Pulsa Actualizar para volver a intentarlo.'); }
      });
    return () => { active = false; };
  }, [refreshToken]);

  async function copyEndpoint(value: string) {
    try { await navigator.clipboard.writeText(value); setToast('Dirección copiada.'); }
    catch { setError('No se pudo copiar la dirección.'); }
  }

  useEffect(() => {
    let active = true;
    setLoading(true);
    void request<Page<Integration>>('GET', '/v1/integrations')
      .then(page => { if (active) { setItems(page.items); setError(''); } })
      .catch(e => { if (active) setError(message(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refreshToken, reload]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (latch.current || fresh || !name.trim() || !scopes.length) return;
    latch.current = true; setCreating(true); setError(''); setToast('');
    try {
      const result = await request<{ id: string; name: string; token: string }>('POST', '/v1/integrations',
        { name: name.trim(), folder_ids: scopes });
      // The integration token is held only in this view's memory and revealed once.
      setFresh(result); setName(''); setScopes([]); setReload(old => old + 1);
    } catch (e) { setError(message(e)); setReload(old => old + 1); }
    finally { latch.current = false; setCreating(false); }
  }

  async function revoke(id: string) {
    if (latch.current) return;
    latch.current = true; setBusy(id); setError(''); setToast('');
    try {
      await request('DELETE', `/v1/integrations/${id}`);
      setConfirm(null); if (fresh?.id === id) setFresh(null);
      setToast('Acceso revocado.'); setReload(old => old + 1);
    } catch (e) { setError(message(e)); }
    finally { latch.current = false; setBusy(null); }
  }

  async function copy() {
    if (!fresh) return;
    try { await navigator.clipboard.writeText(fresh.token); setToast('Clave copiada.'); }
    catch { setError('No se pudo copiar la clave.'); }
  }

  return <div className="voice-library">
    <div className="voice-page-heading"><div><p className="voice-eyebrow">CONECTA TU CONOCIMIENTO</p><h1>Integraciones</h1><p>Comparte transcripciones y actas con otros productos de tu organización.</p></div><Plug size={24} /></div>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {toast && <p className="voice-success" role="status">{toast}</p>}
    {endpointError && <p className="voice-notice" role="status">{endpointError}</p>}
    {endpoints && <>
      <div className="voice-integration-endpoints">{[
        { label: endpoints.cloud ? 'API de la biblioteca cloud' : 'API de la biblioteca local', value: endpoints.api },
        { label: endpoints.cloud ? 'MCP de la biblioteca cloud' : 'MCP de la biblioteca local', value: endpoints.mcp },
      ].map(endpoint => <div key={endpoint.label}><strong>{endpoint.label}</strong><code>{endpoint.value}</code><Button variant="ghost" size="sm" onClick={() => void copyEndpoint(endpoint.value)}><Copy size={14} /> Copiar dirección</Button></div>)}</div>
      <p className="voice-notice">{endpoints.cloud ? 'Puedes consultar la biblioteca desde otros equipos, aunque este Mac esté apagado. ' : ''}Usa una clave de integración con <code>Authorization: Bearer &lt;clave&gt;</code>. MCP utiliza Streamable HTTP.</p>
    </>}
    <section className="voice-integration-create"><h2><KeyRound size={17} /> Nueva integración</h2><p className="voice-notice">Cada clave permite leer únicamente las carpetas que selecciones. El audio y los perfiles de voz permanecen fuera de estas integraciones.</p>
      {fresh ? <div className="voice-integration-secret"><h3>Clave de {fresh.name}</h3><p>Se muestra una vez. Cópiala para configurar tu integración.</p><input className="voice-input" aria-label="Clave de integración recién creada" type="password" readOnly autoComplete="off" value={fresh.token} /><div className="flex gap-2"><Button variant="outline" size="sm" onClick={() => void copy()}><Copy size={14} /> Copiar clave</Button><Button size="sm" onClick={() => { setFresh(null); setToast(''); }}>He guardado la clave</Button></div></div> : owned.length ? <form onSubmit={create}>
        <FormField label="Nombre" hint="Por ejemplo, el producto que consumirá estas reuniones."><Field.Control className="voice-input" value={name} maxLength={200} disabled={creating} onChange={e => setName(e.target.value)} /></FormField>
        <fieldset disabled={creating}><legend>Carpetas permitidas</legend>{owned.map(folder => <label key={folder.id}><input type="checkbox" checked={scopes.includes(folder.id)} onChange={e => setScopes(old => e.target.checked ? [...old, folder.id] : old.filter(id => id !== folder.id))} /><span>{folder.name}</span></label>)}</fieldset>
        <Button className="self-start" type="submit" size="sm" loading={creating} disabled={!name.trim() || !scopes.length}><Plus size={14} /> Crear clave de solo lectura</Button>
      </form> : <div className="voice-integration-empty"><p>Crea una carpeta propia para elegir qué reuniones compartir con una integración.</p><Button size="sm" variant="outline" onClick={onCreateFolder}><Plus size={14} /> Crear carpeta</Button></div>}
    </section>
    <div className="voice-section-label">Tus integraciones</div>
    {loading && <p role="status" className="voice-notice">Consultando integraciones…</p>}
    {!loading && !items.length && !error && <p className="voice-notice">Todavía no has creado ninguna integración.</p>}
    <div className="voice-integration-list">{items.map(item => <article key={item.id}><div className="voice-processing-heading"><strong>{item.name}</strong><span>{item.revoked_at ? 'Revocada' : 'Solo lectura'}</span></div><p>{item.folders.length ? item.folders.map(folder => folder.name).join(' · ') : 'Sin carpetas disponibles'}</p><small>Creada {new Date(item.created_at).toLocaleString('es-ES')}</small>
      {!item.revoked_at && (confirm === item.id ? <div className="voice-processing-confirm"><p>¿Revocar el acceso de esta integración a sus carpetas?</p><div className="flex gap-2"><Button size="sm" variant="outline" loading={busy === item.id} onClick={() => void revoke(item.id)}>Revocar acceso</Button><Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setConfirm(null)}>Volver</Button></div></div> : <Button className="mt-3" size="sm" variant="ghost" disabled={busy !== null} onClick={() => setConfirm(item.id)}>Revocar</Button>)}
    </article>)}</div>
  </div>;
}
