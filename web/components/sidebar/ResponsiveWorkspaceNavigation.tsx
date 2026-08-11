'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import WorkspaceSidebar from '@/components/sidebar/WorkspaceSidebar'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export default function ResponsiveWorkspaceNavigation() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const toggleRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  const closeNavigation = useCallback(() => {
    setOpen(false)
    window.requestAnimationFrame(() => toggleRef.current?.focus())
  }, [])

  useEffect(() => {
    if (!open) return
    closeRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeNavigation()
        return
      }
      if (event.key !== 'Tab') return

      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) || []
      ).filter(element => element.getClientRects().length > 0)
      if (focusable.length === 0) {
        event.preventDefault()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [closeNavigation, open])

  return (
    <>
      <header className="flex h-12 shrink-0 items-center border-b border-[var(--border)] bg-[var(--secondary)] px-3 sm:hidden">
        <button
          ref={toggleRef}
          type="button"
          onClick={() => setOpen(true)}
          aria-label={t('Expand sidebar')}
          aria-expanded={open}
          aria-controls="mobile-workspace-navigation"
          className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)]"
        >
          <PanelLeftOpen size={19} />
        </button>
        <span className="ml-2 text-sm font-semibold text-[var(--foreground)]">
          {t('DeepTutor')}
        </span>
      </header>

      <div className="hidden shrink-0 sm:block">
        <WorkspaceSidebar />
      </div>

      {open && (
        <div className="fixed inset-0 z-50 sm:hidden">
          <div
            className="absolute inset-0 bg-black/45"
            aria-hidden="true"
            onClick={closeNavigation}
          />
          <div
            ref={dialogRef}
            id="mobile-workspace-navigation"
            role="dialog"
            aria-modal="true"
            aria-label={t('Workspace navigation')}
            onClick={event => {
              const target = event.target
              if (target instanceof Element && target.closest('a[href]')) closeNavigation()
            }}
            className="relative h-full w-[220px] max-w-[85vw] bg-[var(--secondary)] shadow-xl"
          >
            <button
              ref={closeRef}
              type="button"
              onClick={closeNavigation}
              aria-label={t('Collapse sidebar')}
              className="absolute right-3 top-2.5 z-10 inline-flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--secondary)] text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)]"
            >
              <PanelLeftClose size={18} />
            </button>
            <WorkspaceSidebar forceExpanded />
          </div>
        </div>
      )}
    </>
  )
}
