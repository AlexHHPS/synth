import Image from 'next/image';
import { ExternalLink, ShieldCheck, AudioLines, Cloud } from 'lucide-react';
import { brand } from '@/synth/brand';
import packageInfo from '../../package.json';

export function About() {
  return <article className="mx-auto max-w-3xl space-y-8 text-sm leading-relaxed">
    <header className="flex items-center gap-4">
      <Image src={brand.logo} width={56} height={56} alt="" className="rounded-xl" />
      <div><h2 className="text-3xl font-semibold tracking-tight">{brand.name}</h2>
        <p className="text-muted-foreground">{brand.tagline}</p>
        <p className="mt-1 text-xs text-muted-foreground">Build {packageInfo.version} · Open source</p></div>
    </header>
    <p>Un espacio para tus notas, conversaciones y decisiones. Grabación nativa en tu Mac, con una biblioteca centralizada que administra tu organización.</p>
    <div className="grid gap-4 sm:grid-cols-2">
      <section className="rounded-xl border p-4"><AudioLines size={18} className="mb-3 text-primary" /><h3 className="font-semibold">Audio en tu Mac</h3><p className="mt-1 text-muted-foreground">Transcripción, separación de hablantes y comparación de voces locales. El audio temporal se elimina tras el análisis.</p></section>
      <section className="rounded-xl border p-4"><Cloud size={18} className="mb-3 text-primary" /><h3 className="font-semibold">Conocimiento compartido</h3><p className="mt-1 text-muted-foreground">Texto y perfiles compartidos cifrados en el servicio configurado. Las actas usan el proveedor de IA elegido por tu organización.</p></section>
    </div>
    <section className="rounded-xl border p-4"><h3 className="flex items-center gap-2 font-semibold"><ShieldCheck size={16} /> Privacidad y control</h3><p className="mt-2 text-muted-foreground">Las reuniones son privadas hasta que las compartes. Compartir tu perfil de voz requiere tu aceptación. El reconocimiento propone coincidencias de piloto pendientes de calibración; no sirve para autenticarte.</p></section>
    <nav aria-label="Información del producto" className="flex flex-wrap gap-x-5 gap-y-3">
      {[[brand.documentation_url, 'Documentación'], [brand.source_url, 'Código fuente'], [brand.support_url, 'Soporte y problemas']].map(([url, label]) => <a key={label} href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 font-medium text-primary underline-offset-4 hover:underline">{label}<ExternalLink size={13} /></a>)}
    </nav>
    <details className="rounded-xl border p-4"><summary className="cursor-pointer font-semibold">Licencias y atribuciones</summary>
      <div className="mt-4 space-y-3 text-muted-foreground">
        <p>Basado en Meetily Community Edition, copyright © 2024 Zackriya Solutions, bajo licencia MIT. {brand.name} es un producto derivado independiente y no es una edición oficial de Meetily.</p>
        <p>Las funciones adicionales de biblioteca, API/MCP y perfiles de voz pertenecen a esta implementación. No incluyen ni conceden acceso a Meetily Pro o a sus servicios comerciales.</p>
        <p>FluidAudio: Apache 2.0. Modelos Community-1 seleccionados: CC BY 4.0, con sus atribuciones y procedencia. FFmpeg de esta distribución Apple Silicon: LGPL 2.1 o posterior, invocado como ejecutable separado.</p>
        <p>El paquete contiene los textos completos de las licencias, avisos de terceros y fuentes correspondientes de FFmpeg en Contents/Resources/Notices. Los componentes y modelos conservan sus propias condiciones.</p>
      </div>
    </details>
  </article>;
}
