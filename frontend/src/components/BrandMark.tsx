export function BrandMark({ className = '' }: { className?: string }) {
  return <svg className={`brand-mark ${className}`} viewBox="0 0 48 48" fill="none" aria-hidden="true">
    <path d="M5 31 14 26 21 29 27 17 36 14 43 8" stroke="currentColor" strokeWidth="2.2" strokeLinecap="square" />
    <path d="m5 31 5-6 7 1 3 7-7 6-7-2Zm14-4 5-10 8-2 5 6-3 10-9 3Zm12-17 7-7 7 3-1 9-8 4-6-4Z" fill="currentColor" />
    <path d="m10 29 5 1 1 4-4 2Zm16-7 4-3 3 3-2 6-5 1Zm10-12 4-3 3 2-1 4-5 2Z" fill="var(--dust-highlight)" />
  </svg>
}
