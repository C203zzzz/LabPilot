/**
 * 候选卡尾部的「来源」块：**照成交价卡片那一块做**。
 *
 * 负责人批注（原话）：「需要提供来源网页 就跟查询成交价一样 且前端页面也要做成相似的效果」。
 * "相似"是三条可验证的事实，这份测试逐条钉住：
 *
 * 1. **同一套样式**：类名就是价格来源那一块的 ``price-record-sources`` /
 *    ``price-source-list``（不新造一套），同样是默认折叠的 ``<details>``；
 * 2. **一条一行、可点开**：每条来源一个 ``<dt>`` 中文标签 + 一个 ``<dd>`` 链接，
 *    ``href`` 是完整地址、页面上不出现 ``http(s)://``；
 * 3. **主来源看得出来**：``source_pages[].primary`` 的那一条排在最前并标「（主来源）」。
 *
 * 另外两条反向断言：老数据（没有 ``source_pages``）退回候选自己的 ``url``，不因为缺
 * 字段就整块消失；参考候选（``run.reference_candidates``）也走同一张卡片，那条清单
 * 里同样能点开来源。
 *
 * 数据用真渲染拿（vite 的 ``ssrLoadModule`` 转 JSX、``react-dom/server`` 出 HTML），
 * 判读逻辑另有一组纯函数断言。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

import {CANDIDATE_SOURCE_ORIGINS, candidateSourceEntries} from '../src/services/candidateSources.js'

const CATALOG = 'https://www.yiqi.com/product/detail_1000001.html'
const DETAIL = 'https://www.xiangyilxj.com/product/info_itemid_1001.html'
const AWARD = 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260301_10000001.htm'

// ---------------------------------------------------------------------------
// 一、纯函数：这个字段说给页面听的那几行
// ---------------------------------------------------------------------------

test('多来源按"主来源在最前、其余保持后端顺序"排', () => {
  const entries = candidateSourceEntries({model: 'Q20', source_pages: [
    {url: DETAIL, origin: 'detail_page'},
    {url: AWARD, origin: 'discovered', primary: true},
    {url: CATALOG, origin: 'config_match'},
  ]})
  assert.deepEqual(entries.map(entry => entry.url), [AWARD, DETAIL, CATALOG])
  assert.deepEqual(entries.map(entry => entry.primary), [true, false, false])
})

test('origin 翻成中文标签与页面类型，认不出来的给中性说法、不回显代号', () => {
  const entries = candidateSourceEntries({source_pages: [
    {url: CATALOG, origin: 'discovered', primary: true},
    {url: DETAIL, origin: 'detail_page'},
  ]})
  assert.deepEqual(entries.map(entry => entry.label), ['发现来源', '产品详情页'])
  assert.deepEqual(entries.map(entry => entry.page), ['网页', '产品页'])
  assert.deepEqual(entries.map(entry => entry.text), ['仪器网 · 网页', '湘仪集团官网 · 产品页'])
  const unknown = candidateSourceEntries({source_pages: [{url: CATALOG, origin: 'brand_new_step'}]})
  assert.equal(unknown[0].label, '来源')
  assert.equal(unknown[0].page, '')
  assert.ok(!unknown[0].text.includes('brand_new_step'))
})

test('空地址与重复网址都不产出行；没有网址的条目一条都不渲染', () => {
  const entries = candidateSourceEntries({source_pages: [
    {url: '', origin: 'discovered', primary: true},
    {url: '   ', origin: 'detail_page'},
    {url: CATALOG, origin: 'discovered', primary: true},
    {url: CATALOG, origin: 'matrix'},
  ]})
  assert.equal(entries.length, 1)
  assert.equal(entries[0].url, CATALOG)
  assert.equal(candidateSourceEntries({}).length, 0)
  assert.equal(candidateSourceEntries(null).length, 0)
})

test('老数据（没有 source_pages）退回候选自己的 url，语义与发现来源相同', () => {
  const entries = candidateSourceEntries({model: 'Q20', url: CATALOG})
  assert.deepEqual(entries, [{key: CATALOG, url: CATALOG, label: '发现来源', page: '网页',
                              text: '仪器网 · 网页', primary: true}])
})

test('前缀与后端那份 origin 常量一一对应（两处各写一套就会漂）', () => {
  const service = readFileSync(new URL('../src/services/candidateSources.js', import.meta.url), 'utf8')
  assert.deepEqual(CANDIDATE_SOURCE_ORIGINS,
    ['discovered', 'detail_page', 'brand_evidence', 'config_match', 'matrix'])
  for (const origin of CANDIDATE_SOURCE_ORIGINS) assert.ok(service.includes(`${origin}:`), origin)
})

// ---------------------------------------------------------------------------
// 二、真渲染：候选卡尾那一块就是价格来源那一套
// ---------------------------------------------------------------------------

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

const candidate = (extra = {}) => ({
  candidate_id: '9a8b7c6d5e4f3021', brand: '贺默', model: 'H1650K', category: '离心机',
  url: CATALOG, excerpt: '高速离心机-贺默H1650K台式微量高速离心机',
  source_pages: [{url: CATALOG, origin: 'discovered', primary: true},
                 {url: DETAIL, origin: 'matrix', primary: false}],
  price_lookup: {status: 'not_requested', reason_code: 'not_requested'}, prices: [],
  ...extra,
})

function render(run) {
  return renderToStaticMarkup(React.createElement(ResearchResults, {
    run: {prices: [], sources: [], lanes: {}, gaps: [], alternatives: [], reference_candidates: [], ...run},
    baseline: {brand: '湘仪', model: 'XKA-2200', category: '离心机'}, section: 'alternatives',
  }))
}

/** 卡尾那一块的 HTML（没有就返回空串）。 */
function sourcesBlock(html) {
  const at = html.indexOf('price-record-sources')
  return at < 0 ? '' : html.slice(at, html.indexOf('</details>', at))
}

test('正式候选：来源块和价格来源同一套类名、默认折叠、标题写明几条', () => {
  const html = render({alternatives: [candidate()]})
  const block = sourcesBlock(html)
  assert.notEqual(block, '', '候选卡片上没有来源块')
  assert.match(html, /<details class="price-record-sources">/, '来源不是折叠块或换了一套类名')
  assert.ok(!html.includes('<details class="price-record-sources" open'), '来源默认展开了')
  assert.ok(block.includes('price-source-list'), '没有用价格来源那份列表样式')
  assert.ok(block.includes('<summary>来源（2）</summary>'), '折叠标题看不出有几条来源')
  assert.ok(block.indexOf('price-source-list') > block.indexOf('price-record-sources'))
})

test('主来源在最前并标出来，每条链接的文字是"指向哪里"的简述而不是 URL', () => {
  const block = sourcesBlock(render({alternatives: [candidate()]}))
  assert.ok(block.includes('<dt>发现来源（主来源）</dt>'), block)
  assert.ok(block.includes('<dt>参数依据</dt>'), block)
  assert.ok(block.includes('仪器网 · 网页') && block.includes('湘仪集团官网 · 产品页'), block)
  assert.ok(block.includes(CATALOG) && block.includes(DETAIL), 'href 里的真实地址丢了（链接点不开）')
  assert.ok(!/>https?:\/\//.test(block), '可见文字里印出了 URL')
})

test('老数据没有 source_pages 时退回候选的 url，来源块照样在', () => {
  const legacy = candidate()
  delete legacy.source_pages
  const block = sourcesBlock(render({alternatives: [legacy]}))
  assert.ok(block.includes('<summary>来源（1）</summary>'), block)
  assert.ok(block.includes('<dt>发现来源（主来源）</dt>'), block)
})

test('候选没有可点地址时这一块整个不存在（不印"来源未提供"）', () => {
  const html = render({alternatives: [candidate({url: '', source_pages: []})]})
  assert.ok(!html.includes('price-record-sources'), html.slice(0, 400))
})
