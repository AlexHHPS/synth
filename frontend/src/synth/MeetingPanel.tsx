'use client';

import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, X } from 'lucide-react';
import { Button } from './design-system/button';

/** A nonmodal inspector: opening it never replaces the human's document. */
export function MeetingPanel({ id, title, open, onClose, children, footer, actions }: {
  id: string; title: string; open: boolean; onClose: () => void;
  children: ReactNode; footer?: ReactNode; actions?: ReactNode;
}) {
  const panel=useRef<HTMLElement>(null),close=useRef(onClose);close.current=onClose;
  useEffect(()=>{
    if(!open)return;
    panel.current?.focus({ preventScroll: true });
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')setTimeout(()=>{if(!event.defaultPrevented)close.current();},0);};
    document.addEventListener('keydown',escape);
    return()=>{document.removeEventListener('keydown',escape);document.querySelector<HTMLButtonElement>(`[aria-controls="${id}"]`)?.focus({ preventScroll: true });};
  },[open,id]);
  if(!open||typeof document==='undefined')return null;
  return createPortal(<section ref={panel} id={id} role="dialog" aria-labelledby={`${id}-title`} tabIndex={-1} className="meeting-panel" data-slot="meeting-panel">
      <header className="meeting-panel-head"><h2 id={`${id}-title`}>{title}</h2><div>{actions}
        <Button size="sm" variant="ghost" onClick={onClose} aria-label={`Cerrar ${title.toLocaleLowerCase('es')}`}><X size={18} /></Button>
      </div></header>
      <div className="meeting-panel-body">{children}</div>
      <footer className="meeting-panel-foot"><span>{footer ?? title}</span>
        <Button size="sm" variant="ghost" onClick={onClose} aria-label={`Minimizar ${title.toLocaleLowerCase('es')}`}><ChevronDown size={18} /></Button>
      </footer>
    </section>,document.body);
}
