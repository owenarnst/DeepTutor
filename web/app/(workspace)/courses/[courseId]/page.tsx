'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, BookOpen, CalendarClock, CheckCircle2, Loader2, RefreshCw, Target } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import {
  coursesApi,
  type Course,
  type CourseJobStatus,
  type CourseManifest,
  type ManifestEntry,
  type ManifestRole,
  type ManifestVisibility,
} from '@/lib/courses-api'

const ROLES: ManifestRole[] = [
  'syllabus',
  'lecture_note',
  'reading',
  'assignment',
  'solution',
  'grading_resource',
  'unknown',
]
const VISIBILITIES: ManifestVisibility[] = ['learner_visible', 'instructor_only']

export default function CourseDetailPage() {
  const { t } = useTranslation()
  const params = useParams<{ courseId: string }>()
  const [course, setCourse] = useState<Course | null>(null)
  const [job, setJob] = useState<Awaited<ReturnType<typeof coursesApi.processing>>['job'] | null>(null)
  const [manifest, setManifest] = useState<CourseManifest | null>(null)
  const [draftEntries, setDraftEntries] = useState<ManifestEntry[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [approving, setApproving] = useState(false)

  async function load() {
    setError('')
    try {
      const courseResult = await coursesApi.get(params.courseId)
      setCourse(courseResult.course)
      const [processingResult, manifestResult] = await Promise.allSettled([
        coursesApi.processing(params.courseId),
        coursesApi.manifest(params.courseId),
      ])
      if (processingResult.status === 'fulfilled') setJob(processingResult.value.job)
      if (manifestResult.status === 'fulfilled') {
        setManifest(manifestResult.value)
        setDraftEntries(manifestResult.value.entries)
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : t('Could not load course.'))
    }
  }

  useEffect(() => {
    void load()
    // The route identity is the only dependency; `load` intentionally reads
    // the current translation function without restarting polling on language
    // changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.courseId])

  useEffect(() => {
    if (!job || (job.status !== 'queued' && job.status !== 'source_processing')) return
    const timer = window.setInterval(async () => {
      try {
        const result = await coursesApi.processing(params.courseId)
        setJob(result.job)
        if (result.job.status === 'awaiting_manifest_review') {
          const nextManifest = await coursesApi.manifest(params.courseId)
          setManifest(nextManifest)
          setDraftEntries(nextManifest.entries)
        }
      } catch {
        // A transient poll failure is shown on the next explicit reload; avoid
        // replacing durable stage state with a guessed client-side error.
      }
    }, 1200)
    return () => window.clearInterval(timer)
  }, [job, params.courseId])

  const blockers = useMemo(() => manifest?.blockers || [], [manifest])

  async function retry() {
    if (!job) return
    setError('')
    try {
      const result = await coursesApi.retry(job.id)
      setJob(result.job)
      if (result.job.status === 'awaiting_manifest_review') {
        const nextManifest = await coursesApi.manifest(params.courseId)
        setManifest(nextManifest)
        setDraftEntries(nextManifest.entries)
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : t('Could not retry course processing.'))
    }
  }

  function updateEntry(id: string, patch: Partial<ManifestEntry>) {
    setDraftEntries(entries => entries.map(entry => (entry.id === id ? { ...entry, ...patch } : entry)))
  }

  async function saveManifest() {
    if (!manifest) return
    setSaving(true)
    setError('')
    try {
      const next = await coursesApi.updateManifest(params.courseId, manifest.revision, draftEntries)
      setManifest(next)
      setDraftEntries(next.entries)
      setJob(current => (current ? { ...current, manifest_revision: next.revision } : current))
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : t('Could not save manifest.'))
    } finally {
      setSaving(false)
    }
  }

  async function approveManifest() {
    if (!manifest || blockers.length > 0) return
    setApproving(true)
    setError('')
    try {
      const next = await coursesApi.approveManifest(params.courseId, manifest.revision)
      setManifest(next)
      setDraftEntries(next.entries)
      setJob(current => (current ? { ...current, status: 'completed', stage: 'completed' } : current))
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : t('Could not approve manifest.'))
    } finally {
      setApproving(false)
    }
  }

  if (error && !course) {
    return (
      <div className="flex h-full items-center justify-center p-6 sm:p-8">
        <div className="max-w-md text-center">
          <p role="alert" aria-live="assertive" className="text-sm text-rose-600 dark:text-rose-300">{error}</p>
          <Link href="/courses" className="mt-4 inline-block rounded text-sm font-medium text-[var(--primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2">
            {t('Back to courses')}
          </Link>
        </div>
      </div>
    )
  }

  if (!course) {
    return (
      <div role="status" aria-live="polite" className="flex h-full items-center justify-center gap-2 text-sm text-[var(--muted-foreground)]">
        <Loader2 size={16} className="animate-spin" /> {t('Loading course…')}
      </div>
    )
  }

  const unit = course.units[0]
  return (
    <div className="h-full overflow-y-auto bg-[var(--background)]">
      <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6 sm:py-8 lg:px-10">
        <Link href="/courses" className="mb-7 inline-flex items-center gap-2 rounded text-sm text-[var(--muted-foreground)] hover:text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2">
          <ArrowLeft size={15} /> {t('All courses')}
        </Link>

        <header className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-6 shadow-sm">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
            <span className="rounded-full bg-amber-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">{t('Draft')}</span>
            <ProcessingStatus job={job} t={t} />
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-[var(--foreground)]">{course.title}</h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-[var(--muted-foreground)]">{course.description || t('No description yet.')}</p>
        </header>

        {error && <div role="alert" aria-live="assertive" className="mt-5 rounded-lg border border-rose-500/20 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">{error}</div>}

        {job?.status === 'failed' && (
          <section aria-labelledby="course-processing-error" className="mt-6 rounded-2xl border border-rose-500/30 bg-rose-500/5 p-5">
            <h2 id="course-processing-error" className="font-semibold text-[var(--foreground)]">{t('Course processing needs attention')}</h2>
            <p className="mt-2 text-sm text-[var(--muted-foreground)]">{t('The source-processing stage failed. You can retry that exact stage.')}</p>
            <p className="mt-2 text-xs text-[var(--muted-foreground)]">{t('Failed stage')}: {job.failed_stage || job.stage} · {job.error_code || t('Unknown error')}</p>
            <button type="button" onClick={retry} className="mt-4 inline-flex h-9 items-center gap-2 rounded-lg bg-[var(--foreground)] px-4 text-xs font-medium text-[var(--background)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2">
              <RefreshCw size={14} /> {t('Retry stage')}
            </button>
          </section>
        )}

        {manifest && manifest.entries.length > 0 && (
          <ManifestReview
            manifest={manifest}
            entries={draftEntries}
            blockers={blockers}
            saving={saving}
            approving={approving}
            onUpdate={updateEntry}
            onSave={saveManifest}
            onApprove={approveManifest}
            t={t}
          />
        )}

        <div className="mt-6 grid gap-5 lg:grid-cols-[1.35fr_1fr]">
          <section aria-labelledby="course-outline-heading" className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
            <h2 id="course-outline-heading" className="mb-4 flex items-center gap-2 text-sm font-semibold text-[var(--foreground)]"><BookOpen size={16} /> {t('Course outline')}</h2>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--secondary)]/25 p-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[var(--muted-foreground)]">{t('Unit 1')}</div>
              <div className="mt-1 font-medium text-[var(--foreground)]">{unit?.title || t('Untitled unit')}</div>
              <p className="mt-2 text-xs leading-5 text-[var(--muted-foreground)]">{t('Course planning will add activities and assignments here.')}</p>
            </div>
          </section>
          <div className="grid gap-5">
            <Placeholder id="mastery-heading" icon={<Target size={17} />} title={t('Mastery')} text={t('Mastery evidence will appear after learning begins.')} />
            <Placeholder id="schedule-heading" icon={<CalendarClock size={17} />} title={t('Schedule')} text={t('No sessions or reviews are scheduled yet.')} />
          </div>
        </div>
      </div>
    </div>
  )
}

function ProcessingStatus({ job, t }: { job: { status: CourseJobStatus } | null; t: (key: string) => string }) {
  if (!job) return <span className="text-xs text-[var(--muted-foreground)]">{t('Course workspace ready')}</span>
  const labels: Record<CourseJobStatus, string> = {
    queued: t('Queued'),
    source_processing: t('Processing sources'),
    awaiting_manifest_review: t('Manifest review required'),
    completed: t('Review complete'),
    failed: t('Processing failed'),
  }
  return <span role="status" className="inline-flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">{job.status === 'completed' ? <CheckCircle2 size={14} /> : job.status !== 'failed' ? <Loader2 size={14} className="animate-spin" /> : null}{labels[job.status]}</span>
}

function ManifestReview({
  manifest,
  entries,
  blockers,
  saving,
  approving,
  onUpdate,
  onSave,
  onApprove,
  t,
}: {
  manifest: CourseManifest
  entries: ManifestEntry[]
  blockers: string[]
  saving: boolean
  approving: boolean
  onUpdate: (id: string, patch: Partial<ManifestEntry>) => void
  onSave: () => void
  onApprove: () => void
  t: (key: string, options?: Record<string, unknown>) => string
}) {
  const roleLabel = (role: ManifestRole) => t(`manifest_role_${role}`)
  const visibilityLabel = (visibility: ManifestVisibility) => t(`manifest_visibility_${visibility}`)
  return (
    <section aria-labelledby="manifest-review-heading" className="mt-6 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 id="manifest-review-heading" className="text-lg font-semibold text-[var(--foreground)]">{t('Source manifest review')}</h2>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">{t('Confirm how each uploaded source should be used before planning begins.')}</p>
        </div>
        <span className="text-xs text-[var(--muted-foreground)]">{t('Revision')} {manifest.revision}</span>
      </div>
      {blockers.length > 0 && <div role="alert" className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-800 dark:text-amber-200"><strong>{t('Review blockers')}</strong><ul className="mt-2 list-disc space-y-1 pl-5">{blockers.map(blocker => <li key={blocker}>{blocker === 'unknown_role' ? t('Every source needs a confirmed role.') : t('Suspected solutions require explicit role and visibility confirmation.')}</li>)}</ul></div>}
      <div className="mt-5 grid gap-3">
        {entries.map(entry => (
          <fieldset key={entry.id} className="rounded-xl border border-[var(--border)] p-4">
            <legend className="max-w-full px-1 text-sm font-medium text-[var(--foreground)]">{entry.display_filename}</legend>
            <p className="mt-1 break-all text-[11px] text-[var(--muted-foreground)]">{t('Original filename')}: {entry.original_filename}</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="grid gap-1 text-xs font-medium text-[var(--foreground)]">{t('Role')}
                <select value={entry.role} onChange={event => onUpdate(entry.id, { role: event.target.value as ManifestRole, role_confirmed: true })} className="h-9 rounded-lg border border-[var(--border)] bg-[var(--background)] px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)]">
                  {ROLES.map(role => <option key={role} value={role}>{roleLabel(role)}</option>)}
                </select>
              </label>
              <label className="grid gap-1 text-xs font-medium text-[var(--foreground)]">{t('Visibility')}
                <select value={entry.visibility} onChange={event => onUpdate(entry.id, { visibility: event.target.value as ManifestVisibility, visibility_confirmed: true })} className="h-9 rounded-lg border border-[var(--border)] bg-[var(--background)] px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)]">
                  {VISIBILITIES.map(visibility => <option key={visibility} value={visibility}>{visibilityLabel(visibility)}</option>)}
                </select>
              </label>
            </div>
            {entry.suspected_solution && <div className="mt-3 grid gap-2 text-xs text-[var(--muted-foreground)]"><label className="flex items-start gap-2"><input type="checkbox" checked={entry.role_confirmed} onChange={event => onUpdate(entry.id, { role_confirmed: event.target.checked })} className="mt-0.5" />{t('I confirm this role for a suspected solution.')}</label><label className="flex items-start gap-2"><input type="checkbox" checked={entry.visibility_confirmed} onChange={event => onUpdate(entry.id, { visibility_confirmed: event.target.checked })} className="mt-0.5" />{t('I confirm this visibility for a suspected solution.')}</label></div>}
          </fieldset>
        ))}
      </div>
      <div className="mt-5 flex flex-wrap items-center justify-end gap-3">
        <button type="button" onClick={onSave} disabled={saving} className="inline-flex h-9 items-center gap-2 rounded-lg border border-[var(--border)] px-4 text-xs font-medium text-[var(--foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] disabled:opacity-50">{saving && <Loader2 size={14} className="animate-spin" />}{t('Save corrections')}</button>
        <button type="button" onClick={onApprove} disabled={approving || blockers.length > 0} className="inline-flex h-9 items-center gap-2 rounded-lg bg-[var(--foreground)] px-4 text-xs font-medium text-[var(--background)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] disabled:opacity-50">{approving && <Loader2 size={14} className="animate-spin" />}{t('Approve manifest')}</button>
      </div>
      {manifest.eligible_for_planning && <div role="status" className="mt-4 flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800 dark:text-emerald-200"><CheckCircle2 size={17} className="mt-0.5 shrink-0" /><span>{t('Ready for planning in OWE-8 / OWE-9. Course Mode does not generate a plan.')}</span></div>}
    </section>
  )
}

function Placeholder({ id, icon, title, text }: { id: string; icon: React.ReactNode; title: string; text: string }) {
  return <section aria-labelledby={id} className="rounded-2xl border border-dashed border-[var(--border)] bg-[var(--card)]/60 p-5"><h2 id={id} className="flex items-center gap-2 text-sm font-semibold text-[var(--foreground)]">{icon}{title}</h2><p className="mt-2 text-xs leading-5 text-[var(--muted-foreground)]">{text}</p></section>
}
