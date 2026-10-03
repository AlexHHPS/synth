import type { ReactElement, ReactNode } from 'react';
import * as Primitive from '@radix-ui/react-tooltip';
export interface TooltipProps { children: ReactElement; content: ReactNode; description?: ReactNode; arrow?: boolean; delay?: number; side?: 'top'|'bottom'|'left'|'right'; closeOnClick?: boolean }
export function TooltipProvider({children}:{children:ReactNode}) { return <Primitive.Provider delayDuration={200}>{children}</Primitive.Provider>; }
export function Tooltip({children,content,description,arrow=false,delay=200,side='top'}:TooltipProps) {
  return <Primitive.Root delayDuration={delay}><Primitive.Trigger asChild>{children}</Primitive.Trigger><Primitive.Portal><Primitive.Content side={side} sideOffset={8} className="z-50 max-w-xs rounded-md bg-foreground px-3 py-2 text-xs text-background shadow-md">{content}{description && <p className="mt-1 opacity-80">{description}</p>}{arrow && <Primitive.Arrow className="fill-foreground"/>}</Primitive.Content></Primitive.Portal></Primitive.Root>;
}
