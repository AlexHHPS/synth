'use client';
import { brand } from './brand';

import { useCallback, useEffect, useState } from 'react';
import Image from 'next/image';
import { LogOut, ShieldCheck } from 'lucide-react';
import { Button } from './design-system/button';
import { pipelineRequest, message } from './api';

interface Session { mode: 'keys' | 'supabase'; signed_in: boolean; email?: string; allowed_domains?: string[]; flow_state?: string; error_code?: string }

export function CorporateSession({ children }: { children: React.ReactNode }) {
  const [session,setSession]=useState<Session|null>(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const refresh=useCallback(async()=>{
    try { setSession(await pipelineRequest<Session>('GET','/v1/auth/state')); setError(''); }
    catch(e) { setError(message(e)); }
  },[]);
  useEffect(()=>{void refresh();},[refresh]);
  useEffect(()=>{
    if(session?.mode!=='supabase'||session.signed_in)return;
    const timer=setInterval(()=>{void refresh();},2500);return()=>clearInterval(timer);
  },[session?.mode,session?.signed_in,refresh]);
  async function login(){
    setBusy(true);setError('');
    try {await pipelineRequest('POST','/v1/auth/login');await refresh();}
    catch(e){setError(message(e));}finally{setBusy(false);}
  }
  async function logout(){
    setBusy(true);setError('');
    try{await pipelineRequest('POST','/v1/auth/logout');}
    catch(e){setError(message(e));}finally{await refresh();setBusy(false);}
  }
  if(session?.signed_in)return <div className="voice-session-shell">
    {session.mode==='supabase'&&<div className="voice-session-bar"><ShieldCheck size={14}/><span>{session.email}</span><Button variant="ghost" size="xs" disabled={busy} onClick={logout}><LogOut size={14}/> Cerrar sesión</Button></div>}
    {children}
  </div>;
  return <div className="voice-login"><div className="voice-login-card">
    <Image src={brand.logo} width={44} height={44} alt={brand.name}/>
    <p className="voice-eyebrow">{brand.name}</p>
    <h1>Tu espacio de conversaciones</h1>
    <p>Inicia sesión con tu cuenta corporativa para acceder a tus notas y reuniones.</p>
    {session?.allowed_domains&&<p className="voice-login-domain">Acceso para {session.allowed_domains.map(d=>'@'+d).join(', ')}</p>}
    {error&&<div className="voice-alert" role="alert">{error}</div>}
    {session?.flow_state==='failed'&&<div className="voice-alert" role="alert">No se pudo completar el acceso. Comprueba que has elegido tu cuenta corporativa y vuelve a intentarlo.</div>}
    {session?<Button loading={busy} onClick={login}>Continuar con Google</Button>:<Button loading={busy} onClick={refresh}>Comprobar conexión</Button>}
    {session?.flow_state==='waiting'&&<p>Completa el acceso en tu navegador. Esta ventana continuará automáticamente.</p>}
    <p className="voice-login-footnote">El audio se procesa en tu Mac. Tus reuniones son privadas hasta que las compartas.</p>
  </div></div>;
}
