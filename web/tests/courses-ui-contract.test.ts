import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const gallery = readFileSync(
  path.resolve(process.cwd(), 'app/(workspace)/courses/page.tsx'),
  'utf8'
)
const detail = readFileSync(
  path.resolve(process.cwd(), 'app/(workspace)/courses/[courseId]/page.tsx'),
  'utf8'
)
const sidebar = readFileSync(
  path.resolve(process.cwd(), 'components/sidebar/SidebarShell.tsx'),
  'utf8'
)

test('Course Mode has a discoverable gallery and dedicated detail route', () => {
  assert.match(sidebar, /href:\s*"\/courses"/)
  assert.match(gallery, /coursesApi\s*\.create\(input, requestKey\)/)
  assert.match(gallery, /t\('Draft'\)/)
  assert.match(detail, /coursesApi\s*\.get\(params\.courseId\)/)
  assert.doesNotMatch(gallery + detail, /\/space\/learning/)
})

test('draft detail keeps mastery and schedule as distinct placeholders', () => {
  assert.match(detail, /title=\{t\('Mastery'\)\}/)
  assert.match(detail, /title=\{t\('Schedule'\)\}/)
  assert.match(detail, /Mastery evidence will appear after learning begins\./)
  assert.match(detail, /No sessions or reviews are scheduled yet\./)
})

test('create form retains a stable request key for the same manual retry', () => {
  assert.match(gallery, /pendingRequest\s*=\s*useRef/)
  assert.match(gallery, /existing\?\.fingerprint\s*===\s*fingerprint/)
  assert.match(gallery, /existing\.requestKey\s*:\s*crypto\.randomUUID\(\)/)
})

test('Course import UI captures the source contract and review workflow', () => {
  assert.match(gallery, /Desired outcome/)
  assert.match(gallery, /weekly_minutes/)
  assert.match(gallery, /ocw_url/)
  assert.match(gallery, /setInputFiles|Source files/)
  assert.match(detail, /Source manifest review/)
  assert.match(detail, /Approve manifest/)
  assert.match(detail, /Retry stage/)
})
