import { BrandMark } from '../../components/BrandMark'

export function NotFoundPage({ onHome }: { onHome: () => void }) {
  return <main className="not-found"><div className="not-found-illustration" aria-hidden="true"><BrandMark/><span>404</span></div><div className="eyebrow">Lost signal</div><h1>This route isn't on the map.</h1><p>The page may have moved, or it hasn't been built yet.</p><a href="/" onClick={event => { event.preventDefault(); onHome() }}>Return to workspace</a></main>
}
