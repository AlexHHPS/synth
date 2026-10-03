import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';
export function CardHeader({title,description,icon:Icon,actions}:{title:ReactNode;description?:ReactNode;icon?:LucideIcon;actions?:ReactNode}) {
  return <header className="voice-card-header">{Icon && <span className="voice-featured-icon"><Icon size={18} aria-hidden="true"/></span>}<div className="voice-card-header-copy"><h2>{title}</h2>{description && <p>{description}</p>}</div>{actions && <div className="voice-card-header-actions">{actions}</div>}</header>;
}
