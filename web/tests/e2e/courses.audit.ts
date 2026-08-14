import { expect, test, type Page } from '@playwright/test'
import type { CourseManifest, CourseProcessingJob } from '../../lib/courses-api'

const COURSE_ID = '11111111-1111-4111-8111-111111111111'

const course = {
  id: COURSE_ID,
  title: 'Cell Biology',
  description: 'Learn how cells are organized.',
  status: 'draft',
  workspace_ref: `courses/${COURSE_ID}`,
  created_at: '2026-08-06T12:00:00Z',
  updated_at: '2026-08-06T12:00:00Z',
  desired_outcome: 'Explain how cells are organized.',
  weekly_minutes: 120,
  ocw_url: 'https://ocw.mit.edu/courses/7-01sc-fundamentals-of-biology-fall-2011/',
  scheduling: null,
  difficulty: null,
  accessibility: null,
  processing_job_id: '33333333-3333-4333-8333-333333333333',
  units: [
    {
      id: '22222222-2222-4222-8222-222222222222',
      course_id: COURSE_ID,
      title: 'The cell',
      position: 0,
    },
  ],
}

const processingJob = {
  id: '33333333-3333-4333-8333-333333333333',
  course_id: COURSE_ID,
  status: 'awaiting_manifest_review',
  stage: 'awaiting_manifest_review',
  failed_stage: null,
  error_code: null,
  attempt_count: 1,
  manifest_revision: 1,
  created_at: '2026-08-06T12:00:00Z',
  updated_at: '2026-08-06T12:00:00Z',
}

const manifest = {
  course_id: COURSE_ID,
  revision: 1,
  entries: [
    {
      id: '44444444-4444-4444-8444-444444444444',
      course_id: COURSE_ID,
      source_id: '55555555-5555-4555-8555-555555555555',
      original_filename: 'lecture-notes.txt',
      display_filename: 'lecture-notes.txt',
      role: 'lecture_note',
      visibility: 'learner_visible',
      suspected_solution: false,
      role_confirmed: true,
      visibility_confirmed: true,
      created_at: '2026-08-06T12:00:00Z',
      updated_at: '2026-08-06T12:00:00Z',
    },
  ],
  blockers: [],
  eligible_for_planning: true,
}

function observeUnexpectedConsole(page: Page): string[] {
  const messages: string[] = []
  page.on('console', message => {
    if (message.type() === 'error' || message.type() === 'warning') {
      messages.push(`${message.type()}: ${message.text()}`)
    }
  })
  page.on('pageerror', error => messages.push(`pageerror: ${error.message}`))
  return messages
}

async function mockWorkspaceShellApi(page: Page) {
  await page.route('**/api/v1/auth/status', route =>
    route.fulfill({ json: { enabled: false, authenticated: true } })
  )
  await page.route('**/api/v1/sessions**', route =>
    route.fulfill({ json: { sessions: [] } })
  )
  await page.route('**/api/v1/settings**', route =>
    route.fulfill({ json: { catalog: {} } })
  )
}

async function mockCourseApi(page: Page, listedCourses = [course]) {
  await mockWorkspaceShellApi(page)
  await page.route('**/api/v1/courses**', async route => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (request.method() === 'GET' && pathname.endsWith('/processing')) {
      await route.fulfill({ json: { job: processingJob } })
      return
    }
    if (request.method() === 'GET' && pathname.endsWith('/manifest')) {
      await route.fulfill({ json: manifest })
      return
    }
    if (request.method() === 'GET' && pathname.endsWith(`/${COURSE_ID}`)) {
      await route.fulfill({ json: { course } })
      return
    }
    if (request.method() === 'GET') {
      await route.fulfill({
        json: {
          courses: listedCourses,
          total: listedCourses.length,
          has_more: false,
          next_offset: null,
        },
      })
      return
    }
    await route.fallback()
  })
}

test.describe('Course Mode workflow', () => {
  test('reviews blockers, survives a stale save, approves, reloads, and renders EN/ZH', async ({
    page,
  }, testInfo) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await mockWorkspaceShellApi(page)

    let staleSaveCount = 2
    const saveRevisions: number[] = []
    let currentJob = { ...processingJob }
    let currentManifest = {
      ...manifest,
      entries: [
        {
          ...manifest.entries[0],
          id: '44444444-4444-4444-8444-444444444444',
          source_id: '55555555-5555-4555-8555-555555555555',
          original_filename: 'course-materials.pdf',
          display_filename: 'course-materials.pdf',
          role: 'unknown' as const,
          visibility: 'learner_visible' as const,
          suspected_solution: false,
          role_confirmed: false,
          visibility_confirmed: true,
        },
        {
          ...manifest.entries[0],
          id: '44444444-4444-4444-8444-444444444445',
          source_id: '55555555-5555-4555-8555-555555555556',
          original_filename: 'answer-key.pdf',
          display_filename: 'answer-key.pdf',
          role: 'solution' as const,
          visibility: 'instructor_only' as const,
          suspected_solution: true,
          role_confirmed: false,
          visibility_confirmed: false,
        },
      ],
      blockers: ['unknown_role', 'suspected_solution_confirmation'],
      eligible_for_planning: false,
    }

    await page.route('**/api/v1/courses**', async route => {
      const request = route.request()
      const { pathname } = new URL(request.url())
      if (request.method() === 'GET' && pathname.endsWith('/processing')) {
        await route.fulfill({ json: { job: currentJob } })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/manifest')) {
        await route.fulfill({ json: currentManifest })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith(`/${COURSE_ID}`)) {
        await route.fulfill({ json: { course } })
        return
      }
      if (request.method() === 'PATCH' && pathname.endsWith('/manifest')) {
        const body = request.postDataJSON() as {
          revision: number
          entries: Array<Record<string, unknown> & { id: string }>
        }
        const allowedEntryFields = new Set([
          'id',
          'role',
          'visibility',
          'role_confirmed',
          'visibility_confirmed',
        ])
        const unknownFields = body.entries.flatMap(entry =>
          Object.keys(entry).filter(field => !allowedEntryFields.has(field))
        )
        if (unknownFields.length > 0) {
          await route.fulfill({
            status: 422,
            contentType: 'application/json',
            body: JSON.stringify({ detail: `Unknown manifest field: ${unknownFields[0]}` }),
          })
          return
        }
        saveRevisions.push(body.revision)
        if (staleSaveCount > 0) {
          const remoteFilename = staleSaveCount === 2
            ? 'answer-key-reviewed.pdf'
            : 'answer-key-reviewed-again.pdf'
          staleSaveCount -= 1
          currentManifest = {
            ...currentManifest,
            revision: currentManifest.revision + 1,
            entries: currentManifest.entries.map(entry =>
              entry.id === '44444444-4444-4444-8444-444444444445'
                ? { ...entry, display_filename: remoteFilename }
                : entry
            ),
          }
          await route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Manifest revision is stale.' }),
          })
          return
        }
        if (body.revision !== currentManifest.revision) {
          await route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Manifest revision is stale.' }),
          })
          return
        }
        currentManifest = {
          ...currentManifest,
          revision: body.revision + 1,
          entries: body.entries.map(entry => ({
            ...currentManifest.entries.find(item => item.id === entry.id),
            ...entry,
            updated_at: '2026-08-06T12:01:00Z',
          })) as typeof currentManifest.entries,
          blockers: [],
          eligible_for_planning: false,
        }
        await route.fulfill({ json: currentManifest })
        return
      }
      if (request.method() === 'POST' && pathname.endsWith('/manifest/approve')) {
        currentManifest = { ...currentManifest, eligible_for_planning: true }
        currentJob = { ...currentJob, status: 'completed', stage: 'completed' }
        await route.fulfill({ json: currentManifest })
        return
      }
      await route.fallback()
    })

    await page.goto(`/courses/${COURSE_ID}`)
    await expect(page.getByRole('heading', { name: 'Source manifest review' })).toBeVisible()
    await expect(page.getByText('Review blockers', { exact: true })).toBeVisible()
    await expect(page.getByText('Every source needs a confirmed role.')).toBeVisible()
    await expect(
      page.getByText('Suspected solutions require explicit role and visibility confirmation.')
    ).toBeVisible()

    const firstRole = page.getByLabel('Role').first()
    await firstRole.focus()
    await page.keyboard.press('Tab')
    await expect(page.getByLabel('Visibility').first()).toBeFocused()
    await firstRole.selectOption('reading')
    await page.getByLabel('Visibility').first().selectOption('learner_visible')
    await page.getByLabel('Role').nth(1).selectOption('assignment')
    await page.getByLabel('Visibility').nth(1).selectOption('learner_visible')

    await page.getByRole('button', { name: 'Save corrections' }).click()
    await expect(
      page.getByText('Manifest changed while you were editing. Review refreshed values before saving.')
    ).toBeVisible()
    // The failed 409 is intentionally part of this workflow; assert the
    // subsequent successful path independently of the browser's network log.
    unexpectedConsole.length = 0
    await expect(page.getByText('Revision 2')).toBeVisible()
    await expect(page.getByText('answer-key-reviewed.pdf')).toBeVisible()
    await expect(page.getByLabel('Role').first()).toHaveValue('reading')
    await page.getByRole('button', { name: 'Save corrections' }).click()
    await expect(
      page.getByText('Manifest changed while you were editing. Review refreshed values before saving.')
    ).toBeVisible()
    await expect(page.getByText('Revision 3')).toBeVisible()
    await expect(page.getByText('answer-key-reviewed-again.pdf')).toBeVisible()
    await expect(page.getByLabel('Role').first()).toHaveValue('reading')
    unexpectedConsole.length = 0
    await page.getByRole('button', { name: 'Save corrections' }).click()
    await expect.poll(() => saveRevisions).toEqual([1, 2, 3])
    await expect(page.getByText('Review blockers', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Approve manifest' })).toBeEnabled()

    await page.getByRole('button', { name: 'Approve manifest' }).click()
    await expect(page.getByText(/Ready for planning in OWE-8 \/ OWE-9/)).toBeVisible()
    await expect(page.getByText('Review complete')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save corrections' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Approve manifest' })).toHaveCount(0)
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
    ).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('course-review-390-en.png'), fullPage: true })

    await page.evaluate(() => window.localStorage.setItem('deeptutor-language', 'zh'))
    await page.reload()
    await expect(page.getByRole('heading', { name: '源清单审阅' })).toBeVisible()
    await expect(page.getByText(/已准备好交给 OWE-8 \/ OWE-9/)).toBeVisible()
    await expect(page.getByRole('button', { name: '保存修正' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '批准清单' })).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('course-review-390-zh.png'), fullPage: true })

    await page.evaluate(() => window.localStorage.setItem('deeptutor-language', 'en'))
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Source manifest review' })).toBeVisible()
    await expect(page.getByText(/Ready for planning in OWE-8 \/ OWE-9/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save corrections' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Approve manifest' })).toHaveCount(0)
    expect(saveRevisions).toEqual([1, 2, 3])
    expect(unexpectedConsole).toEqual([])
  })

  test('surfaces durable processing and manifest request errors with retries', async ({ page }) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    await mockWorkspaceShellApi(page)
    let processingFailures = 1
    let manifestFailures = 1
    await page.route('**/api/v1/courses**', async route => {
      const request = route.request()
      const { pathname } = new URL(request.url())
      if (request.method() === 'GET' && pathname.endsWith('/processing') && processingFailures > 0) {
        processingFailures -= 1
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/manifest') && manifestFailures > 0) {
        manifestFailures -= 1
        await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/processing')) {
        await route.fulfill({ json: { job: processingJob } })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/manifest')) {
        await route.fulfill({ json: manifest })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith(`/${COURSE_ID}`)) {
        await route.fulfill({ json: { course } })
        return
      }
      await route.fallback()
    })

    await page.goto(`/courses/${COURSE_ID}`)
    await expect(page.getByRole('heading', { name: 'Processing status unavailable' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Manifest unavailable' })).toBeVisible()
    // The injected 503 responses are expected error-state traffic. Clear it
    // before asserting that the recovered UI is console-clean.
    unexpectedConsole.length = 0
    await page.getByRole('button', { name: 'Retry status' }).click()
    await page.getByRole('button', { name: 'Retry manifest' }).click()
    await expect(page.getByRole('heading', { name: 'Source manifest review' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Processing status unavailable' })).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'Manifest unavailable' })).toHaveCount(0)
    expect(unexpectedConsole).toEqual([])
  })

  test('recovers a crash-queued import through public retry and reload', async ({ page }) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    await mockWorkspaceShellApi(page)
    let processingReads = 0
    let retryCalls = 0
    let currentJob: CourseProcessingJob = {
      ...processingJob,
      status: 'queued',
      stage: 'queued',
      manifest_revision: 0,
    } as CourseProcessingJob
    let currentManifest: CourseManifest = {
      ...manifest,
      revision: 0,
      entries: [],
      blockers: [],
      eligible_for_planning: false,
    } as CourseManifest
    await page.route('**/api/v1/courses**', async route => {
      const request = route.request()
      const { pathname } = new URL(request.url())
      if (request.method() === 'GET' && pathname.endsWith('/processing')) {
        processingReads += 1
        await route.fulfill({ json: { job: currentJob } })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/manifest')) {
        await route.fulfill({ json: currentManifest })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith(`/${COURSE_ID}`)) {
        await route.fulfill({ json: { course } })
        return
      }
      if (request.method() === 'POST' && pathname.endsWith(`/jobs/${processingJob.id}/retry`)) {
        retryCalls += 1
        currentJob = { ...processingJob } as CourseProcessingJob
        currentManifest = { ...manifest } as CourseManifest
        await route.fulfill({ json: { job: currentJob } })
        return
      }
      await route.fallback()
    })

    await page.goto(`/courses/${COURSE_ID}`)
    await expect(page.getByText('Queued — resume required')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Course import is queued' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Resume processing' })).toBeVisible()
    await page.waitForTimeout(1400)
    expect(processingReads).toBe(1)

    await page.getByRole('button', { name: 'Resume processing' }).click()
    await expect(page.getByRole('heading', { name: 'Source manifest review' })).toBeVisible()
    expect(retryCalls).toBe(1)

    await page.reload()
    await expect(page.getByRole('heading', { name: 'Source manifest review' })).toBeVisible()
    await expect(page.getByText('Manifest review required')).toBeVisible()
    expect(unexpectedConsole).toEqual([])
  })

  test('loads bounded pages until an older course is discoverable', async ({ page }) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    const allCourses = Array.from({ length: 55 }, (_, index) => ({
      ...course,
      id: `course-${index}`,
      title: `Course ${index.toString().padStart(2, '0')}`,
      workspace_ref: `courses/course-${index}`,
      units: course.units.map(unit => ({
        ...unit,
        id: `unit-${index}`,
        course_id: `course-${index}`,
      })),
    }))
    const offsets: string[] = []
    await mockWorkspaceShellApi(page)
    await page.route('**/api/v1/courses**', async route => {
      const { pathname } = new URL(route.request().url())
      if (route.request().method() === 'GET' && pathname.endsWith('/processing')) {
        await route.fulfill({ json: { job: processingJob } })
        return
      }
      if (route.request().method() === 'GET' && pathname.endsWith('/manifest')) {
        await route.fulfill({ json: manifest })
        return
      }
      const url = new URL(route.request().url())
      const requestedCourse = allCourses.find(item => url.pathname.endsWith(`/${item.id}`))
      if (requestedCourse) {
        await route.fulfill({ json: { course: requestedCourse } })
        return
      }
      const offset = Number(url.searchParams.get('offset') || 0)
      offsets.push(String(offset))
      const courses = allCourses.slice(offset, offset + 50)
      const nextOffset = offset + courses.length
      await route.fulfill({
        json: {
          courses,
          total: allCourses.length,
          has_more: nextOffset < allCourses.length,
          next_offset: nextOffset < allCourses.length ? nextOffset : null,
        },
      })
    })

    await page.goto('/courses')
    await expect(page.getByText('Course 54')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible()
    await page.getByRole('button', { name: 'Load more' }).click()

    await expect(page.getByRole('heading', { name: 'Course 54' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0)
    await expect(page.locator('a[href^="/courses/"]')).toHaveCount(55)
    expect(offsets).toEqual(['0', '50'])

    await page.getByRole('heading', { name: 'Course 54' }).click()
    await expect(page).toHaveURL(/\/courses\/course-54$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Course 54' })).toBeVisible()
    expect(unexpectedConsole).toEqual([])
  })

  test('loads, creates, retries a lost response with one key, and reopens detail', async ({
    page,
  }) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    const requestKeys: string[] = []
    let createAttempt = 0
    await mockWorkspaceShellApi(page)

    await page.route('**/api/v1/courses**', async route => {
      const request = route.request()
      const { pathname } = new URL(request.url())
      if (request.method() === 'POST') {
        createAttempt += 1
        requestKeys.push(request.headers()['idempotency-key'] || '')
        if (createAttempt === 1) {
          // The upstream may commit while a proxy loses its response. The UI
          // must retain the attempt identity when it reports the failure.
          await route.fulfill({
            status: 504,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Response lost; retry safely.' }),
          })
        } else {
          await route.fulfill({ status: 200, json: { course, created: false, processing_job: processingJob } })
        }
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/processing')) {
        await route.fulfill({ json: { job: processingJob } })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith('/manifest')) {
        await route.fulfill({ json: manifest })
        return
      }
      if (request.method() === 'GET' && pathname.endsWith(`/${COURSE_ID}`)) {
        await route.fulfill({ json: { course } })
        return
      }
      await route.fulfill({
        json: { courses: [], total: 0, has_more: false, next_offset: null },
      })
    })

    await page.goto('/courses')
    await expect(page.getByRole('heading', { level: 1, name: 'Your courses' })).toBeVisible()
    await page.getByRole('button', { name: 'New course' }).click()
    await expect(page.getByRole('button', { name: 'New course' })).toHaveAttribute(
      'aria-expanded',
      'true'
    )
    await page.getByLabel('Course title').fill('Cell Biology')
    await page.getByLabel('First unit').fill('The cell')
    await page.getByLabel('Course description').fill('Learn how cells are organized.')
    await page.getByLabel('Desired outcome').fill('Explain how cells are organized.')
    await page.getByLabel('MIT OpenCourseWare URL').fill(course.ocw_url)
    await page.getByLabel('Source files').setInputFiles({
      name: 'lecture-notes.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('Cell biology lecture notes'),
    })

    await page.getByRole('button', { name: 'Import course' }).click()
    await expect(page.getByText('Response lost; retry safely.')).toBeVisible()
    // A simulated 504 is expected to produce one browser resource error. The
    // successful retry and reopened workspace must remain console-clean.
    unexpectedConsole.length = 0
    await page.getByRole('button', { name: 'Import course' }).click()

    await expect(page).toHaveURL(new RegExp(`/courses/${COURSE_ID}$`))
    await expect(page.getByRole('heading', { level: 1, name: course.title })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Course outline' })).toContainText('The cell')
    await expect(page.getByRole('region', { name: 'Mastery' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Schedule' })).toBeVisible()
    expect(requestKeys).toHaveLength(2)
    expect(requestKeys[0]).toBeTruthy()
    expect(requestKeys[1]).toBe(requestKeys[0])
    expect(unexpectedConsole).toEqual([])
  })

  test('is usable at 390x844 with named regions and keyboard focus', async ({
    page,
  }, testInfo) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await mockCourseApi(page)
    await page.goto('/courses')

    await expect(page.getByRole('heading', { level: 1, name: 'Your courses' })).toBeVisible()
    const navigationToggle = page.getByRole('button', { name: 'Open navigation' })
    await expect(navigationToggle).toBeVisible()
    await expect(navigationToggle).toHaveAttribute('aria-expanded', 'false')
    await navigationToggle.click()
    const navigation = page.locator('aside').first()
    await expect(navigation).toBeVisible()
    await expect(navigationToggle).toHaveAttribute('aria-expanded', 'true')
    for (const href of [
      '/home',
      '/partners',
      '/agents',
      '/co-writer',
      '/book',
      '/courses',
      '/space',
      '/memory',
      '/knowledge',
      '/settings',
    ]) {
      await expect(navigation.locator(`a[href="${href}"]`)).toBeVisible()
    }
    await page.keyboard.press('Shift+Tab')
    expect(
      await page.evaluate(() =>
        document.querySelector('aside')?.contains(document.activeElement)
      )
    ).toBe(true)
    await page.keyboard.press('Escape')
    await expect(navigationToggle).toHaveAttribute('aria-expanded', 'false')

    await navigationToggle.click()
    await navigation.locator('a[href="/courses"]').click()
    await expect(navigationToggle).toHaveAttribute('aria-expanded', 'false')
    const mainBox = await page.getByRole('main').boundingBox()
    expect(mainBox?.x).toBe(0)
    expect(mainBox?.width).toBe(390)
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
    ).toBe(true)

    const createButton = page.getByRole('button', { name: 'New course' })
    await expect(createButton).toHaveAttribute('aria-expanded', 'false')
    await createButton.focus()
    await expect(createButton).toBeFocused()
    await createButton.click()
    await expect(createButton).toHaveAttribute('aria-controls', 'new-course-form')
    await expect(page.locator('#new-course-form')).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('courses-mobile.png'), fullPage: true })

    await page.goto(`/courses/${COURSE_ID}`)
    await expect(page.getByRole('heading', { level: 1, name: course.title })).toBeVisible()
    await expect(page.getByRole('heading', { level: 2, name: 'Course outline' })).toBeVisible()
    await expect(page.getByRole('heading', { level: 2, name: 'Mastery' })).toBeVisible()
    await expect(page.getByRole('heading', { level: 2, name: 'Schedule' })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Mastery' })).toContainText(
      'Mastery evidence'
    )
    await expect(page.getByRole('region', { name: 'Schedule' })).toContainText('No sessions')
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
    ).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('course-detail-mobile.png'), fullPage: true })
    expect(unexpectedConsole).toEqual([])
  })

  test('empty gallery discloses the form and moves keyboard focus to its title', async ({
    page,
  }) => {
    await mockCourseApi(page, [])
    await page.goto('/courses')

    const emptyCta = page.getByRole('button', { name: 'Create your first course' })
    await expect(emptyCta).toHaveAttribute('aria-expanded', 'false')
    await expect(emptyCta).toHaveAttribute('aria-controls', 'new-course-form')
    await emptyCta.focus()
    await page.keyboard.press('Enter')

    await expect(emptyCta).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('#new-course-form')).toBeVisible()
    await expect(page.getByLabel('Course title')).toBeFocused()
  })

  test('Book keeps its bottom content inside the mobile workspace viewport', async ({ page }) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await mockWorkspaceShellApi(page)
    await page.route('**/api/v1/book/books', route =>
      route.fulfill({ json: { books: [] } })
    )
    await page.goto('/book')

    await expect(page.getByText('No books yet')).toBeVisible()
    const bookContent = page.locator('main').last()
    const bounds = await bookContent.boundingBox()
    expect(bounds).not.toBeNull()
    expect((bounds?.y || 0) + (bounds?.height || 0)).toBeLessThanOrEqual(844)
    await bookContent.evaluate(element => {
      element.scrollTop = element.scrollHeight
    })
    await expect(bookContent.getByRole('button', { name: 'New book' })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390)
    expect(unexpectedConsole).toEqual([])
  })

  test('keeps the desktop sidebar and course workspace visible', async ({ page }) => {
    const unexpectedConsole = observeUnexpectedConsole(page)
    await page.setViewportSize({ width: 1280, height: 900 })
    await mockCourseApi(page)
    await page.goto(`/courses/${COURSE_ID}`)

    await expect(page.locator('aside')).toBeVisible()
    await expect(page.getByRole('heading', { level: 1, name: course.title })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Course outline' })).toBeVisible()
    expect(unexpectedConsole).toEqual([])
  })
})
