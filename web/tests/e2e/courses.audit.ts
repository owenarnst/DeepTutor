import { expect, test, type Page } from '@playwright/test'

const COURSE_ID = '11111111-1111-4111-8111-111111111111'

const course = {
  id: COURSE_ID,
  title: 'Cell Biology',
  description: 'Learn how cells are organized.',
  status: 'draft',
  workspace_ref: `courses/${COURSE_ID}`,
  created_at: '2026-08-06T12:00:00Z',
  updated_at: '2026-08-06T12:00:00Z',
  units: [
    {
      id: '22222222-2222-4222-8222-222222222222',
      course_id: COURSE_ID,
      title: 'The cell',
      position: 0,
    },
  ],
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
  await page.route('**/api/v1/settings', route =>
    route.fulfill({ json: { catalog: {} } })
  )
}

async function mockCourseApi(page: Page, listedCourses = [course]) {
  await mockWorkspaceShellApi(page)
  await page.route('**/api/v1/courses**', async route => {
    const request = route.request()
    const { pathname } = new URL(request.url())
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
          await route.fulfill({ status: 200, json: { course, created: false } })
        }
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
    await page.getByLabel('Description').fill('Learn how cells are organized.')

    await page.getByRole('button', { name: 'Create draft' }).click()
    await expect(page.getByText('Response lost; retry safely.')).toBeVisible()
    // A simulated 504 is expected to produce one browser resource error. The
    // successful retry and reopened workspace must remain console-clean.
    unexpectedConsole.length = 0
    await page.getByRole('button', { name: 'Create draft' }).click()

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
    const navigationToggle = page.getByRole('button', { name: 'Expand sidebar' })
    await expect(navigationToggle).toBeVisible()
    await navigationToggle.click()
    const navigation = page.getByRole('dialog', { name: 'Workspace navigation' })
    await expect(navigation).toBeVisible()
    await expect(navigation.getByRole('button', { name: 'Collapse sidebar' })).toBeFocused()
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
        document.querySelector('[role="dialog"]')?.contains(document.activeElement)
      )
    ).toBe(true)
    await page.keyboard.press('Escape')
    await expect(navigation).toHaveCount(0)
    await expect(navigationToggle).toBeFocused()

    await navigationToggle.click()
    await navigation.locator('a[href="/courses"]').click()
    await expect(navigation).toHaveCount(0)
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
