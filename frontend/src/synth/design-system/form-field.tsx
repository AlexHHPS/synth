import type { ReactNode } from 'react';
import { Field } from '@base-ui/react/field';
import { Tooltip } from './tooltip';
import { cn } from '@/lib/utils';
export interface FormFieldProps { children: ReactNode; label?: ReactNode; hint?: ReactNode; error?: ReactNode; help?: ReactNode; required?: boolean; className?: string; labelClassName?: string; nativeControl?: boolean }
export function FormField({children,label,hint,error,help,required,className,labelClassName,nativeControl=true}:FormFieldProps) {
  return <Field.Root invalid={Boolean(error)} className={cn('flex w-full flex-col gap-2',className)}>
    {label && <Field.Label nativeLabel={nativeControl} render={nativeControl?undefined:<div/>} className={cn('flex items-center gap-2 text-sm font-medium',labelClassName)}>{label}{required && <span aria-hidden="true">*</span>}{help && <Tooltip content={help}><span tabIndex={0} aria-label="Ayuda">?</span></Tooltip>}</Field.Label>}
    {children}{error ? <Field.Error match className="text-sm text-destructive">{error}</Field.Error> : hint && <Field.Description className="text-sm text-muted-foreground">{hint}</Field.Description>}
  </Field.Root>;
}
