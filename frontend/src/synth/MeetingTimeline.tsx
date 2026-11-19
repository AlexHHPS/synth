'use client';

import { useId } from 'react';
import { FileText } from 'lucide-react';
import type { Folder, Meeting } from './api';
import { dateLabel, localDay } from './meetingDates';

const stateLabels: Record<string, string> = {
  capturing: 'Captura', queued: 'En cola', transcribing: 'Transcribiendo', processing: 'Procesando',
  completed: 'Acta disponible', ready: 'Disponible', failed: 'Requiere atención', cancelled: 'Cancelada',
};

export const MEETING_PAGE_SIZE = 100;
const meetingTimeFormat = new Intl.DateTimeFormat('es-ES', {
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

export function MeetingTimeline({ meetings, folders, onOpen }: {
  meetings: Meeting[];
  folders: Folder[];
  onOpen: (id: string) => void;
}) {
  const idPrefix = useId();
  const today = new Date();
  const names = new Map(folders.map(folder => [folder.id, folder.name]));
  const entries = meetings.map(meeting => ({
    meeting, timestamp: Date.parse(meeting.created_at ?? ''),
  }));
  entries.sort((a, b) => (Number.isNaN(b.timestamp) ? -Infinity : b.timestamp)
    - (Number.isNaN(a.timestamp) ? -Infinity : a.timestamp));
  const groups = new Map<string, typeof entries>();
  for (const entry of entries) {
    const day = Number.isNaN(entry.timestamp) ? '' : localDay(new Date(entry.timestamp).toISOString());
    const group = groups.get(day);
    if (group) group.push(entry);
    else groups.set(day, [entry]);
  }

  if (!meetings.length) return null;

  return <div className="voice-meeting-timeline" data-slot="meeting-timeline">
    {[...groups].map(([day, group]) => <section key={day} className="voice-meeting-day" aria-labelledby={idPrefix + '-' + day}>
      <h2 id={idPrefix + '-' + day}>{day ? <time dateTime={day}>{dateLabel(day, today)}</time> : 'Sin fecha'}</h2>
      <ul role="list">
        {group.map(({ meeting, timestamp }) => <li key={meeting.id}>
          <button type="button" className="voice-meeting-row" onClick={() => onOpen(meeting.id)}>
            <span className="voice-row-icon" aria-hidden="true"><FileText size={18} /></span>
            <span className="voice-row-content">
              <strong>{meeting.title}</strong>
              <span className="voice-row-meta">
                <small>{names.get(meeting.folder_id ?? '') ?? 'Privada · Sin carpeta'}</small>
                <span className="voice-state" data-state={meeting.state}>{stateLabels[meeting.state] ?? meeting.state}</span>
              </span>
            </span>
            {!Number.isNaN(timestamp) && <time dateTime={meeting.created_at}>{meetingTimeFormat.format(timestamp)}</time>}
          </button>
        </li>)}
      </ul>
    </section>)}
  </div>;
}
