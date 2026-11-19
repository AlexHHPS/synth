'use client';
import { brand } from './brand';

import { createContext, useCallback, useEffect, useRef, useState } from 'react';
import { SignInView, type AuthState } from './SignInView';
import { LogOut, ShieldCheck } from 'lucide-react';
import { Button } from './design-system/button';
import { pipelineRequest, message } from './api';
import { AudioPreparation, useAudioSetup } from './AudioPreparation';
import { setCaptureIdentity } from './capture';
import { invoke } from '@tauri-apps/api/core';

/** True while a meeting is being recorded; false when the native side cannot say. */
const recordingNow = () => invoke<boolean>('is_recording').catch(() => false);

export const SessionEmail = createContext<string | undefined>(undefined);

export function CorporateSession({ children }: { children: React.ReactNode }) {
  const {setup,retry}=useAudioSetup();
  const [session,setSession]=useState<AuthState|null>(null);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [busy,setBusy]=useState(false);
  const [attempts,setAttempts]=useState(0);
  const generation=useRef(0);
  const inFlight=useRef(0);
  const mounted=useRef(true);
  const signedIn=useRef(false); signedIn.current=!!session?.signed_in;
  const refresh=useCallback(async()=>{
    const request=++generation.current;
    inFlight.current++;
    try {
      const next = await pipelineRequest<AuthState>('GET','/v1/auth/state');
      if (!mounted.current || generation.current !== request) return;
      if (!next.signed_in && signedIn.current && await recordingNow()) {
        // A session that lapses mid-meeting must not tear down the capture
        // screen: the audio keeps being saved and is processed after signing in.
        setNotice('Tu sesión ha caducado. La grabación continúa; inicia sesión cuando termines para procesarla.');
        return;
      }
      setCaptureIdentity(next.signed_in ? next.principal_id ?? (next.mode === 'keys' ? 'local-keys' : null) : null);
      setSession(next); setError(''); setNotice(''); setAttempts(0);
    }
    catch(e) { if (mounted.current && generation.current === request) { setError(message(e)); setAttempts(n=>n+1); } }
    finally { inFlight.current--; }
  },[]);
  useEffect(()=>{
    mounted.current=true;
    const focus=()=>{void refresh();};
    window.addEventListener('focus',focus);
    return()=>{mounted.current=false;generation.current++;setCaptureIdentity(null);window.removeEventListener('focus',focus);};
  },[refresh]);
  useEffect(()=>{void refresh();},[refresh]);
  useEffect(()=>{
    if(session && (session.mode!=='supabase'||session.signed_in))return;
    // The first request can precede the LaunchAgent. Keep recovering while no
    // session exists, but never overlap a slow automatic auth request.
    const timer=setInterval(()=>{if(inFlight.current===0)void refresh();},2500);return()=>clearInterval(timer);
  },[session?.mode,session?.signed_in,refresh]);
  async function login(){
    setBusy(true);setError('');
    try {await pipelineRequest('POST','/v1/auth/login');await refresh();}
    catch(e){setError(message(e));}finally{setBusy(false);}
  }
  async function logout(){
    if(await recordingNow()){setNotice('Termina la grabación antes de cerrar sesión.');return;}
    setNotice('');
    generation.current++;
    setBusy(true);setError('');
    try{await pipelineRequest('POST','/v1/auth/logout');}
    catch(e){setError(message(e));}finally{
      // A failed cloud refresh after local logout must not keep private views open.
      generation.current++;setCaptureIdentity(null);setSession(null);
      await refresh();setBusy(false);
    }
  }
  if(session?.signed_in)return <div className="voice-session-shell" key={session.principal_id ?? session.mode}>
    {notice&&<div className="voice-alert" role="alert">{notice}</div>}
    {session.mode==='supabase'&&<div className="voice-session-bar"><ShieldCheck size={14}/><span>{session.email}</span><Button variant="ghost" size="xs" disabled={busy} onClick={logout}><LogOut size={14}/> Cerrar sesión</Button></div>}
    <SessionEmail.Provider value={session.email}>{setup.state==='ready'?children:<main className="voice-audio-welcome"><AudioPreparation setup={setup} onRetry={retry}/></main>}</SessionEmail.Provider>
  </div>;
  async function retrySession(){setBusy(true);setAttempts(0);await refresh();setBusy(false);}
  return <SignInView session={session} error={error} attempts={attempts} busy={busy} onSignIn={()=>void login()} onRetry={()=>void retrySession()}/>;
}
