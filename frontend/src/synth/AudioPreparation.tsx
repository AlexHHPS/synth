'use client';
import { brand } from './brand';
import { useEffect, useState } from 'react';
import { Download, RefreshCw } from 'lucide-react';
import { Button } from './design-system/button';
import { pipelineRequest } from './api';

export interface AudioSetup { state: 'checking' | 'preparing' | 'failed' | 'ready'; step: number; message: string }
export function useAudioSetup() {
  const [setup,setSetup]=useState<AudioSetup>({state:'checking',step:0,message:'Conectando con el audio de este Mac…'});
  const [reload,setReload]=useState(0);
  useEffect(()=>{
    let active=true;let timer:ReturnType<typeof setTimeout>;
    async function load(){
      let done=false;
      try{
        const status=await pipelineRequest<{audio_setup?:AudioSetup}>('GET','/v1/status');
        if(!active)return;
        if(status.audio_setup){setSetup(status.audio_setup);done=status.audio_setup.state==='ready';}
        else setSetup({state:'failed',step:0,message:`El servicio anterior sigue abierto. Cuando termine de procesar, cierra ${brand.name} y vuelve a abrirla.`});
      }catch{if(active)setSetup({state:'checking',step:0,message:'Esperando al servicio de audio local…'});}
      finally{if(active&&!done)timer=setTimeout(()=>void load(),2000);}
    }
    void load();return()=>{active=false;clearTimeout(timer);};
  },[reload]);
  async function retry(){
    setSetup({state:'checking',step:0,message:'Reintentando la preparación…'});
    try{await pipelineRequest('POST','/v1/audio/setup/retry');}
    catch{setSetup({state:'failed',step:0,message:`Cierra ${brand.name} y vuelve a abrirla para actualizar el servicio.`});}
    finally{setReload(value=>value+1);}
  }
  return {setup,retry};
}

export function AudioPreparation({setup,onRetry}:{setup:AudioSetup;onRetry:()=>Promise<void>}){
  if(setup.state==='ready')return null;
  return <section className="voice-audio-preparation" aria-label="Preparación automática del audio">
    <Download size={24}/><h2>Preparando el audio de tu Mac</h2>
    <p>Se reutilizan los modelos descargados. Una instalación nueva descarga unos 5,9 GB. Podrás grabar cuando termine.</p>
    <progress aria-label="Progreso de la preparación" max={3} value={setup.step}/>
    <p role={setup.state==='failed'?'alert':'status'}>{setup.message}</p>
    {setup.state==='failed'&&<Button variant="outline" onClick={()=>void onRetry()}><RefreshCw size={14}/> Reintentar preparación</Button>}
  </section>;
}
