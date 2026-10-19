/**
 * 一张价格记录卡的三块排法（项目负责人转述的用户口径）。
 *
 * 用户原话：「型号 价格 / 下方是模块化的展示配置 / 再下一层是显示来源（需点击显示
 * 默认隐藏）/ 点击后提供公告的链接（价格来源）和厂家信息页的链接（配置来源）」，
 * 批注补充两条：**型号价格和配置是同级的两块、同在一张卡里**；**链接不要展示出来，
 * 而是简述它指向哪里**（页面上任何默认可见文字里都不出现 ``http://``／``https://``）。
 *
 * 这份测试真的把 ``ResearchResults`` 渲染出来数 HTML（vite 的 ``ssrLoadModule``
 * 转 JSX，``react-dom/server`` 出 HTML），要钉住的：
 *
 * 1. 第一块是型号与价格**同一行**（原来价格在上、型号下面单独占一行）；
 * 2. 配置是同一张卡里的第二块，不再另开一张卡；
 * 3. 卡尾「来源」默认折叠，标题写明各类各有几条；
 * 4. 链接文字是「站点名 · 页面类型」的简述，可见文字与 ``title`` 里都没有 URL；
 * 5. 没有地址的条目一条都不渲染（没有占位符、没有"未提供链接"），两条都没有时
 *    这一块整个不存在；
 * 6. 每条只占一行：``<dt>`` 已经写明这是哪一类来源，行内不再重述一遍（原来的
 *    「这条成交金额的原始出处：」和「这段配置参数来自厂家资料，不是公告内容。出处：」
 *    是同一件事在一行里说两遍）。
 */
import assert from 'node:assert/strict'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

const PRICE_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260301_10000001.htm'
const SPEC_URL = 'https://www.xiangyilxj.com/product/info_itemid_1001.html'
const SPEC_TEXT = ['技术参数', '型号 | XKA-2200', '最高转速 | 4500r/min'].join('\n')
const CAVEAT = '也不是招标文件对投标产品提出的门槛'

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

function record(overrides = {}) {
  return {
    kind: 'award', model: 'XKA-2200', brand: '湘仪', unit_price: 17480, currency: 'CNY',
    date: '2026-03-25', url: PRICE_URL, excerpt: '品牌：湘仪\n规格型号：XKA-2200\n单价(元)：17480',
    gaps: [], ...overrides,
  }
}

const render = price => renderToStaticMarkup(React.createElement(ResearchResults, {
  run: {prices: [price], sources: [], lanes: {}, alternatives: []},
  baseline: {brand: '湘仪', model: 'XKA-2200'}, section: 'price',
}))

const full = () => render(record({configuration: SPEC_TEXT, configuration_source: 'manufacturer_spec',
                                  configuration_url: SPEC_URL}))

/** 第一块那个 div 的内容。里面没有嵌套 div，第一个 </div> 就是它的结束。 */
function firstRow(html) {
  const at = html.indexOf('price-record-identity')
  return at < 0 ? '' : html.slice(at, html.indexOf('</div>', at))
}

function sourcesBlock(html) {
  const at = html.indexOf('price-record-sources')
  return at < 0 ? '' : html.slice(at, html.indexOf('</details>', at))
}

/** 会显示给人看的文字：把所有标签剥掉，属性（含 href）不参与。 */
function visibleText(html) {
  return html.replace(/<[^>]*>/g, ' ')
}

test('第一块：型号与价格并排在同一行里，型号在前', () => {
  const html = full()
  const row = firstRow(html)
  assert.ok(row.includes('XKA-2200'), '型号没在第一块里：' + row)
  assert.ok(row.includes('17,480 元'), '价格没在第一块里：' + row)
  assert.ok(row.indexOf('XKA-2200') < row.indexOf('17,480 元'), '型号与价格的顺序反了')
  // 原来价格下面那一行单独的「成交型号」没有了：同一件事不再同一张卡里印两遍。
  assert.equal((html.match(/成交型号/g) || []).length, 1)
})

test('第二块：配置与型号价格同在一张卡里，不是另一张卡', () => {
  const html = full()
  const cardAt = html.indexOf('product-spec-card')
  assert.ok(cardAt > 0, '配置块没渲染')
  assert.ok(cardAt > html.indexOf('price-record-identity'), '配置块排在型号价格前面了')
  assert.ok(cardAt < html.indexOf('</article>'), '配置块被画到价格卡外面去了')
  assert.ok(html.includes('product-spec-grid'), '配置不是模块化的格子')
})

test('第三块：来源默认折叠，折叠标题写明各类各有几条', () => {
  const html = full()
  assert.match(html, /<details class="price-record-sources">/, '来源不是折叠块')
  assert.ok(!html.includes('<details class="price-record-sources" open'), '来源默认展开了')
  assert.ok(html.includes('<summary>来源（价格 1 · 配置 1）</summary>'), '折叠标题看不出有几个来源')
  assert.ok(html.indexOf('price-record-sources') < html.indexOf('</article>'), '来源块不在价格卡里')
})

test('两条链接分开写，文字是"指向哪里"的简述而不是 URL', () => {
  const html = full()
  const block = sourcesBlock(html)
  assert.ok(block.includes('<dt>价格来源</dt>'), '没有价格来源这一条')
  assert.ok(block.includes('<dt>配置来源</dt>'), '没有配置来源这一条')
  assert.ok(block.includes('中国政府采购网 · 中标公告'), '价格来源的简述不对：' + block)
  assert.ok(block.includes('湘仪集团官网 · 产品页'), '配置来源的简述不对：' + block)
  assert.ok(block.includes(PRICE_URL) && block.includes(SPEC_URL), 'href 里的真实地址丢了（链接点不开）')
})

test('来源每条只有一行：标签已经写明是哪一类，行内不再重述一遍', () => {
  // 用户口径：同一件事不说两遍。原来两条 <dd> 是
  // 「这条成交金额的原始出处：＋链接」和「这段配置参数来自厂家资料，不是公告内容。出处：＋链接」，
  // ``<dt>`` 上的「价格来源 / 配置来源」把同一件事又说了一遍。
  const block = sourcesBlock(full())
  assert.ok(!block.includes('原始出处'), '价格来源那一行还把"原始出处"重述了一遍')
  assert.ok(!block.includes('不是公告内容'), '配置来源那一行还把"不是公告内容"重述了一遍')
  // 保留下来的：两类的标签、两个"指向哪里"的中文简述、两个真实地址。
  assert.match(block, /<dt>价格来源<\/dt><dd><a class="source-link"[^>]*>中国政府采购网 · 中标公告<\/a><\/dd>/)
  assert.match(block, /<dt>配置来源<\/dt><dd><a class="source-link"[^>]*>湘仪集团官网 · 产品页<\/a><\/dd>/)
})

test('默认可见的文字与悬停提示里都没有 http(s):// 或英文域名', () => {
  const html = full()
  assert.ok(!/>\s*https?:\/\//.test(html), '有文字节点直接显示了 URL')
  assert.ok(!/title="[^"]*https?:\/\//.test(html), '悬停提示里还挂着完整 URL')
  // href 里当然还是完整地址（链接要能点开）；这里看的是**给人看的文字**。
  const shown = visibleText(html)
  assert.ok(!shown.includes('www.xiangyilxj.com'), '英文域名还印在页面上')
  assert.ok(!shown.includes('www.ccgp.gov.cn'), '英文域名还印在页面上')
  assert.ok(!/\bhttps?:\/\/\S+/.test(shown), '去掉标签后仍能看到 URL')
})

test('配置卡那句防混淆的话不因折叠而丢', () => {
  const html = full()
  const card = html.slice(html.indexOf('product-spec-card'), html.indexOf('price-record-sources'))
  // 三层区分逐层钉住：是厂家标称值 / 不是政府采购公告里写的配置 / 不是招标文件的投标门槛。
  assert.ok(card.includes('厂家资料（湘仪集团官网）上这个型号的标称值'), '第一层（是标称值）没了：' + card)
  assert.ok(card.includes('不是政府采购公告里写的配置'), '第二层（不是公告写的配置）没了')
  assert.ok(card.includes(CAVEAT), '第三层（不是招标门槛）没了')
})

test('区块引言把判据写成"收录标准"，与空态那句指回的说法对得上', () => {
  // 空态那句是「没有一条金额同时满足上面列出的收录标准」——引言里必须有"收录标准"这四个字，
  // 指回才找得到；判据四项与"不齐的仍列出"一句都不能省。
  const html = render(record({kind: 'listing'}))
  assert.ok(html.includes('收录标准：品牌、完整型号、单价、公告日期都能在原文中查到。'))
  assert.ok(html.includes('条件不齐的仍会列出并标注缺口。'))
  assert.ok(html.includes('上面列出的收录标准'), '空态没有指回上面那段判据')
  assert.ok(!html.includes('只列出原文能定位到'), '原来那句口语化的说法还在')
})

test('卡尾边界句：尾巴收短，判断条件一个没少', () => {
  const html = render(record({configuration: 'unknown'}))
  assert.ok(html.includes('这是同型号历史成交记录，不是当前报价。'))
  assert.ok(html.includes('与销售报价有差异或缺失：'), '比较基准（销售报价）和"差异或缺失"都要在')
  assert.ok(html.includes('请以原文为准。'))
  assert.ok(!html.includes('请结合原文自行判断'), '原来那句没换掉')
  assert.ok(!html.includes('存在差异或信息缺失'), '原来那句没换掉')
})

test('没有配置时不渲染配置块，来源里也没有配置这一条', () => {
  const html = render(record({configuration: 'unknown'}))
  assert.ok(!html.includes('product-spec-card'), '没有配置却渲染了配置块')
  assert.ok(!html.includes('<dt>配置来源</dt>'), '没有配置来源却列了一条')
  assert.ok(!html.includes('配置 1'), '折叠标题里凭空多了一条配置来源')
  assert.ok(html.includes('<summary>来源（价格 1）</summary>'), '价格来源那条被漏掉了')
})

test('一条可用地址都没有时，整块不渲染（不印占位内容）', () => {
  const html = render(record({url: '', configuration: 'unknown'}))
  assert.ok(!html.includes('price-record-sources'), '没有来源却渲染了来源块')
  assert.ok(!html.includes('来源（'), '没有来源却印了条数')
  assert.ok(!/未提供链接|来源网址未提供|暂无来源/.test(html), '印了假占位内容')
})

test('配置来源地址不是合法网址时，不渲染一条点不开的假链接', () => {
  // 后端字段里存的可能是没有协议的裸域名，那种值 SourceLink 会退化成一段文字，
  // 看着像链接却打不开——宁可不列，也不能列。
  const html = render(record({configuration: SPEC_TEXT, configuration_source: 'manufacturer_spec',
                              configuration_url: 'www.xiangyilxj.com/product/info_itemid_1001.html'}))
  assert.ok(!html.includes('<dt>配置来源</dt>'), '列了一条点不开的配置来源')
  assert.ok(html.includes('<summary>来源（价格 1）</summary>'))
  assert.ok(html.includes('product-spec-card'), '配置块本身照旧显示')
})

test('价格来源地址不是合法网址时，同样不列', () => {
  const html = render(record({url: 'javascript:alert(1)', configuration: 'unknown'}))
  assert.ok(!html.includes('price-record-sources'), '非法地址被渲染成来源了')
})

test('合同类记录的页面类型说成采购合同公告', () => {
  const html = render(record({kind: 'contract', url: 'https://ggzyjy.sc.gov.cn/a/b.html',
                              configuration: 'unknown'}))
  assert.ok(html.includes('四川省公共资源交易信息网 · 采购合同公告'), '页面类型不对：' + sourcesBlock(html))
})
