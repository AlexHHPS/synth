import { brand } from '@/synth/brand';

export function About() {
  return <div className="space-y-4 text-sm"><h2 className="text-xl font-semibold">{brand.name}</h2><p>{brand.tagline}</p><p>Grabación y análisis acústico en tu Mac, con una biblioteca centralizada en el servicio configurado por tu organización.</p><p>Basado en Meetily Community Edition, de Zackriya Solutions. Licencia MIT. Incluye FFmpeg bajo LGPL 2.1 o posterior; el paquete incluye su código fuente y licencia. Los modelos y las dependencias conservan sus propias licencias y atribuciones.</p><p><a href="https://github.com/AlexHHPS/synth" target="_blank" rel="noreferrer" className="underline">Código fuente y documentación</a></p></div>;
}
