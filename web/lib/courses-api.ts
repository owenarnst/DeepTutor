import { apiFetch, apiUrl } from '@/lib/api'

const BASE = '/api/v1/courses'

export interface CourseUnit {
  id: string
  course_id: string
  title: string
  position: number
}

export interface Course {
  id: string
  title: string
  description: string
  status: 'draft'
  workspace_ref: string
  created_at: string
  updated_at: string
  units: CourseUnit[]
}

export interface CreateCourseInput {
  title: string
  description?: string
  unit_title?: string
}

export interface CourseListPage {
  courses: Course[]
  total: number
  has_more: boolean
  next_offset: number | null
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(apiUrl(`${BASE}${path}`), init)
  if (!response.ok) {
    let detail = response.statusText || 'Request failed'
    try {
      const payload = (await response.json()) as { detail?: string }
      detail = payload.detail || detail
    } catch {
      // The status text remains a safe fallback for non-JSON proxy failures.
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export const coursesApi = {
  create: (input: CreateCourseInput, requestKey: string) =>
    request<{ course: Course; created: boolean }>('', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': requestKey,
      },
      body: JSON.stringify(input),
    }),
  list: (limit = 50, offset = 0) =>
    request<CourseListPage>(`?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    }).toString()}`),
  get: (courseId: string) => request<{ course: Course }>(`/${encodeURIComponent(courseId)}`),
}
