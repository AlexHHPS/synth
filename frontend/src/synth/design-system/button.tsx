import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { Loader2 } from 'lucide-react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

export const buttonVariants = cva('inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4', {
  variants: {
    variant: {
      default: 'bg-primary text-primary-foreground hover:bg-primary/90',
      fancy: 'bg-brand text-brand-foreground hover:bg-brand/90',
      destructive: 'bg-destructive text-destructive-foreground hover:bg-destructive/90',
      outline: 'border border-border bg-card hover:bg-muted',
      secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
      ghost: 'hover:bg-muted', link: 'underline-offset-4 hover:underline',
    },
    size: { default: 'h-9 px-4 text-sm', sm: 'h-8 px-3 text-xs', xs: 'h-7 px-2 text-xs', lg: 'h-11 px-6', icon: 'h-9 w-9' },
  }, defaultVariants: { variant: 'default', size: 'default' },
});
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> { loading?: boolean; asChild?: boolean }
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({ children, className, variant, size, loading = false, disabled, asChild = false, ...rest }, ref) {
  const Element = asChild ? Slot : 'button';
  return <Element ref={ref} type="button" className={cn(buttonVariants({variant,size}),className)} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>{loading && !asChild && <Loader2 className="animate-spin" aria-hidden="true"/>}{children}</Element>;
});
