'use client'

import Link from 'next/link'
import { FormEvent, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowRight,
  BookOpen,
  CalendarClock,
  GraduationCap,
  Loader2,
  Plus,
  Sparkles,
  Target,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { coursesApi, type Course, type CreateCourseInput } from '@/lib/courses-api'

const COURSE_PAGE_SIZE = 50

interface PendingRequest {
  fingerprint: string
  requestKey: string
}

function normalizedFingerprint(input: CreateCourseInput): string {
  const normalize = (value = '') => value.trim().replace(/\s+/g, ' ')
  return JSON.stringify({
    title: normalize(input.title),
    description: normalize(input.description),
    unit_title: normalize(input.unit_title || 'Unit 1'),
  })
}

export default function CoursesPage() {
  const { t } = useTranslation()
  const router = useRouter()
  const [courses, setCourses] = useState<Course[]>([])
  const [total, setTotal] = useState(0)
  const [nextOffset, setNextOffset] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [creating, setCreating] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [error, setError] = useState('')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [unitTitle, setUnitTitle] = useState('Unit 1')
  const pendingRequest = useRef<PendingRequest | null>(null)

  useEffect(() => {
    let active = true
    coursesApi
      .list(COURSE_PAGE_SIZE, 0)
      .then(result => {
        if (active) {
          setCourses(result.courses)
          setTotal(result.total)
          setNextOffset(result.next_offset)
        }
      })
      .catch((reason: unknown) => {
        if (active)
          setError(reason instanceof Error ? reason.message : t('Could not load courses.'))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [t])

  const draftCount = total

  async function handleLoadMore() {
    if (nextOffset === null || loadingMore) return
    setLoadingMore(true)
    setError('')
    try {
      const result = await coursesApi.list(COURSE_PAGE_SIZE, nextOffset)
      setCourses(current => {
        const known = new Set(current.map(course => course.id))
        return [...current, ...result.courses.filter(course => !known.has(course.id))]
      })
      setTotal(result.total)
      setNextOffset(result.next_offset)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('Could not load courses.'))
    } finally {
      setLoadingMore(false)
    }
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const input: CreateCourseInput = {
      title,
      description,
      unit_title: unitTitle,
    }
    const fingerprint = normalizedFingerprint(input)
    const existing = pendingRequest.current
    const requestKey =
      existing?.fingerprint === fingerprint ? existing.requestKey : crypto.randomUUID()
    pendingRequest.current = { fingerprint, requestKey }

    setCreating(true)
    setError('')
    try {
      const result = await coursesApi.create(input, requestKey)
      pendingRequest.current = null
      router.push(`/courses/${encodeURIComponent(result.course.id)}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('Could not create course.'))
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="h-full overflow-y-auto bg-[var(--background)]">
      <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8 lg:px-10">
        <header className="mb-8 flex flex-col justify-between gap-5 sm:flex-row sm:items-end">
          <div>
            <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-[var(--primary)]">
              <GraduationCap size={15} />
              {t('Course Mode')}
            </div>
            <h1 className="text-3xl font-semibold tracking-tight text-[var(--foreground)]">
              {t('Your courses')}
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--muted-foreground)]">
              {t('Build durable learning plans that keep mastery and scheduling visible.')}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowCreate(open => !open)}
            aria-expanded={showCreate}
            aria-controls="new-course-form"
            className="inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-[var(--primary)] px-4 text-sm font-medium text-[var(--primary-foreground)] shadow-sm transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2"
          >
            <Plus size={16} />
            {t('New course')}
          </button>
        </header>

        <section aria-label={t('Course Mode')} className="mb-7 grid gap-3 sm:grid-cols-3">
          <Metric icon={<BookOpen size={16} />} label={t('Courses')} value={total} />
          <Metric icon={<Sparkles size={16} />} label={t('Drafts')} value={draftCount} />
          <Metric icon={<Target size={16} />} label={t('Mastery tracking')} value="—" />
        </section>

        {showCreate && (
          <form
            id="new-course-form"
            onSubmit={handleCreate}
            className="mb-8 grid gap-4 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 shadow-sm"
          >
            <div>
              <h2 className="font-semibold text-[var(--foreground)]">
                {t('Create a draft course')}
              </h2>
              <p className="mt-1 text-xs text-[var(--muted-foreground)]">
                {t('Start with one unit. You can shape the course in later steps.')}
              </p>
            </div>
            <label className="grid gap-1.5 text-xs font-medium text-[var(--foreground)]">
              {t('Course title')}
              <input
                required
                maxLength={200}
                value={title}
                onChange={event => setTitle(event.target.value)}
                className="h-10 rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 text-sm outline-none focus-visible:border-[var(--primary)] focus-visible:ring-2 focus-visible:ring-[var(--primary)]/30"
                placeholder={t('e.g. Foundations of statistics')}
              />
            </label>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="grid gap-1.5 text-xs font-medium text-[var(--foreground)]">
                {t('First unit')}
                <input
                  required
                  maxLength={200}
                  value={unitTitle}
                  onChange={event => setUnitTitle(event.target.value)}
                  className="h-10 rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 text-sm outline-none focus-visible:border-[var(--primary)] focus-visible:ring-2 focus-visible:ring-[var(--primary)]/30"
                />
              </label>
              <label className="grid gap-1.5 text-xs font-medium text-[var(--foreground)]">
                {t('Description')}
                <input
                  maxLength={2000}
                  value={description}
                  onChange={event => setDescription(event.target.value)}
                  className="h-10 rounded-lg border border-[var(--border)] bg-[var(--background)] px-3 text-sm outline-none focus-visible:border-[var(--primary)] focus-visible:ring-2 focus-visible:ring-[var(--primary)]/30"
                  placeholder={t('What do you want to learn?')}
                />
              </label>
            </div>
            <div className="flex items-center justify-between gap-4">
              <p className="text-xs text-[var(--muted-foreground)]">
                {t('A failed network request can be retried safely.')}
              </p>
              <button
                type="submit"
                disabled={creating}
                className="inline-flex h-9 items-center gap-2 rounded-lg bg-[var(--foreground)] px-4 text-xs font-medium text-[var(--background)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2 disabled:opacity-50"
              >
                {creating && <Loader2 size={14} className="animate-spin" />}
                {t('Create draft')}
              </button>
            </div>
          </form>
        )}

        {error && (
          <div
            role="alert"
            aria-live="assertive"
            className="mb-5 rounded-lg border border-rose-500/20 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-300"
          >
            {error}
          </div>
        )}

        {loading ? (
          <div role="status" aria-live="polite" className="flex items-center justify-center gap-2 py-20 text-sm text-[var(--muted-foreground)]">
            <Loader2 size={16} className="animate-spin" /> {t('Loading courses…')}
          </div>
        ) : courses.length === 0 ? (
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="flex w-full flex-col items-center rounded-2xl border border-dashed border-[var(--border)] px-6 py-16 text-center transition-colors hover:bg-[var(--secondary)]/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2"
          >
            <GraduationCap size={30} className="mb-3 text-[var(--primary)]" />
            <span className="font-medium text-[var(--foreground)]">
              {t('Create your first course')}
            </span>
            <span className="mt-1 text-sm text-[var(--muted-foreground)]">
              {t('Every course begins as a draft with one unit.')}
            </span>
          </button>
        ) : (
          <div>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {courses.map(course => (
                <Link
                  key={course.id}
                  href={`/courses/${encodeURIComponent(course.id)}`}
                  className="group min-w-0 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5 shadow-sm transition-all hover:-translate-y-0.5 hover:border-[var(--primary)]/40 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2"
                >
                  <div className="mb-6 flex items-start justify-between gap-4">
                    <span className="rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
                      {t('Draft')}
                    </span>
                    <ArrowRight
                      size={16}
                      className="text-[var(--muted-foreground)] transition-transform group-hover:translate-x-1"
                    />
                  </div>
                  <h2 className="font-semibold text-[var(--foreground)]">{course.title}</h2>
                  <p className="mt-2 line-clamp-2 min-h-10 text-sm leading-5 text-[var(--muted-foreground)]">
                    {course.description || t('No description yet.')}
                  </p>
                  <div className="mt-5 flex items-center justify-between border-t border-[var(--border)] pt-4 text-xs text-[var(--muted-foreground)]">
                    <span className="flex min-w-0 items-center gap-1.5 truncate">
                      <BookOpen size={13} />
                      {course.units[0]?.title}
                    </span>
                    <span className="flex items-center gap-1.5">
                      <CalendarClock size={13} />
                      {t('Not scheduled')}
                    </span>
                  </div>
                </Link>
              ))}
            </div>
            {nextOffset !== null && (
              <div className="mt-6 flex justify-center">
                <button
                  type="button"
                  onClick={handleLoadMore}
                  disabled={loadingMore}
                  className="inline-flex h-10 items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] px-4 text-sm font-medium text-[var(--foreground)] hover:bg-[var(--secondary)]/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--primary)] focus-visible:ring-offset-2 disabled:opacity-50"
                >
                  {loadingMore && <Loader2 size={14} className="animate-spin" />}
                  {t('Load more')}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Metric({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode
  label: string
  value: number | string
}) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--card)]/70 px-4 py-3">
      <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
        {icon}
        {label}
      </div>
      <div className="mt-1 text-xl font-semibold text-[var(--foreground)]">{value}</div>
    </div>
  )
}
