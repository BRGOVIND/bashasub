import type { ReactNode, SVGProps } from 'react'

type Name = 'grid' | 'orbit' | 'book' | 'search' | 'plus' | 'arrow' | 'play' | 'stop' | 'refresh' | 'close' | 'spark' | 'folder' | 'terminal' | 'external'

const paths: Record<Name, ReactNode> = {
  grid: <><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></>,
  orbit: <><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/><path d="M3 12h4m10 0h4M12 3v4m0 10v4"/></>,
  book: <><path d="M12 6c-2-2-5-2-9-1v14c4-1 7-1 9 1 2-2 5-2 9-1V5c-4-1-7-1-9 1Z"/><path d="M12 6v14"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  plus: <path d="M12 4v16M4 12h16"/>,
  arrow: <><path d="M4 12h16m-7-7 7 7-7 7"/></>,
  play: <path d="m8 5 11 7-11 7V5Z"/>,
  stop: <rect x="6" y="6" width="12" height="12"/>,
  refresh: <><path d="M20 11a8 8 0 1 0-2 6"/><path d="M20 4v7h-7"/></>,
  close: <path d="M5 5 19 19M19 5 5 19"/>,
  spark: <><path d="m12 2 2.2 7.8L22 12l-7.8 2.2L12 22l-2.2-7.8L2 12l7.8-2.2L12 2Z"/></>,
  folder: <path d="M3 6h7l2 3h9v10H3V6Z"/>,
  terminal: <><rect x="2" y="3" width="20" height="18" rx="1"/><path d="m6 8 4 4-4 4m6 0h6"/></>,
  external: <><path d="M13 4h7v7m0-7-9 9"/><path d="M20 14v6H4V4h6"/></>,
}

export function Glyph({ name, ...props }: SVGProps<SVGSVGElement> & { name: Name }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>
}
