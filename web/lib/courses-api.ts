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
  desired_outcome?: string
  weekly_minutes?: number
  ocw_url?: string
  scheduling?: string | null
  difficulty?: string | null
  accessibility?: string | null
  processing_job_id?: string | null
}

export interface CreateCourseInput {
  title: string
  description?: string
  unit_title?: string
  desired_outcome?: string
  weekly_minutes?: number
  ocw_url?: string
  scheduling?: string
  difficulty?: string
  accessibility?: string
  files?: File[]
}

export type CourseJobStatus =
  | 'queued'
  | 'source_processing'
  | 'awaiting_manifest_review'
  | 'completed'
  | 'failed'

export interface CourseProcessingJob {
  id: string
  course_id: string
  status: CourseJobStatus
  stage: 'queued' | 'source_processing' | 'awaiting_manifest_review' | 'completed'
  failed_stage: 'queued' | 'source_processing' | 'awaiting_manifest_review' | 'completed' | null
  error_code: string | null
  attempt_count: number
  manifest_revision: number
  created_at: string
  updated_at: string
}

export type ManifestRole =
  | 'syllabus'
  | 'lecture_note'
  | 'reading'
  | 'assignment'
  | 'solution'
  | 'grading_resource'
  | 'unknown'

export type ManifestVisibility = 'learner_visible' | 'instructor_only'

export interface ManifestEntry {
  id: string
  course_id: string
  source_id: string
  original_filename: string
  display_filename: string
  role: ManifestRole
  visibility: ManifestVisibility
  suspected_solution: boolean
  role_confirmed: boolean
  visibility_confirmed: boolean
  created_at: string
  updated_at: string
}

export interface ManifestEntryUpdate {
  id: string
  role?: ManifestRole
  visibility?: ManifestVisibility
  role_confirmed?: boolean
  visibility_confirmed?: boolean
}

export interface CourseManifest {
  course_id: string
  revision: number
  entries: ManifestEntry[]
  blockers: string[]
  eligible_for_planning: boolean
}

export interface CourseListPage {
  courses: Course[]
  total: number
  has_more: boolean
  next_offset: number | null
}

export class CoursesApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'CoursesApiError'
    this.status = status
  }
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
    throw new CoursesApiError(detail, response.status)
  }
  return (await response.json()) as T
}

export const coursesApi = {
  create: (input: CreateCourseInput, requestKey: string) => {
    const { files, ...fields } = input
    const body = new FormData()
    for (const [key, value] of Object.entries(fields)) {
      if (value !== undefined && value !== null) body.append(key, String(value))
    }
    for (const file of files || []) body.append('files', file, file.name)
    return request<{ course: Course; created: boolean; processing_job: CourseProcessingJob }>(
      '',
      {
        method: 'POST',
        headers: { 'Idempotency-Key': requestKey },
        body,
      }
    )
  },
  list: (limit = 50, offset = 0) =>
    request<CourseListPage>(`?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    }).toString()}`),
  get: (courseId: string) => request<{ course: Course }>(`/${encodeURIComponent(courseId)}`),
  processing: (courseId: string) =>
    request<{ job: CourseProcessingJob }>(`/${encodeURIComponent(courseId)}/processing`),
  manifest: (courseId: string) =>
    request<CourseManifest>(`/${encodeURIComponent(courseId)}/manifest`),
  updateManifest: (
    courseId: string,
    revision: number,
    entries: ManifestEntryUpdate[]
  ) => {
    const updates: ManifestEntryUpdate[] = entries.map(entry => ({
      id: entry.id,
      role: entry.role,
      visibility: entry.visibility,
      role_confirmed: entry.role_confirmed,
      visibility_confirmed: entry.visibility_confirmed,
    }))
    return request<CourseManifest>(`/${encodeURIComponent(courseId)}/manifest`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision, entries: updates }),
    })
  },
  approveManifest: (courseId: string, revision: number) =>
    request<CourseManifest>(`/${encodeURIComponent(courseId)}/manifest/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision }),
    }),
  retry: (jobId: string) =>
    request<{ job: CourseProcessingJob }>(`/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: 'POST',
    }),
}
