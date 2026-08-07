import test from 'node:test'
import assert from 'node:assert/strict'

import { coursesApi, type CreateCourseInput } from '../lib/courses-api'

const sampleCourse = {
  id: '11111111-1111-4111-8111-111111111111',
  title: 'Topology',
  description: 'Open sets',
  status: 'draft' as const,
  workspace_ref: 'courses/11111111-1111-4111-8111-111111111111',
  created_at: '2026-08-06T00:00:00+00:00',
  updated_at: '2026-08-06T00:00:00+00:00',
  units: [
    {
      id: '22222222-2222-4222-8222-222222222222',
      course_id: '11111111-1111-4111-8111-111111111111',
      title: 'Unit 1',
      position: 0,
    },
  ],
}

function stubFetch(
  handler: (input: RequestInfo | URL, init?: RequestInit) => Response
): () => void {
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => handler(input, init)
  return () => {
    globalThis.fetch = original
  }
}

test("create sends the caller's stable idempotency key without ownership", async () => {
  const payload: CreateCourseInput = {
    title: 'Topology',
    description: 'Open sets',
    unit_title: 'Unit 1',
  }
  const restore = stubFetch((input, init) => {
    assert.equal(String(input), '/api/v1/courses')
    assert.equal(init?.method, 'POST')
    assert.equal(new Headers(init?.headers).get('Idempotency-Key'), 'browser-request-1')
    assert.deepEqual(JSON.parse(String(init?.body)), payload)
    assert.equal('user_id' in JSON.parse(String(init?.body)), false)
    return Response.json({ course: sampleCourse, created: true }, { status: 201 })
  })
  try {
    const result = await coursesApi.create(payload, 'browser-request-1')
    assert.equal(result.created, true)
    assert.equal(result.course.id, sampleCourse.id)
  } finally {
    restore()
  }
})

test('list and detail use retry-safe GETs and encode the course id', async () => {
  const seen: string[] = []
  const restore = stubFetch((input, init) => {
    seen.push(String(input))
    assert.equal(init?.method, undefined)
    if (String(input).endsWith('/courses')) {
      return Response.json({ courses: [sampleCourse] })
    }
    return Response.json({ course: sampleCourse })
  })
  try {
    assert.equal((await coursesApi.list()).courses.length, 1)
    assert.equal((await coursesApi.get('course/id')).course.title, 'Topology')
    assert.deepEqual(seen, ['/api/v1/courses', '/api/v1/courses/course%2Fid'])
  } finally {
    restore()
  }
})

test("transport surfaces the backend's safe conflict detail", async () => {
  const restore = stubFetch(() =>
    Response.json(
      { detail: 'Idempotency key was already used with different course input' },
      { status: 409 }
    )
  )
  try {
    await assert.rejects(
      coursesApi.create({ title: 'Changed' }, 'browser-request-1'),
      /already used with different course input/
    )
  } finally {
    restore()
  }
})
