'use client';

import { useEffect, useState } from 'react';
import { Fingerprint, RefreshCw } from 'lucide-react';
import { Button } from './design-system/button';
import { CardHeader } from './design-system/card-header';
import { message, pipelineRequest } from './api';

interface Sample { id:string; own:boolean; state:'accepted'|'pending'|'rejected'; created_at:string; origin:string }
interface Status { enabled:boolean; sync_state:string; outbox:Record<string,number>; samples:Sample[] }

export function VoiceLearning() {
  const [status,setStatus]=useState<Status|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{let active=true;void pipelineRequest<Status>('GET','/v1/voice/learning').then(value=>{if(active)setStatus(value);}).catch(failure=>{if(active)setError(message(failure));});return()=>{active=false;};},[]);
  async function run(method:string,path:string,body?:unknown){
    if(busy)return;setBusy(true);setError('');
    try{await pipelineRequest(method,path,body);setStatus(await pipelineRequest<Status>('GET','/v1/voice/learning'));}
    catch(failure){setError(message(failure));}finally{setBusy(false);}
  }
  const pending=status?.samples.filter(sample=>sample.own&&sample.state==='pending')??[];
  const accepted=status?.samples.filter(sample=>sample.own&&sample.state==='accepted')??[];
  return <section className="voice-processing-task" aria-label="Aprendizaje de voces"><CardHeader title="Aprendizaje de voces" description="Huellas adicionales, conservando tu perfil original." icon={Fingerprint} />
    {error&&<p role="alert" className="voice-alert">{error}</p>}
    {!status?<p role="status">Consultando las huellas…</p>:<>
      <label className="voice-learning-toggle"><input type="checkbox" checked={status.enabled} disabled={busy} onChange={event=>void run('POST','/v1/voice/learning/settings',{enabled:event.target.checked})} /><span>Añadir huellas de alta confianza de mis reuniones</span></label>
      <p className="voice-notice">Solo se comparten huellas cifradas. El audio permanece en el Mac.</p>
      <div className="voice-section-actions"><span>{accepted.length} huellas añadidas · {status.outbox.queued??0} por sincronizar</span><Button variant="ghost" size="sm" disabled={busy} onClick={()=>void run('POST','/v1/voice/learning/sync')}><RefreshCw size={15} /> Sincronizar</Button></div>
      {status.sync_state!=='available'&&<p role="status">Las huellas locales se conservarán hasta recuperar la conexión.</p>}
      {pending.length>0&&<div className="voice-learning-proposals"><h3>Propuestas para tu voz</h3><p className="voice-notice">Las aportaciones de otra persona necesitan tu aprobación.</p>{pending.map(sample=><div className="voice-learning-sample" key={sample.id}><span>Huella recibida · {new Date(sample.created_at).toLocaleDateString('es-ES')}</span><div><Button variant="outline" size="sm" disabled={busy} onClick={()=>void run('POST',`/v1/voice/samples/${sample.id}/accept`)}>Añadir a mi perfil</Button><Button variant="ghost" size="sm" disabled={busy} onClick={()=>void run('DELETE',`/v1/voice/samples/${sample.id}`)}>Descartar</Button></div></div>)}</div>}
      {accepted.length>0&&<details><summary>Huellas compartidas</summary>{accepted.map(sample=><div className="voice-learning-sample" key={sample.id}><span>{new Date(sample.created_at).toLocaleDateString('es-ES')} · {sample.origin==='human_confirmation'?'Voz confirmada':'Coincidencia alta'}</span><Button variant="ghost" size="sm" disabled={busy} onClick={()=>void run('DELETE',`/v1/voice/samples/${sample.id}`)}>Retirar huella</Button></div>)}</details>}
    </>}
  </section>;
}
