'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useEffect, useState } from 'react'
import { ArrowLeft, BookOpen, CalendarClock, Loader2, Target } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { coursesApi, type Course } from '@/lib/courses-api'

export default function CourseDetailPage() {
  const { t } = useTranslation()
  const params = useParams<{ courseId: string }>()
  const [course, setCourse] = useState<Course | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    coursesApi
      .get(params.courseId)
      .then(result => {
        if (active) setCourse(result.course)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : t('Could not load course.'))
      })
    return () => {
      active = false
    }
  }, [params.courseId, t])

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-md text-center">
          <p className="text-sm text-rose-600 dark:text-rose-300">{error}</p>
          <Link
            href="/courses"
            className="mt-4 inline-block text-sm font-medium text-[var(--primary)]"
          >
            {t('Back to courses')}
          </Link>
        </div>
      </div>
    )
  }

  if (!course) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-[var(--muted-foreground)]">
        <Loader2 size={16} className="animate-spin" />
        {t('Loading course…')}
      </div>
    )
  }

  const unit = course.units[0]
  return (
    <div className="h-full overflow-y-auto bg-[var(--background)]">
      <div className="mx-auto max-w-5xl px-6 py-8 lg:px-10">
        <Link
          href="/courses"
          className="mb-7 inline-flex items-center gap-2 text-sm text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
        >
          <ArrowLeft size={15} />
          {t('All courses')}
        </Link>

        <header className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-6 shadow-sm">
          <div className="mb-4 flex items-center justify-between gap-4">
            <span className="rounded-full bg-amber-500/10 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
              {t('Draft')}
            </span>
            <span className="text-xs text-[var(--muted-foreground)]">
              {t('Course workspace ready')}
            </span>
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-[var(--foreground)]">
            {course.title}
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-[var(--muted-foreground)]">
            {course.description || t('No description yet.')}
          </p>
        </header>

        <div className="mt-6 grid gap-5 lg:grid-cols-[1.35fr_1fr]">
          <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-[var(--foreground)]">
              <BookOpen size={16} />
              {t('Course outline')}
            </div>
            <div className="rounded-xl border border-[var(--border)] bg-[var(--secondary)]/25 p-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[var(--muted-foreground)]">
                {t('Unit 1')}
              </div>
              <div className="mt-1 font-medium text-[var(--foreground)]">
                {unit?.title || t('Untitled unit')}
              </div>
              <p className="mt-2 text-xs leading-5 text-[var(--muted-foreground)]">
                {t('Course planning will add activities and assignments here.')}
              </p>
            </div>
          </section>

          <div className="grid gap-5">
            <Placeholder
              icon={<Target size={17} />}
              title={t('Mastery')}
              text={t('Mastery evidence will appear after learning begins.')}
            />
            <Placeholder
              icon={<CalendarClock size={17} />}
              title={t('Schedule')}
              text={t('No sessions or reviews are scheduled yet.')}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

function Placeholder({
  icon,
  title,
  text,
}: {
  icon: React.ReactNode
  title: string
  text: string
}) {
  return (
    <section className="rounded-2xl border border-dashed border-[var(--border)] bg-[var(--card)]/60 p-5">
      <div className="flex items-center gap-2 text-sm font-semibold text-[var(--foreground)]">
        {icon}
        {title}
      </div>
      <p className="mt-2 text-xs leading-5 text-[var(--muted-foreground)]">{text}</p>
    </section>
  )
}
