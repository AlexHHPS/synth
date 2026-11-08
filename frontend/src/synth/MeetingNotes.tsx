'use client';

import { useEffect, useRef, useState } from 'react';
import { useCreateBlockNote } from '@blocknote/react';
import { BlockNoteView } from '@blocknote/shadcn';
import '@blocknote/shadcn/style.css';

/** Markdown remains the storage format; the editor never receives generated notes. */
export default function MeetingNotes({ value, onChange, label = 'Mis notas', placeholder = 'Escribe tus notas o pulsa «/» para añadir un bloque.' }: {
  value: string; onChange: (markdown: string) => void; label?: string; placeholder?: string;
}) {
  const [mode,setMode] = useState<'light'|'dark'>('light');
  useEffect(() => {
    const root=document.documentElement;
    const update=()=>setMode(root.classList.contains('dark') || root.dataset.mode==='dark' ? 'dark' : 'light');
    update();const observer=new MutationObserver(update);observer.observe(root,{attributes:true,attributeFilter:['class','data-mode']});
    return()=>observer.disconnect();
  },[]);
  const ready = useRef(false), serialized = useRef(value), revision = useRef(0);
  const callback = useRef(onChange); callback.current = onChange;
  const editor = useCreateBlockNote({
    placeholders: { default: placeholder },
    _tiptapOptions: { editorProps: { attributes: { 'aria-label': label, role: 'textbox', 'aria-multiline': 'true' } } },
  });
  useEffect(() => () => { revision.current++; ready.current = false; }, []);
  useEffect(() => {
    let active = true;
    if (ready.current && value === serialized.current) return;
    const run = ++revision.current;
    ready.current = false;
    void editor.tryParseMarkdownToBlocks(value).then(blocks => {
      if (!active || revision.current !== run) return;
      editor.replaceBlocks(editor.document, blocks.length ? blocks : [{type:'paragraph'}]);
      serialized.current = value; ready.current = true;
    });
    return () => { active = false; };
  }, [editor, value]);
  useEffect(() => editor.onChange(() => {
    if (!ready.current) return;
    const run = ++revision.current;
    void editor.blocksToMarkdownLossy(editor.document).then(markdown => {
      if (run !== revision.current) return;
      serialized.current = markdown;
      callback.current(markdown);
    });
  }), [editor]);
  return <div className="meeting-notes" data-slot="meeting-notes"><BlockNoteView editor={editor} theme={mode} /></div>;
}
