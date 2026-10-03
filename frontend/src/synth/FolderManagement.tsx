'use client';

import { useEffect, useState } from 'react';
import { Field } from '@base-ui/react/field';
import { Folder, Page, message, request } from './api';
import { Button } from './design-system/button';
import { FormField } from './design-system/form-field';

interface Person { id: string; name: string }
interface Member { principal_id: string; name: string; role: 'reader' | 'editor' }

export function FolderManagement({ folder, onChanged, onDeleted }: {
  folder: Folder; onChanged: () => Promise<void>; onDeleted: () => void;
}) {
  const [name, setName] = useState(folder.name);
  const [members, setMembers] = useState<Member[]>([]);
  const [people, setPeople] = useState<Person[]>([]);
  const [person, setPerson] = useState('');
  const [role, setRole] = useState<'reader' | 'editor'>('reader');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleting, setDeleting] = useState(false);
  useEffect(() => {
    let active = true;
    Promise.all([
      request<Page<Member> & { owner_id: string }>('GET', `/v1/folders/${folder.id}/members`),
      request<Page<Person>>('GET', '/v1/people'),
    ]).then(([access, roster]) => {
      if (!active) return;
      setMembers(access.items); setPeople(roster.items.filter(p => p.id !== access.owner_id));
    }).catch(e => { if (active) setError(message(e)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [folder.id]);
  async function operation(action: () => Promise<void>, success: string) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); setNotice(success); await onChanged(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function share(event: React.FormEvent) {
    event.preventDefault(); if (!person) return;
    await operation(async () => {
      await request('PUT', `/v1/folders/${folder.id}/members`, { principal_id: person, role });
      const access = await request<Page<Member>>('GET', `/v1/folders/${folder.id}/members`);
      setMembers(access.items); setPerson('');
    }, 'Acceso actualizado.');
  }
  return <section className="voice-folder-management" aria-label="Ajustes y acceso de la carpeta">
    <h2>Ajustes de la carpeta</h2>
    {error && <p role="alert" className="voice-alert">{error}</p>}
    {notice && <p role="status" className="voice-success">{notice}</p>}
    <form onSubmit={event => { event.preventDefault(); void operation(async () => { await request('PATCH', `/v1/folders/${folder.id}`, { name: name.trim() }); }, 'Nombre guardado.'); }}>
      <FormField label="Nombre"><Field.Control className="voice-input" value={name} maxLength={200} onChange={event => setName(event.target.value)} /></FormField>
      <Button type="submit" variant="outline" size="sm" loading={busy} disabled={!name.trim() || name.trim() === folder.name}>Guardar nombre</Button>
    </form>
    <div><h3>Personas con acceso</h3><p className="voice-notice">Solo estas personas y el propietario pueden acceder. Las integraciones requieren claves limitadas a carpetas explícitas.</p></div>
    {loading ? <p role="status">Consultando accesos…</p> : members.length ? <ul>{members.map(member => <li key={member.principal_id}><span>{member.name}<small>{member.role === 'editor' ? 'Puede editar' : 'Solo lectura'}</small></span><Button variant="ghost" size="xs" disabled={busy} onClick={() => void operation(async () => { await request('DELETE', `/v1/folders/${folder.id}/members/${member.principal_id}`); setMembers(old => old.filter(m => m.principal_id !== member.principal_id)); }, 'Acceso revocado.')}>Revocar</Button></li>)}</ul> : <p className="voice-notice">Carpeta privada. Todavía no se ha compartido con ninguna persona.</p>}
    <form onSubmit={share}>
      <label>Compartir con<select className="voice-input" aria-label="Persona con quien compartir la carpeta" value={person} onChange={event => setPerson(event.target.value)} disabled={loading || busy}><option value="">Selecciona una persona</option>{people.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <label>Permiso<select className="voice-input" aria-label="Permiso de la carpeta" value={role} onChange={event => setRole(event.target.value as 'reader' | 'editor')} disabled={busy}><option value="reader">Solo lectura</option><option value="editor">Puede editar</option></select></label>
      <Button type="submit" size="sm" loading={busy} disabled={!person || loading}>Compartir</Button>
    </form>
    <div className="voice-folder-delete">{deleting ? <><p>Eliminar la carpeta revoca el acceso compartido. Las reuniones se conservan privadas para sus propietarios.</p><div className="flex gap-2"><Button variant="destructive" size="sm" loading={busy} onClick={() => void operation(async () => { await request('DELETE', `/v1/folders/${folder.id}`); onDeleted(); }, 'Carpeta eliminada.')}>Eliminar carpeta</Button><Button variant="ghost" size="sm" disabled={busy} onClick={() => setDeleting(false)}>Conservar carpeta</Button></div></> : <Button variant="ghost" size="xs" disabled={busy} onClick={() => setDeleting(true)}>Eliminar carpeta…</Button>}</div>
  </section>;
}
