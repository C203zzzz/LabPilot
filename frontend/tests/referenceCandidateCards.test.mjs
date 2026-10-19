/**
 * 参考候选（``run.reference_candidates``）在页面上的样子。
 *
 * 这一栏原来是紧凑的一行（``<article class="source-row">``：标题 + 原因 + 一个链接），
 * **没有候选卡片、也没有"查这个的价格"按钮**——所以只把后端查价范围扩到参考候选，
 * 用户还是点不到（负责人批注「定这个」要的是"参考候选也能查价"这件事真的能用）。
 *
 * 现在它渲染成和正式候选**同一张卡片**（``Alternative``），这份测试钉住三件事：
 *
 * 1. 卡片在、查价按钮在（按钮的文案与 disabled 来自同一个 ``priceButtonState``）；
 * 2. "为什么它是参考候选"那句话还在（``reference_reason`` 的中文文案一字未改）；
 * 3. 候选卡尾的来源块也在（任务 A 的来源网页对参考候选同样生效）。
 */
import assert from 'node:assert/strict'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

const CATALOG = 'https://www.yiqi.com/product/detail_1000001.html'
const DETAIL = 'https://www.xiangyilxj.com/product/info_itemid_1001.html'

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

function render(run) {
  return renderToStaticMarkup(React.createElement(ResearchResults, {
    run: {prices: [], sources: [], lanes: {}, gaps: [], alternatives: [], reference_candidates: [], ...run},
    baseline: {brand: '湘仪', model: 'XKA-2200', category: '离心机'}, section: 'alternatives',
  }))
}

const reference = (extra = {}) => ({
  candidate_id: '9a8b7c6d5e4f3021', brand: '', model: 'H1650K', category: '离心机',
  url: CATALOG, excerpt: '高速离心机-贺默H1650K台式微量高速离心机',
  reference_reason: 'brand_unresolved',
  source_pages: [{url: CATALOG, origin: 'discovered', primary: true},
                 {url: DETAIL, origin: 'matrix', primary: false}],
  price_lookup: {status: 'not_requested', reason_code: 'not_requested'}, prices: [],
  ...extra,
})

test('参考候选渲染成候选卡片：查价按钮在、编号带进按钮状态', () => {
  const html = render({reference_candidates: [reference()]})
  assert.ok(html.includes('alternative-result'), '参考候选不是候选卡片')
  assert.ok(html.includes('查这个的价格'), '参考候选卡片上没有查价按钮')
  // 有编号才给按钮（``priceButtonState`` 对空编号给的是禁用 + 提示）。
  assert.ok(!html.includes('这一轮数据里没有候选编号'), '参考候选居然被判成没有编号')
})

test('"为什么它是参考候选"那句话逐字保留，且印在卡片上方', () => {
  const html = render({reference_candidates: [reference()]})
  assert.ok(html.includes('品牌信息不完整，暂不能判断是否跨品牌。'), html.slice(0, 300))
  const note = html.indexOf('品牌信息不完整，暂不能判断是否跨品牌。')
  const card = html.indexOf('alternative-result')
  assert.ok(note > 0 && card > note, '原因那句没有排在卡片前面')
  // 同品牌参考那一档的说法也在（按 ``sameBrandAsBaseline`` 给 reference 参数）。
  const same = render({reference_candidates: [reference({brand: '湘仪', reference_reason: 'same_brand'})]})
  assert.ok(same.includes('属于原品牌，保留为同品牌参考。'), same.slice(0, 300))
})

test('参考候选卡片上也有来源块，主来源照样标出来', () => {
  const html = render({reference_candidates: [reference()]})
  const at = html.indexOf('price-record-sources')
  const block = at < 0 ? '' : html.slice(at, html.indexOf('</details>', at))
  assert.notEqual(block, '', '参考候选卡片上没有来源块')
  assert.ok(block.includes('<dt>发现来源（主来源）</dt>'), block)
  assert.ok(block.includes('仪器网 · 网页'), block)
})

test('参考候选没有编号的老数据：卡片照渲染，只是按钮禁用并说清原因', () => {
  const legacy = reference()
  delete legacy.candidate_id
  const html = render({reference_candidates: [legacy]})
  assert.ok(html.includes('alternative-result'))
  assert.ok(html.includes('这一轮数据里没有候选编号'), '没编号时应当说明为什么不能查')
})
