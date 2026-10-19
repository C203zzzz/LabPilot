import test from 'node:test'
import assert from 'node:assert/strict'

import { pageProgress, pageRouteSummary } from '../src/services/pageProgress.js'

/**
 * The real page list from the 17-page acceptance upload
 * (task-<run-14>, document 00000000-0000-4000-8000-000000000004).
 *
 * Pages 10 and 15 are the ones that used to disappear: they are `vision` but
 * were never paid for (held back by the page cap) and hold no local text, so
 * neither the "本地文字" nor the "视觉识别" filter matched them.
 */
const local = n => ({
  number: n,
  route: 'native_text',
  route_reason: 'deferred_by_page_cap',
  provenance: [{ kind: 'native_pdf_text', status: 'empty' }, { kind: 'structured_pdf', status: null }],
})
const paid = n => ({
  number: n,
  route: 'vision',
  route_reason: 'explicit_page_retry',
  provenance: [
    { kind: 'native_pdf_text', status: 'empty' },
    { kind: 'structured_pdf', status: null },
    { kind: 'visual_page_analysis', status: 'completed' },
  ],
})
const unread = n => ({
  number: n,
  route: 'vision',
  route_reason: 'deferred_by_page_cap',
  provenance: [{ kind: 'native_pdf_text', status: 'empty' }],
})

const ACCEPTANCE_PAGES = [
  local(1), paid(2), paid(3), local(4), local(5), paid(6), local(7), local(8),
  paid(9), unread(10), local(11), local(12), paid(13), paid(14), unread(15),
  paid(16), paid(17),
]

test('the three reading outcomes partition the page list', () => {
  const progress = pageProgress(ACCEPTANCE_PAGES)

  assert.equal(progress.total, 17)
  assert.equal(progress.visual, 8)
  assert.equal(progress.local, 7)
  assert.equal(progress.pending, 2)
  // The bug this guards: 7 + 8 = 15, so two pages belonged to no bucket.
  assert.equal(progress.visual + progress.local + progress.pending, progress.total)
})

test('pages held back by the page cap are never lost', () => {
  const progress = pageProgress(ACCEPTANCE_PAGES)

  // 7 local + 2 unread were all deferred; the 8 paid pages were not.
  assert.equal(progress.deferred, 9)
  // Deferred pages are still counted in a reading outcome.
  assert.equal(progress.local + progress.pending, 9)
})

test('the summary adds up to the total it prints', () => {
  const text = pageRouteSummary(ACCEPTANCE_PAGES)

  assert.match(text, /本地文字 7 页/)
  assert.match(text, /视觉识别 8 页/)
  assert.match(text, /待识别 2 页/)
  assert.match(text, /共 17 页/)
  assert.match(text, /其中 9 页因识别页数上限暂缓/)
  // 7 + 8 + 2 是且仅是 17，不再出现"待处理 9 页"这种与前两项重叠的第三个数。
  assert.doesNotMatch(text, /待处理/)
})

test('a page that is both local and paid is counted once, as vision', () => {
  // A page retried through paid vision must not be double reported as local.
  const both = [{
    number: 1,
    route: 'native_text',
    route_reason: 'explicit_page_retry',
    provenance: [
      { kind: 'structured_pdf', status: null },
      { kind: 'visual_page_analysis', status: 'completed' },
    ],
  }]

  const progress = pageProgress(both)

  assert.equal(progress.visual, 1)
  assert.equal(progress.local, 0)
  assert.equal(progress.pending, 0)
  assert.equal(progress.visual + progress.local + progress.pending, 1)
})

test('a failed vision attempt does not count as read', () => {
  const failed = [{
    number: 1,
    route: 'vision',
    route_reason: 'explicit_page_retry',
    provenance: [{ kind: 'visual_page_analysis', status: 'failed' }],
  }]

  const progress = pageProgress(failed)

  assert.equal(progress.visual, 0)
  assert.equal(progress.pending, 1)
})

test('a document with no pages says so instead of printing zeros', () => {
  assert.equal(pageRouteSummary([]), '尚未识别')
  assert.equal(pageRouteSummary(undefined), '尚未识别')
  assert.equal(pageProgress(undefined).total, 0)
})

test('a fully paid document reports no pending pages explicitly', () => {
  const text = pageRouteSummary([paid(1), paid(2)])

  assert.equal(text, '视觉识别 2 页 · 待识别 0 页（共 2 页）')
})
