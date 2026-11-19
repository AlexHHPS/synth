export function dateOf(value: string) {
  return value.length === 10 ? new Date(value + 'T12:00:00') : new Date(value);
}

export function localDay(value: string) {
  return value.length === 10 ? value : new Date(value).toLocaleDateString('sv-SE');
}

export function dateLabel(value: string, today = new Date()) {
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const key = localDay(value);
  if (key === localDay(today.toISOString())) return 'Hoy';
  if (key === localDay(yesterday.toISOString())) return 'Ayer';
  const date = dateOf(value);
  return date.toLocaleDateString('es', {
    weekday: 'short', day: 'numeric', month: 'short',
    year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}
