import { useEffect, useRef, useState } from 'react'
import { Glyph } from '../components/Glyph'

export interface Command { id: string; label: string; group: string; run: () => void; disabled?: boolean }

export function CommandPalette({ open, onClose, commands }: { open: boolean; onClose: () => void; commands: Command[] }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    const node = dialog.current
    if (!node) return
    if (open && !node.open) { node.showModal(); setQuery('') }
    if (!open && node.open) node.close()
  }, [open])

  const results = commands.filter(command => `${command.label} ${command.group}`.toLowerCase().includes(query.toLowerCase()))
  return <dialog ref={dialog} className="command-dialog" onClose={onClose} onCancel={onClose} aria-label="Command menu">
    <div className="command-search"><Glyph name="search"/><input autoFocus aria-label="Search commands" placeholder="Where shall we go?" value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => {
      if (event.key === 'Enter') { const first = results.find(command => !command.disabled); if (first) { first.run(); onClose() } }
    }}/><kbd>ESC</kbd></div>
    <div className="command-list">{results.length ? results.map(command => <button key={command.id} disabled={command.disabled} onClick={() => { command.run(); onClose() }}><span>{command.label}</span><small>{command.group}{command.disabled ? ' · unavailable' : ''}</small></button>) : <p>No matching commands.</p>}</div>
  </dialog>
}
