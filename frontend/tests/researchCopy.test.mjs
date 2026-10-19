/**
 * researchCopy 的失败代号处理。
 *
 * 这份是报告页「影响采购判断的未完成事项 / 注意事项」那一块真正用的渲染入口
 * （`ResearchIssues.jsx` → `researchIssue()`），publicText 不在这条路径上。
 *
 * 它和 publicText 的差别是：这里把每一条缺口**归并成一句给人看的中文话术**，认不出来
 * 的才原样保留（else 分支）。所以漏不漏，取决于「代号有没有被某个分支认出来」。
 *
 * 数据来源：output/ 下 368 个含 gaps 的 json、2968 条 gap 字符串全量跑过一遍。
 *   落进 else 兜底分支的 369 条里，9 条是 RemoteDisconnected（英文原名整段印给用户），
 *   48 条是型号名开头的中文说明（PF-22／MH2100R／HT190R），其余是本就通顺的中文句子。
 *   http_<状态码> 从没进过 run.gaps，tls_* 与 dns_* / empty_* 已经被既有分支认掉。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import { BUDGET_EXCEEDED_MESSAGE, groupResearchIssues, researchIssue } from '../src/services/researchCopy.js'

test('连接被对方中断的类名不再把英文原样印给用户', () => {
  // 原文照抄 output/acceptance-57 等产物里的 run.gaps。
  const raw = '该来源连续两次无法读取（RemoteDisconnected），其他来源可正常访问；保留URL及原始失败原因待补查（不等于未检索到产品）：https://bbs.eeworld.com.cn/thread-1000000-1-1.html'
  const { message, urls } = researchIssue(raw)
  assert.ok(!message.includes('RemoteDisconnected'), message)
  assert.equal(message, '与来源网站的连接被中途断开，尚未读取资料。可以尝试手动打开来源。')
  // URL 仍然作为「相关来源」被保留下来，只是不再混在正文里。
  assert.deepEqual(urls, ['https://bbs.eeworld.com.cn/thread-1000000-1-1.html'])
  assert.ok(!message.includes('https://'))
})

test('同类传输层异常名一起认掉', () => {
  for (const name of ['ConnectionResetError', 'ConnectionAbortedError', 'ConnectionRefusedError']) {
    const { message } = researchIssue('聚合站线索指向的官方原文未读到：' + name)
    assert.ok(!message.includes(name), message)
    assert.ok(message.includes('连接被中途断开'), message)
  }
})

test('既有分支的翻译结果逐字不变', () => {
  const cases = [
    ['no_official_source', '未找到可核实的厂家原始资料，产品参数还不能确认。'],
    ['tender_body_unverified: http://a.gov.cn/x.htm', '找到采购公告线索，但尚未核实公告正文与该型号的成交信息。'],
    ['empty_results', '部分检索没有找到相关资料，尚不能据此作出价格或替代判断。'],
    ['provider_budget_exceeded', BUDGET_EXCEEDED_MESSAGE],
    ['读取失败（http_403）', '网站限制自动读取，尚未取得可核实内容。可以尝试手动打开来源。'],
    ['读取失败（tls_BAD_ECPOINT）', '与来源网站的安全连接未能建立，尚未读取资料。可以尝试手动打开来源。'],
    ['读取失败（dns_not_found）', '来源网站暂时无法连接，尚未读取资料。可以尝试手动打开来源。'],
    ['读取失败（empty_body）', '来源网页没有返回可读取的正文，内容尚未核实。'],
    ['聚合站线索指向的官方原文未读到：TimeoutError', '部分检索或读取未完成，相关结论仍缺少依据。已有结果已保留。'],
    ['替代产品核查未完成', '替代品的关键参数尚未核全，目前不能判断能否替代。'],
    ['核查其他品牌未完成', '替代品的关键参数尚未核全，目前不能判断能否替代。'],
    ['同款价格核查未完成', '尚未取得可核实的同型号价格，当前无法判断销售报价是否合理。'],
    ['核查同款价格未完成', '尚未取得可核实的同型号价格，当前无法判断销售报价是否合理。'],
    ['读取失败（some_internal_field）', '部分资料尚未完成核验，暂不能用于采购结论。'],
  ]
  for (const [input, expected] of cases) {
    assert.equal(researchIssue(input).message, expected, `「${input}」的翻译结果变了`)
  }
})

test('额度用尽那一句必须说清三件事：是上限不是故障、什么时候能再试、结果不会丢', () => {
  // 原来的「本轮分析因服务额度限制中断，已取得的结果仍可查看。」读起来像产品坏了；
  // 同一件事的三个出口（研究缺口、问答接口、需求访谈接口）当时各说各的。
  for (const word of ['用量上限', '不是故障', '重试', '结果']) {
    assert.ok(BUDGET_EXCEEDED_MESSAGE.includes(word), `这句话少了「${word}」：${BUDGET_EXCEEDED_MESSAGE}`)
  }
  // 三处走的是同一个常数，不是各自抄一份中文。
  for (const raw of ['provider_budget_exceeded', '本轮资料分析达到十二次调用上限', '共享额度已用完', 'budget_exceeded']) {
    assert.equal(researchIssue(raw).message, BUDGET_EXCEEDED_MESSAGE, `「${raw}」没有走到统一那句`)
  }
})

test('兜底分支保持原样：认不出来的中文缺口照原样留着，只去掉 URL', () => {
  // 这一类在产物里有 360 条，把兜底换成一个通用说法会把它们全丢掉 —— 这条盯着它。
  assert.equal(researchIssue('本轮未取得可核实的同型号成交单价。').message, '本轮未取得可核实的同型号成交单价。')
  assert.equal(
    researchIssue('MH2100R：发现来源的参数低于要求，仅保留线索，不占用深入核验名额；未作为正式不合格结论。').message,
    'MH2100R：发现来源的参数低于要求，仅保留线索，不占用深入核验名额；未作为正式不合格结论。',
  )
  assert.equal(researchIssue('模型返回空内容').message, '模型返回空内容')
  const withUrl = researchIssue('正文读取额度已用完，保留已取得的来源继续分析：https://a.gov.cn/x')
  assert.equal(withUrl.message, '正文读取额度已用完，保留已取得的来源继续分析')
  assert.deepEqual(withUrl.urls, ['https://a.gov.cn/x'])
})

test('五个代号不再塌成同一句通用话术', () => {
  // 原来这五个都被 snake_case 形状分支收走，全都吐「部分资料尚未完成核验，暂不能用于采购结论。」，
  // 用户读那一列分不出「页面过大」和「来源已不存在」。
  const codes = ['unsupported_source_url', 'parse_incomplete', 'page_too_large', 'empty_shell', 'not_found']
  const messages = codes.map(code => researchIssue(`该来源连续两次无法读取（${code}），其他来源可正常访问；保留URL及原始失败原因待补查`).message)
  assert.equal(new Set(messages).size, codes.length, '有代号还在吐同一句话：' + JSON.stringify(messages))
  for (const message of messages) {
    assert.ok(!/[A-Za-z]+_[A-Za-z_]+/.test(message), '话术里还留着下划线代号：' + message)
    assert.ok(!/暂不能用于采购结论/.test(message), '还是通用的形状分支话术：' + message)
  }
  assert.equal(researchIssue('读取失败（page_too_large）').message, '页面过大，未能完整读取，请提供具体明细页。')
  assert.equal(researchIssue('读取失败（not_found）').message, '来源已不存在，保留URL待补查。')
})

test('not_found 的两头都不会误咬：dns_not_found 仍走 dns 分支', () => {
  // `not_found` 是 `dns_not_found` 的后半截，反过来 `dns_` 分支在本条之前已经先接走 —— 两头都盯住。
  const dns = researchIssue('该来源连续两次无法读取（dns_not_found），其他来源可正常访问；保留URL及原始失败原因待补查')
  assert.equal(dns.message, '来源网站暂时无法连接，尚未读取资料。可以尝试手动打开来源。')
  assert.ok(!dns.message.includes('来源已不存在'), dns.message)
  const notFound = researchIssue('该来源连续两次无法读取（not_found）')
  assert.equal(notFound.message, '来源已不存在，保留URL待补查。')
  assert.ok(!notFound.message.includes('dns'), notFound.message)
})

test('新加的区分分支抢不走上面任何一条既有分支', () => {
  // 它们插在形状分支之前、所有中文措辞分支之后，结构上只能从形状分支和 else 捞人。这里按
  // 「同一句话里既有老分支认得的词、又有新分支认得的代号」来验证优先级顺序没被写反。
  assert.equal(
    researchIssue('该来源连续两次无法读取（tls_BAD_ECPOINT）').message,
    '与来源网站的安全连接未能建立，尚未读取资料。可以尝试手动打开来源。',
  )
  assert.equal(researchIssue('no_official_source：not_found').message, '未找到可核实的厂家原始资料，产品参数还不能确认。')
  assert.equal(researchIssue('tender_body_unverified：empty_shell').message, '找到采购公告线索，但尚未核实公告正文与该型号的成交信息。')
  assert.equal(researchIssue('读取失败（empty_body）').message, '来源网页没有返回可读取的正文，内容尚未核实。')
})

test('空值仍然给出兜底说法，不返回空串', () => {
  assert.equal(researchIssue('').message, '相关资料尚未完成核验。')
  assert.equal(researchIssue(null).message, '相关资料尚未完成核验。')
  assert.equal(researchIssue(undefined).message, '相关资料尚未完成核验。')
})

test('分组去重与 URL 合并的既有行为不变', () => {
  const groups = groupResearchIssues([
    'no_official_source：https://a.gov.cn/1',
    'no_official_source：https://b.gov.cn/2',
    '本轮未取得可核实的同型号成交单价。',
    '',
  ])
  assert.equal(groups.length, 3)
  assert.deepEqual(groups[0].urls, ['https://a.gov.cn/1', 'https://b.gov.cn/2'])
  assert.equal(groups[1].message, '本轮未取得可核实的同型号成交单价。')
  assert.equal(groups[2].message, '相关资料尚未完成核验。')
})
