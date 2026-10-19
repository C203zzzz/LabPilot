/**
 * 同一个型号有多条成交记录时的**编号与顺序**。
 *
 * 项目负责人的话：「一个型号有三条记录就是三张卡原样往下堆，谁也不告诉用户共有
 * 几条、现在是第几条。」区块标题 ``<h3>可比历史成交记录</h3>`` **已经在
 * ``prices.map`` 外面、只渲染一次**，所以编号只能是每张卡自己的一个小标记
 * （「第 n 条 / 共 m 条」）——**不许再给每条记录加一遍标题**（用户明确抱怨过
 * 「又重复一次可比历史成交记录」）。这份测试把这两条同时钉住：编号必须在，
 * 标题必须只有一次。
 *
 * 顺序为什么是"成交日期从新到旧"：``run['prices']`` 的顺序不是价格顺序。
 * ``backend/services/procurement_research.py`` 里 ``_merged_prices``（520-536 行）→
 * ``_merge_price_rows``（497-517 行）是"先见先留"的并集（docstring 第一句：
 * "The first reading of a transaction keeps its position"），而 ``prices()`` 扫来源
 * 用的 ``_price_source_rank``（定义 333-345 行，``sorted(sources, key=…)`` 的调用处
 * 在 3261 行）那段 docstring 写的是 "Order in which a price **budget spends its
 * calls** on eligible sources"——它是**"预算先花在谁身上"的排序，不是价格排序**。
 * 也就是说：谁在第一行取决于这轮检索先扫到哪个来源，跟金额、日期都无关。所以页面
 * 自己定一个说得出口的顺序，否则「第 1 条」会因为一次无关的检索顺序变化而换人。
 */
import assert from 'node:assert/strict'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

/** 三条同型号记录，故意**按乱序**给（最旧的排在最前面），日期各不相同。 */
const records = [
  {kind: 'award', model: 'XKA-2200', brand: '湘仪', unit_price: 10000, currency: 'CNY',
   date: '2024-05-06', url: 'https://a.example/old', excerpt: '最旧的一条'},
  {kind: 'award', model: 'XKA-2200', brand: '湘仪', unit_price: 30000, currency: 'CNY',
   date: '2026-03-25', url: 'https://a.example/new', excerpt: '最新的一条'},
  {kind: 'contract', model: 'XKA-2200', brand: '湘仪', unit_price: 20000, currency: 'CNY',
   date: '2025-01-08', url: 'https://a.example/mid', excerpt: '中间那条'},
]

const render = prices => renderToStaticMarkup(React.createElement(ResearchResults, {
  run: {prices, sources: [], lanes: {}, alternatives: []},
  baseline: {brand: '湘仪', model: 'XKA-2200'}, section: 'price',
}))

/** 会显示给人看的文字：标签与属性都不参与。 */
const visibleText = html => html.replace(/<[^>]*>/g, ' ')

test('三条记录：按成交日期从新到旧排，每条带自己的序号', () => {
  const html = render(records)
  assert.ok(html.indexOf('30,000 元') < html.indexOf('20,000 元'), '最新的没排在前面')
  assert.ok(html.indexOf('20,000 元') < html.indexOf('10,000 元'), '日期从新到旧的顺序不对')
  // 编号跟着排出来的顺序走：日期最新的那条是第 1 条。
  assert.deepEqual((html.match(/第 \d+ 条 \/ 共 3 条/g) || []), ['第 1 条 / 共 3 条', '第 2 条 / 共 3 条', '第 3 条 / 共 3 条'])
  const firstAt = html.indexOf('30,000 元')
  const ordinalAt = html.indexOf('第 1 条 / 共 3 条')
  const secondAt = html.indexOf('20,000 元')
  assert.ok(ordinalAt > firstAt && ordinalAt < secondAt, '「第 1 条」没跟最新那条排在同一张卡里')
})

test('只有一条记录时不编号（一条记录谈不上第几条）', () => {
  const html = render([records[1]])
  assert.ok(!html.includes('第 1 条'), '单独一条记录也印了编号')
  assert.ok(html.includes('30,000 元'), '这条记录本身没渲染')
})

test('区块标题只渲染一次，不是每条记录头上再来一遍', () => {
  const html = render(records)
  const shown = visibleText(html)
  assert.equal((shown.match(/可比历史成交记录/g) || []).length, 1,
    '「可比历史成交记录」在页面文字里出现了不止一次：' + shown.slice(shown.indexOf('可比历史成交记录') - 40, shown.indexOf('可比历史成交记录') + 200))
  assert.equal((html.match(/<h3>可比历史成交记录<\/h3>/g) || []).length, 1, '标题不止一个')
  // 卡片自己用的是 <h4>（价格）与配置块的 <h4>/<h5>，没有一条记录重新起「可比历史成交记录」这个标题。
  assert.ok(!/<h[1-6][^>]*>可比历史成交记录<\/h[1-6]>/.test(html.replace('<h3>可比历史成交记录</h3>', '')))
})

test('日期读不出来的记录排在最后，并保持后端原来的顺序', () => {
  const undated = [
    {kind: 'award', model: 'XKA-2200', unit_price: 10000, date: '2026-01-01', url: 'https://a.example/d'},
    {kind: 'award', model: 'XKA-2200', unit_price: 20000, url: 'https://a.example/u1'},
    {kind: 'award', model: 'XKA-2200', unit_price: 30000, date: '', url: 'https://a.example/u2'},
  ]
  const html = render(undated)
  assert.ok(html.indexOf('10,000 元') < html.indexOf('20,000 元'), '有日期的那条没有排到最前')
  assert.ok(html.indexOf('20,000 元') < html.indexOf('30,000 元'), '两条没日期的记录没有保持后端原序')
  // 不能因为读不出日期就把它当成"最新"。
  assert.ok(html.indexOf('10,000 元') < html.indexOf('30,000 元'))
})

test('不是成交记录（挂牌价之类）的一条都不进这个区块，编号只数成交记录', () => {
  const html = render([...records, {kind: 'listing', model: 'XKA-2200', unit_price: 99000, url: 'https://a.example/listing'}])
  assert.ok(!html.includes('99,000 元'), '挂牌价被当成成交记录渲染了')
  assert.deepEqual((html.match(/第 \d+ 条 \/ 共 \d+ 条/g) || []), ['第 1 条 / 共 3 条', '第 2 条 / 共 3 条', '第 3 条 / 共 3 条'])
})

test('备选卡片「该型号价格依据（N 条）」的 N 与实际列出的条数、编号分母一致', () => {
  // 摘要原来数的是 ``candidate.prices.length``（连挂牌价一起数），列出来的却是
  // 过滤后的成交记录。以前看不出问题（列表里没有总数），加了「共 m 条」以后两者
  // 必须对得上，否则一眼就是「（3 条）」下面只有两张卡。
  const html = renderToStaticMarkup(React.createElement(ResearchResults, {
    run: {prices: [], sources: [], lanes: {}, alternatives: [{
      brand: '某厂', model: 'YX-100', url: 'https://example.com/yx-100', matrix: [],
      prices: [
        {kind: 'listing', model: 'YX-100', unit_price: 99000, url: 'https://a.example/listing'},
        {kind: 'award', model: 'YX-100', unit_price: 10000, date: '2024-05-06', url: 'https://b.example/old'},
        {kind: 'award', model: 'YX-100', unit_price: 30000, date: '2026-03-25', url: 'https://b.example/new'},
      ],
    }]},
    baseline: {brand: '湘仪', model: 'XKA-2200'}, section: 'alternatives',
  }))
  assert.ok(html.includes('该型号价格依据（2 条）'),
    '摘要的条数把非成交记录也算进去了：' + JSON.stringify(html.match(/该型号价格依据（[^）]*）/)))
  assert.ok(!html.includes('99,000 元'), '挂牌价被当成成交记录渲染了')
  assert.deepEqual((html.match(/第 \d+ 条 \/ 共 \d+ 条/g) || []), ['第 1 条 / 共 2 条', '第 2 条 / 共 2 条'])
  assert.ok(html.indexOf('30,000 元') < html.indexOf('10,000 元'), '备选卡片里的记录没按日期从新到旧排')
})
