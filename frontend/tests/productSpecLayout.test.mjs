/**
 * 厂家资料那段正文 → 模块化网格：重复行只留一条、网页垃圾不进渲染、分组标题不当键值对、
 * 多列表头按行列对齐。
 *
 * 输入是**真实文本**：``fixtures/xka-2200-manufacturer-spec.txt`` 是隔离库
 * ``output/bio-instrument-acceptance-20260920/browser-runtime/acceptance.sqlite3`` 里
 * ``task-013dcdcff7e6``（湘仪 XKA-2200 台式离心机）那条 research 记录
 * ``prices[0].configuration`` 的**全文 2400 字**（``configuration_source=manufacturer_spec``，
 * ``configuration_url=https://www.xiangyilxj.com/product/info_itemid_952.html``）。
 * 不用假造的小样本：真实的脏法就是这三样——同一份参数被「表格按列展开」重复一遍、
 * 尾部整块「相关推荐」的产品名和裸链接、以及夹在中间的一行 5 列表头。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {after, test} from 'node:test'
import {fileURLToPath} from 'node:url'

import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {createServer} from 'vite'

import {productSpecLayout} from '../src/services/productSpecLayout.js'

//: ``core.autocrlf=true`` 且仓库没有 .gitattributes，签出时这个 txt 会带 CRLF；
//: 先归一化，字符数和行数的断言才在任何一台机器上都成立。
const SPEC = readFileSync(new URL('./fixtures/xka-2200-manufacturer-spec.txt', import.meta.url), 'utf8').replace(/\r\n?/g, '\n')
const SPEC_URL = 'https://www.xiangyilxj.com/product/info_itemid_952.html'

const layout = productSpecLayout(SPEC)
/** 会出现在页面上的全部文字。``discarded`` 是被判定为噪声而没渲染的行，不算输出。 */
const rendered = JSON.stringify({groups: layout.groups, notes: layout.notes})
const headings = layout.groups.map(group => group.title)
const parameters = layout.groups.flatMap(group => group.rows)
const rotorGroup = layout.groups.find(group => group.table) || {}
const rotorTable = rotorGroup.table || {headers: [], rows: [], numeric: []}

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: {middlewareMode: true}, appType: 'custom', logLevel: 'error',
})
const {default: ResearchResults} = await server.ssrLoadModule('/src/pages/ResearchResults.jsx')
after(() => server.close())

const render = price => renderToStaticMarkup(React.createElement(ResearchResults, {
  run: {prices: [price], sources: [], lanes: {}, alternatives: []},
  baseline: {brand: '湘仪', model: 'XKA-2200'}, section: 'price',
}))
const specRecord = render({
  kind: 'award', model: 'XKA-2200', brand: '湘仪', unit_price: 17480, currency: 'CNY',
  date: '2026-03-25', url: 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm',
  excerpt: '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480', gaps: [],
  configuration: SPEC, configuration_source: 'manufacturer_spec', configuration_url: SPEC_URL,
})

// ---------------------------------------------------------------------------
// 输入本身是脏的：先把三个特征钉死，免得清洗逻辑失效时测试还绿
// ---------------------------------------------------------------------------

test('the fixture is the real, dirty page text', () => {
  assert.equal(SPEC.length, 2400, '不是库里那 2400 字的原文')
  assert.equal(SPEC.split('\n').length, 74)
  assert.ok(SPEC.includes('留言板') && SPEC.includes('发 送') && SPEC.includes('相关推荐'))
  assert.ok(SPEC.includes('[https://www.xiangyilxj.com/product/info_itemid_2261.html]'))
  // 同一份参数在原文里出现两遍：直行一次，「表格按列展开」一次。
  assert.equal(SPEC.split('\n').filter(line => line.includes('最高转速')).length, 2)
  assert.equal(SPEC.split('\n').filter(line => line.startsWith('[表格按列展开')).length, 10)
})

// ---------------------------------------------------------------------------
// ① 重复行只出一条
// ---------------------------------------------------------------------------

test('a parameter repeated by the flattened table is rendered once', () => {
  assert.equal(layout.stats.parameters, 13)
  assert.equal(parameters.filter(row => row.key === '最高转速').length, 1)
  assert.deepEqual(parameters.find(row => row.key === '最高转速'), {key: '最高转速', value: '4500r/min'})
  // 10 条「表格按列展开」全部与上面逐字重复，一条都不该进渲染。
  const keys = parameters.map(row => row.key)
  assert.equal(new Set(keys).size, keys.length, '同一个键渲染了多次')
  for (const key of ['型号', '订货号', '最大相对离心力', '转子容量', '转速精度', '定时范围', '整机噪声', '电源', '整机功率', '外形尺寸', '包装尺寸', '重量']) {
    assert.equal(keys.filter(value => value === key).length, 1, `${key} 出现了不止一次`)
  }
  assert.equal(layout.discarded.filter(line => line.startsWith('[表格按列展开')).length, 10)
})

// ---------------------------------------------------------------------------
// ② 裸 URL 与「留言板 / 发送 / 相关推荐」不进渲染
// ---------------------------------------------------------------------------

test('page navigation and recommendations never reach the grid', () => {
  assert.ok(!rendered.includes('http'), '裸链接被渲染出来了')
  assert.ok(!rendered.includes('留言板'))
  assert.ok(!rendered.includes('发送'))
  assert.ok(!rendered.includes('相关推荐'))
  assert.ok(!rendered.includes('info_itemid_2261'))
  assert.ok(!rendered.includes('H300R大容量高速冷冻离心机'), '推荐位的产品名被当成参数渲染了')
  // 丢掉的只能是导航区和重复行：非展开行的丢弃项全部落在「留言板」这个导航锚点之后。
  const cut = SPEC.indexOf('留言板')
  const navigation = layout.discarded.filter(line => !line.startsWith('[表格按列展开'))
  assert.ok(navigation.length > 0)
  assert.ok(navigation.every(line => SPEC.indexOf(line) >= cut), '参数被当成导航区丢掉了')
  // 而真正的参数一个都不能被丢（重复的展开行不算——它们本来就与上面逐字相同）。
  for (const key of ['型号', '订货号', '转子容量', '包装尺寸']) {
    assert.ok(!navigation.some(line => line.includes(key)), `${key} 被当成噪声丢掉了`)
  }
})

// ---------------------------------------------------------------------------
// ③ 分组标题不是键值对
// ---------------------------------------------------------------------------

test('group headings become headings, not key/value rows', () => {
  assert.deepEqual(headings, ['技术参数', '转子参数'])
  assert.ok(!parameters.some(row => ['技术参数', '转子参数'].includes(row.key)))
  assert.ok(!parameters.some(row => ['技术参数', '转子参数'].includes(row.value)))
  assert.equal(layout.groups[0].rows.length, 13)
})

// ---------------------------------------------------------------------------
// ④ 5 列表头按行列对齐
// ---------------------------------------------------------------------------

test('the five-column table keeps its columns', () => {
  assert.deepEqual(rotorTable.headers, ['转子名称', '转子号', '离心力(xg)', '离心时间(s)', '用途'])
  assert.equal(rotorTable.rows.length, 6)
  for (const row of rotorTable.rows) assert.equal(row.length, 5)
  assert.deepEqual(rotorTable.rows[0], ['淋巴球清洗用转子/HLA', '1', '2000xg', '180s', '淋巴球分离,培养细胞分离'])
  assert.deepEqual(rotorTable.rows[5], ['红血球清洗用转子/SERO', '6', '1000xg', '60s', '血球清洗、血清、血浆的提取'])
  assert.equal(layout.stats.tableRows, 6)
  assert.equal(layout.stats.tableColumns, 5)
})

// ---------------------------------------------------------------------------
// ⑤ 相邻列不能被读成同一个值：数值列逐列标出来（页面据此右对齐 + 加列间隔）
// ---------------------------------------------------------------------------

test('数值列按数据判，不按表头猜：转子号/离心力/离心时间右对齐，名称和用途不右对齐', () => {
  // 表头里的 ``离心力(xg)`` 自带单位，值却是 ``2000xg``——按表头猜会猜错，所以判据是
  // 「这一列每个非空格子都长得像数字＋单位符号」。
  assert.deepEqual(rotorTable.numeric, [false, true, true, true, false])
  // 名称列里的一格带着 ``/HLA``——它按定义不是数值列，不然「淋巴球清洗用转子/HLA」
  // 会和右边的「1」一起被右对齐贴到一块。
  assert.equal(rotorTable.numeric[0], false)
})

test('页面把数值列标成 is-numeric，名称列一格都不标', () => {
  const card = specRecord.slice(specRecord.indexOf('product-spec-table'))
  const body = card.slice(card.indexOf('<tbody>'), card.indexOf('</tbody>'))
  assert.equal((body.match(/class="is-numeric"/g) || []).length, 18, '6 行 × 3 个数值列')
  assert.ok(!body.includes('<td class="is-numeric">淋巴球清洗用转子/HLA</td>'), '名称列被当成数值列了')
  assert.ok(!body.includes('<td class="is-numeric">淋巴球分离,培养细胞分离</td>'), '用途列被当成数值列了')
  assert.ok(body.includes('<td>淋巴球清洗用转子/HLA</td>'), '名称列不再是一格普通单元格')
  // 表头也要跟着右对齐，否则「转子号」这个标题贴在列左、它的值贴在列右，中间空一片。
  const head = card.slice(card.indexOf('<thead>'), card.indexOf('</thead>'))
  assert.equal((head.match(/class="is-numeric"/g) || []).length, 3, '数值列的表头没跟着右对齐')
})

test('列间隔写在 CSS 里：内边距、相邻列竖线、数值列右对齐，一样都不能少', () => {
  // 这条不是样式洁癖：截图里「淋巴球清洗用转子/HLA」后面紧跟着「1」，读起来就是
  // 「HLA1」（实际是两个字段）。三样东西一起给间隔，少一样就只剩"看着像有缝"。
  const css = readFileSync(new URL('../src/styles/results.css', import.meta.url), 'utf8').replace(/\r\n?/g, '\n')
  const tableRules = css.slice(css.indexOf('.product-spec-table table'), css.indexOf('.product-spec-note'))
  assert.match(tableRules, /padding: 8px 14px/, '格子没有左右内边距')
  assert.match(tableRules, /:is\(th, td\) \+ :is\(th, td\) \{ border-left: 1px solid var\(--line\); \}/, '相邻列之间没有竖线')
  assert.match(tableRules, /td\.is-numeric,\n\.product-spec-table th\.is-numeric \{ text-align: right/, '数值列没有右对齐')
  assert.match(tableRules, /border-collapse: collapse/, '表格还是默认的分离边框')
})

test('the trailing note stays a note and nothing else is left over', () => {
  assert.deepEqual(layout.notes, ['*所有产品都可根据客户要求定制、设计、生产。'])
  // 51 = 10 条重复的展开行 + 41 行尾部导航/推荐，其余全部渲染。
  assert.equal(layout.discarded.length, 51)
  assert.equal(layout.stats.discarded, 51)
})

// ---------------------------------------------------------------------------
// 渲染：页面上真的是一格一格的网格，而不是一串「键 | 值」
// ---------------------------------------------------------------------------

test('the card renders a grid of cells, not one line per key', () => {
  // T6（2026-10-07）：标题从「厂家标称参数（非公告内容）」改成「该型号产品资料的
  // 标称参数（非成交配置）」——业务口径是把"该型号产品资料的标称参数"与"这条成交
  // 记录里写的配置"分开，光说"非公告内容"没把这层说透。
  assert.ok(specRecord.includes('该型号产品资料的标称参数（非成交配置）'), '标题不对')
  // 配置来源的链接在卡尾的「来源」里，文字是"指向哪里"的简述，不是 URL。
  assert.ok(specRecord.includes('湘仪集团官网 · 产品页'), '原文链接没了')
  assert.ok(specRecord.includes(SPEC_URL), '原文链接地址没了')
  // 这一条只在产品参数块里找：规范项 的「向销售索要的条件」话术块用的是 ``<pre>``
  // （整段复制的前提），那是另一个组件，不该被这条断言牵连。
  const specCard = specRecord.slice(specRecord.indexOf('product-spec-card'), specRecord.indexOf('price-record-sources'))
  assert.ok(!specCard.includes('<pre'), '还是原来的 <pre> 原样铺开')
  assert.ok(!specRecord.includes('最高转速 | 4500r/min'), '键值对还是以「键 | 值」串行渲染')
  assert.ok(!specRecord.includes('留言板') && !specRecord.includes('相关推荐'))
  assert.ok(!specRecord.includes('info_itemid_2261'), '推荐位的裸链接进了页面')

  // 「配置卡」这一段：卡尾接着的是这条记录第三层的「来源」（价格来源 / 配置来源，
  // 默认折叠），它自带 <dt>/<dd>，不属于配置卡，所以切片到它为止。不这么切，
  // 下面数参数格子的断言会把来源层的两个 <dt> 也算进来。
  const cardStart = specRecord.indexOf('product-spec-card')
  const sourcesAt = specRecord.indexOf('price-record-sources')
  const card = specRecord.slice(cardStart, sourcesAt > cardStart ? sourcesAt : undefined)
  assert.equal((card.match(/<dt>/g) || []).length, 13, '参数格子数量不对')
  assert.equal((card.match(/class="product-spec-group-title"/g) || []).length, 2, '分组小标题数量不对')
  assert.equal((card.match(/<th scope="col"/g) || []).length, 5, '多列表头列数不对')
  const body = card.slice(card.indexOf('<tbody>'), card.indexOf('</tbody>'))
  assert.equal((body.match(/<tr>/g) || []).length, 6, '表格数据行数不对')
  // 同一份数据只出现一次：原文两处 4500r/min，页面上只能有一处。
  assert.equal((specRecord.match(/4500r\/min/g) || []).length, 1)
  // 参数格子复用的就是「本次核价对象」那块的网格类，视觉语言是同一套。
  assert.match(card, /<dl class="price-target-terms product-spec-grid">/)
})

test('the spec card is the second block inside the same price record card', () => {
  // 业务口径：型号价格和配置是**同级的**，同在一张卡里——不再另开一张卡。
  const recordEnd = specRecord.indexOf('</article>')
  const cardAt = specRecord.indexOf('product-spec-card')
  const firstBlock = specRecord.indexOf('price-record-identity')
  assert.ok(recordEnd > 0 && cardAt > 0 && cardAt > firstBlock, '配置块不在型号价格那一行之后')
  assert.ok(cardAt < recordEnd, '配置块又被画到价格卡外面去了')
})

// ---------------------------------------------------------------------------
// 解析器本身：空输入、纯噪声、只有表头的表格
// ---------------------------------------------------------------------------

test('nothing to lay out yields nothing to render', () => {
  for (const value of ['', '   ', null, undefined, '留言板\n发 送\n相关推荐\n[http://a.example/x]']) {
    const empty = productSpecLayout(value)
    assert.deepEqual(empty.groups, [])
    assert.deepEqual(empty.notes, [])
  }
})

test('a repeated key with a different value is real information and is kept', () => {
  const both = productSpecLayout(['转子参数', '最高转速 | 4000r/min', '最高转速 | 6000r/min'].join('\n'))
  assert.deepEqual(both.groups[0].rows, [{key: '最高转速', value: '4000r/min'}, {key: '最高转速', value: '6000r/min'}])
})

test('a table header without any data row is not rendered as a table', () => {
  const header = productSpecLayout(['技术参数', '型号 | XKA-2200', '转子名称 | 转子号 | 离心力(xg) | 离心时间(s) | 用途'].join('\n'))
  assert.equal(header.stats.tableRows, 0)
  assert.equal(header.stats.parameters, 1)
  assert.deepEqual(header.discarded, ['转子名称 | 转子号 | 离心力(xg) | 离心时间(s) | 用途'])
})

// ---------------------------------------------------------------------------
// 空壳：整块不渲染（设计规范，用户批注"要改"）
// ---------------------------------------------------------------------------

//: 真跑里存下来的那一类"配置"：一条产品页的标题与导航，没有一个可对齐的参数条目
//: （``BXM-30R`` 那页 shboxun.com.cn 是 5152 字、记录里存的却是标题＋导航）。
const EMPTY_SHELL = ['全自动高压蒸汽灭菌器 - 上海博迅医疗生物仪器股份有限公司',
                     '首页 | 产品中心 | 新闻中心 | 联系我们',
                     '欢迎致电 400-000-0000', '版权所有 © 上海博迅'].join('\n')

test('一个参数组都分不出来时整块不渲染（标题、口径说明、空壳句都不留）', () => {
  assert.deepEqual(productSpecLayout(EMPTY_SHELL).groups, [], '这条样本不再是空壳了，换一条真实文本')
  const html = render({
    kind: 'award', model: 'BXM-30R', brand: '博迅', unit_price: 3980, currency: 'CNY',
    date: '2025-04-01', url: 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202504/t20250401_1.htm',
    excerpt: '品牌：博迅\n规格型号：BXM-30R\n数量：1\n单价(元)：3980', gaps: [],
    configuration: EMPTY_SHELL, configuration_source: 'product_page_spec', configuration_url: SPEC_URL,
  })
  assert.ok(!html.includes('product-spec-card'), '空壳块还在渲染')
  assert.ok(!html.includes('该型号产品资料的标称参数'), '空壳时标题还在')
  assert.ok(!html.includes('厂家资料正文里没有可对齐的参数条目'), '空壳句还在（整块应当不渲染）')
  // 块没了不等于出处也没了：卡尾「来源」里那条配置来源链接照旧（默认折叠），
  // 要核对原文随时点得开——所以"不渲染"没有把可追溯性一起删掉。
  assert.ok(html.includes(SPEC_URL), '配置来源链接跟着块一起没了')
  assert.ok(html.includes('配置来源'), '配置来源那一行没了')
})

test('有参数组时照旧渲染，口径说明一个字不少', () => {
  // 上一条是"空壳不渲染"，这一条把另一头钉住：别把有参数的也一起吞了。
  assert.ok(specRecord.includes('该型号产品资料的标称参数（非成交配置）'))
  assert.ok(specRecord.includes('上的标称参数，不是成交配置'))
  assert.ok(specRecord.includes('product-spec-grid'))
})
