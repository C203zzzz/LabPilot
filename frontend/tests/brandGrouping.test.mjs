/**
 * 品牌分组：判据必须在后端，页面上不许再有第二份别名表。
 *
 * 病根（`docs/195` §5）：别名表曾经存在三处——后端 `_BRAND_ALIASES`（8 条）、
 * 后端 hardened 实现（已收口）、以及 `ResearchResults.jsx` 里硬编码的
 * `{'天津泰斯特':'泰斯特','上海一恒':'一恒'}`（只有 2 条）。前端缺 `徕卡` 与两条
 * 苏州安泰，于是界面上把 `徕卡`／`Leica`、`苏州安泰`／`苏净安泰` 显示成两个品牌两组。
 *
 * 现在读接口（`procurement_research.annotate_candidate_brands`）给每个候选项算好
 * `brand_key`（规范写法）与 `same_brand_as_baseline`（算不算原品牌），前端只展示。
 * 这份测试钉三件事：新字段照用；老 payload 走退化不抛错；页面上一个字都不许再写别名。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {brandGroupKey, sameBrandAsBaseline} from '../src/services/alternativePresentation.js'

const page = readFileSync(new URL('../src/pages/ResearchResults.jsx', import.meta.url), 'utf8')

test('页面上不再有别名条目：第三份表必须消失，不是从 2 条补成 5 条', () => {
  assert.ok(!/天津泰斯特/.test(page), '页面里又出现了硬编码的别名表条目')
  assert.ok(!/上海一恒/.test(page), '页面里又出现了硬编码的别名表条目')
  assert.ok(!/brandIdentity/.test(page), '页面里又出现了自己那份 brandIdentity')
  assert.ok(!/'泰斯特'\s*:/.test(page), '页面里又出现了别名映射')
  // 这一行原来把整条 import 语句钉死。候选卡片后来从同一个 service 又多取了几个
  // 呈现函数（``candidateTitle`` / ``configMatchPresentation``，两段式候选卡片用），
  // 所以这里只钉"品牌判据仍然从 service 里取"这件事——它才是这条用例的目的
  // （页面不许自己维护别名表）。
  assert.match(page, /import \{[^}]*brandGroupKey[^}]*sameBrandAsBaseline[^}]*\} from '\.\.\/services\/alternativePresentation'/)
})

test('后端算好的字段直接用：徕卡 / Leica 归到同一组', () => {
  const baseline = {brand: 'Leica', brand_key: 'leica', same_brand_as_baseline: true}
  const candidate = {brand: '徕卡', brand_key: 'leica', same_brand_as_baseline: true}
  assert.equal(brandGroupKey(candidate), 'leica')
  assert.equal(sameBrandAsBaseline(candidate, baseline), true)
})

test('后端算好的字段直接用：苏州安泰 / 苏净安泰 归到同一组，且算原品牌', () => {
  const baseline = {brand: '苏净安泰', brand_key: '苏净安泰', same_brand_as_baseline: true}
  for (const brand of ['苏州安泰', '苏州安泰空气技术有限公司', '苏净安泰']) {
    // 后端把三种写法都归到 `苏净安泰`，前端只读它给的键。
    const candidate = {brand, brand_key: '苏净安泰', same_brand_as_baseline: true}
    assert.equal(brandGroupKey(candidate), '苏净安泰')
    assert.equal(sameBrandAsBaseline(candidate, baseline), true)
  }
})

test('后端说不是原品牌就是跨品牌，前端不许自己翻案', () => {
  const baseline = {brand: '湘仪', brand_key: '湘仪', same_brand_as_baseline: true}
  const candidate = {brand: '湘仪', brand_key: '湘仪', same_brand_as_baseline: false}
  assert.equal(sameBrandAsBaseline(candidate, baseline), false)
})

test('老 payload 没有新字段时走退化：按写法本身相等，不抛错', () => {
  const baseline = {brand: '泰斯特'}
  assert.equal(sameBrandAsBaseline({brand: '泰斯特'}, baseline), true)
  // 旧数据里这两种写法会分成两组——这是已知限制，不是崩溃（后端没给 brand_key）。
  assert.equal(sameBrandAsBaseline({brand: '天津泰斯特'}, baseline), false)
  assert.equal(brandGroupKey({brand: '天津泰斯特'}), '天津泰斯特')
  assert.equal(brandGroupKey({brand: '徕卡'}), '徕卡')
})

test('退化路径的空输入：基线没有品牌时任何候选都算跨品牌，且不抛错', () => {
  for (const baseline of [{}, {brand: ''}, {brand: null}, undefined, null]) {
    assert.equal(sameBrandAsBaseline({brand: '湘仪'}, baseline), false)
  }
  for (const candidate of [{}, {brand: ''}, {brand: null}, undefined, null]) {
    assert.equal(brandGroupKey(candidate), '')
    assert.equal(sameBrandAsBaseline(candidate, {brand: '湘仪'}), false)
  }
})

test('alternatives 全为 0 的老任务（190 那三次 run）在退化路径上不出错', () => {
  // 这三份 run 的 `alternatives` 就是空列表：分组三步都只对空数组做 filter。
  const run = {alternatives: []}
  const baseline = {brand: '苏净安泰'}
  const cross = run.alternatives.filter(a => a.brand && !sameBrandAsBaseline(a, baseline))
  const same = run.alternatives.filter(a => a.brand && sameBrandAsBaseline(a, baseline))
  const unknown = run.alternatives.filter(a => !a.brand)
  assert.deepEqual([cross, same, unknown], [[], [], []])
  assert.deepEqual([...new Set(cross.map(brandGroupKey))], [])
  // 退一步：连 alternatives 字段本身都没有的 run 也一样。
  const bare = {}
  assert.deepEqual((bare.alternatives || []).filter(a => a.brand), [])
})

test('分组只读后端字段与写法本身，不再做任何别名归一', () => {
  // 页面上那三处分组调用必须走这两个 service 函数。
  assert.match(page, /const cross = alternatives\.filter\(a => a\.brand && !sameBrandAsBaseline\(a, baseline\)\)/)
  assert.match(page, /const same = alternatives\.filter\(a => a\.brand && sameBrandAsBaseline\(a, baseline\)\)/)
  assert.match(page, /const brands = \[\.\.\.new Set\(cross\.map\(brandGroupKey\)\)\]/)
  assert.match(page, /cross\.filter\(a => brandGroupKey\(a\) === brand\)/)
})
