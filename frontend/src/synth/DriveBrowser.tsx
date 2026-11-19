'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, FileText, FileVideo, Folder, HardDrive, Users } from 'lucide-react';
import { Button } from './design-system/button';
import { message, pipelineRequest, type Page } from './api';

type Location = 'my' | 'shared' | 'drives';
interface DriveItem {
  id: string;
  name: string;
  folder: boolean;
  resource_key: string;
  drive_id: string;
  size: number | null;
  disabled: boolean;
}

const locations = [
  { id: 'my', name: 'Mi unidad', icon: HardDrive },
  { id: 'shared', name: 'Compartidos conmigo', icon: Users },
  { id: 'drives', name: 'Unidades compartidas', icon: Folder },
] as const;

const errors: Record<string, string> = {
  drive_access_denied: 'Tu cuenta no tiene acceso a esta carpeta.',
  auth_identity_changed: 'La cuenta de Google Drive ha cambiado.',
};

function sizeLabel(bytes: number) {
  const large = bytes >= 1024 ** 3;
  return new Intl.NumberFormat('es-ES', {
    style: 'unit', unit: large ? 'gigabyte' : 'megabyte',
    maximumFractionDigits: 1,
  }).format(bytes / (1024 ** (large ? 3 : 2)));
}

function itemKey(item: DriveItem) {
  return item.id + ':' + item.name;
}

export function DriveBrowser({ video, disabled, onSelect, onConnectionExpired }: {
  video: boolean;
  disabled: boolean;
  onSelect: (link: string) => void;
  onConnectionExpired: () => void;
}) {
  const [location, setLocation] = useState<Location>('my');
  const [path, setPath] = useState<DriveItem[]>([]);
  const [items, setItems] = useState<DriveItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<DriveItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const current = path[path.length - 1];
  const root = locations.find(place => place.id === location)!;

  const load = useCallback(async (after?: string) => {
    const run = ++generation.current;
    setLoading(true);
    setError('');
    if (!after) { setItems([]); setCursor(null); setSelected(null); }
    try {
      const page = await pipelineRequest<Page<DriveItem>>('POST', '/v1/drive/browse', {
        location: current ? 'folder' : location,
        video,
        ...(current ? { folder_id: current.id, drive_id: current.drive_id, resource_key: current.resource_key } : {}),
        ...(after ? { cursor: after } : {}),
      });
      if (run !== generation.current) return;
      setItems(before => [...new Map([...before, ...page.items].map(item => [itemKey(item), item])).values()]);
      setCursor(page.next_cursor && page.next_cursor !== after ? page.next_cursor : null);
    } catch (error) {
      if (run !== generation.current) return;
      const code = message(error);
      if (code === 'drive_connection_required' || code === 'auth_identity_changed') onConnectionExpired();
      else setError(errors[code] ?? 'No se pudieron cargar los archivos de Drive.');
    } finally {
      if (run === generation.current) setLoading(false);
    }
  }, [current, location, video, onConnectionExpired]);

  useEffect(() => {
    void load();
    return () => { generation.current++; };
  }, [load]);

  function navigate(next: DriveItem[]) {
    setSelected(null);
    setPath(next);
  }

  function confirm() {
    if (disabled || loading || !selected) return;
    const link = new URL('https://drive.google.com/file/d/' + selected.id + '/view');
    if (selected.resource_key) link.searchParams.set('resourcekey', selected.resource_key);
    onSelect(link.href);
  }

  return <div className="drive-browser" data-slot="drive-browser">
    <div className="drive-locations" role="group" aria-label="Ubicaciones de Google Drive">
      {locations.map(({ id, name, icon: Icon }) => <Button key={id} variant="ghost" size="sm"
        aria-pressed={location === id} disabled={disabled} onClick={() => { setLocation(id); navigate([]); }}>
        <Icon size={15} aria-hidden /> {name}
      </Button>)}
    </div>
    {path.length > 0 && <nav className="drive-path" aria-label="Ruta de Drive">
      <Button size="sm" variant="ghost" disabled={disabled} onClick={() => navigate([])}>{root.name}</Button>
      {path.map((folder, index) => <span key={folder.id + ':' + index}>
        <ChevronRight size={14} aria-hidden />
        {index === path.length - 1 ? <span aria-current="page">{folder.name}</span>
          : <Button size="sm" variant="ghost" disabled={disabled} onClick={() => navigate(path.slice(0, index + 1))}>{folder.name}</Button>}
      </span>)}
    </nav>}
    <div className="drive-entries" aria-busy={loading}>
      {items.length > 0 && <ul aria-label="Archivos y carpetas de Drive" role="list">
        {items.map(item => {
          const Icon = item.folder ? Folder : video ? FileVideo : FileText;
          const isSelected = selected != null && itemKey(selected) === itemKey(item);
          return <li key={itemKey(item)}>
            <button type="button" className="drive-entry" aria-pressed={item.folder ? undefined : isSelected}
              disabled={disabled || loading || item.disabled} onClick={() => item.folder ? navigate([...path, item]) : setSelected(item)}>
              <Icon size={17} aria-hidden />
              <span>{item.name}</span>
              {item.folder ? <ChevronRight size={15} aria-hidden />
                : isSelected ? <Check size={16} aria-hidden />
                : item.disabled ? <small>No disponible</small>
                : item.size != null ? <small>{sizeLabel(item.size)}</small> : null}
            </button>
          </li>;
        })}
      </ul>}
      {loading && <p className="drive-entry-message" role="status">Cargando archivos…</p>}
      {error && <div className="drive-entry-message" role="alert">{error}
        <Button size="sm" variant="ghost" onClick={() => void load()}>Reintentar</Button>
      </div>}
      {!items.length && !loading && !error && !cursor && <p className="drive-entry-message">
        {location === 'drives' && !current ? 'No tienes unidades compartidas.' : 'No hay carpetas ni archivos compatibles aquí.'}
      </p>}
      {cursor && !loading && !error && <Button size="sm" variant="ghost" className="drive-more"
        disabled={disabled} onClick={() => void load(cursor)}>Cargar más</Button>}
    </div>
    <div className="drive-selection">
      {selected && <span>{selected.name}</span>}
      <Button size="sm" variant="outline" disabled={disabled || loading || !selected} onClick={confirm}>
        {video ? 'Elegir vídeo' : 'Elegir adjunto'}
      </Button>
    </div>
  </div>;
}
