/**
 * publicText 的失败代号词典。
 *
 * 背景：后端会把产品自己生成的失败代号原样拼进缺口文字，例如
 * 「该来源连续两次无法读取（empty_body），其他来源可正常访问；……保留URL及原始失败原因待补查」。
 * 这些代号以前会原样印在页面上给采购人员看。
 *
 * 数据来源：对 output/ 下全部 2112 个 json 做过一次全量扫描，其中 368 个是带 gaps 的
 * run json、279 个 gaps 非空、共 2968 条 gap 字符串。run['gaps'] 里实际出现过的代号：
 *   no_official_source 964、empty_results 791、tender_body_unverified 525、
 *   empty_body 18、RemoteDisconnected 9、dns_not_found 5、tls_* 合计 4、empty_shell 1。
 *   独立的 not_found 在 run['gaps'] 里 0 次（扫描时若把 not_found 列在 dns_not_found
 *   前面，那 5 次 dns_not_found 会被误记成 not_found —— 这个坑有实证）。
 *
 * 本文件盯住三件事：漏掉的代号有了中文、原有规则逐字不变、正常的英文正文不被误伤。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { candidateName, publicText } from '../src/services/publicText.js'

const state = {
  candidates: [{ id: 'cand-1', brand: '中科中佳', model: 'H1850R' }],
  documents: [{ id: 'doc-1', source_name: '采购需求.pdf' }],
}

const translate = value => publicText(value, state)

/** 断言：输出里不再有英文代号，并且带上了对应的中文。 */
function assertTranslated(input, code, chinese) {
  const output = translate(input)
  assert.ok(!output.includes(code), `「${input}」里还漏着代号 ${code}：${output}`)
  assert.ok(output.includes(chinese), `「${input}」没有翻成「${chinese}」：${output}`)
}

// --- 原来漏掉的那几个代号 ----------------------------------------------------

test('产物里真实出现过的失败代号都翻成中文，不再原样漏出去', () => {
  assertTranslated('empty_body', 'empty_body', '来源正文为空')
  assertTranslated('RemoteDisconnected', 'RemoteDisconnected', '连接被对方中断')
  assertTranslated('dns_not_found', 'dns_not_found', '域名解析失败')
  assertTranslated('not_found', 'not_found', '来源已不存在')
  assertTranslated('empty_shell', 'empty_shell', '页面只有空壳')
})

test('后端还会产生的其余代号也有对应中文，取码不封顶的两族按前缀一次吃掉', () => {
  assertTranslated('unsupported_source_url', 'unsupported_source_url', '这类来源地址暂不支持自动读取')
  assertTranslated('page_too_large', 'page_too_large', '页面过大')
  assertTranslated('parse_incomplete', 'parse_incomplete', '正文解析不完整')
  assertTranslated('http_403', 'http_403', '来源返回错误状态')
  assertTranslated('http_404', 'http_404', '来源返回错误状态')
  assertTranslated('http_999', 'http_999', '来源返回错误状态')
  assertTranslated('tls_BAD_ECPOINT', 'tls_BAD_ECPOINT', '与来源网站的安全连接未建立')
  assertTranslated('tls_CERTIFICATE_VERIFY_FAILED', 'tls_CERTIFICATE_VERIFY_FAILED', '与来源网站的安全连接未建立')
  assertTranslated('gaierror', 'gaierror', '域名解析失败')
})

test('任意异常类名靠形状兜底，不去穷举类名', () => {
  // 后端有一处直接把 type(cause).__name__ 拼进缺口，是什么类名取决于运行时报错。
  // 这里挑几个产物里出现过、以及同形状但本轮没出现的。
  assertTranslated('聚合站线索指向的官方原文未读到：TimeoutError', 'TimeoutError', '来源读取未完成')
  assertTranslated('附件地址渲染未成功，保留文件名线索：ConnectionResetError', 'ConnectionResetError', '来源读取未完成')
  assertTranslated('ValueError', 'ValueError', '来源读取未完成')
  assertTranslated('UnicodeEncodeError', 'UnicodeEncodeError', '来源读取未完成')
  assertTranslated('HTTPError', 'HTTPError', '来源读取未完成')
  assertTranslated('DeepSeekError', 'DeepSeekError', '来源读取未完成')
  // 形状兜底只在「它自己占着代号位」时生效：前面是行首、全角括号或冒号。
  assertTranslated('（IncompleteRead）', 'IncompleteRead', '来源读取未完成')
})

// --- 长的必须排在短的前面 ----------------------------------------------------

test('dns_not_found 不会被 not_found 那条规则抢先啃掉后半截', () => {
  assert.equal(translate('dns_not_found'), '域名解析失败')
  assert.equal(translate('not_found'), '来源已不存在')
  // 被抢先的典型症状就是留下一个 dns_ 前缀。
  assert.ok(!translate('dns_not_found').includes('dns_'))
  const sentence = translate('该来源连续两次无法读取（dns_not_found），其他来源可正常访问；保留URL及原始失败原因待补查')
  assert.ok(!sentence.includes('dns_'), sentence)
  assert.ok(sentence.includes('域名解析失败'), sentence)
  // 同样的前缀关系：empty_body / empty_shell 不能被 empty_results 那条吃掉。
  assert.equal(translate('empty_body'), '来源正文为空')
  assert.equal(translate('empty_shell'), '页面只有空壳')
  // empty_results（一条结果都没有）与 no_official_source（有结果但没有官方类型）
  // 是两件事，说法必须不同：说成同一句会让报告一边列着官方来源、一边说没找到。
  assert.equal(translate('empty_results'), '这一次检索没有返回任何结果')
  assert.equal(translate('no_official_source'), '这一次检索未取得官方来源')
})

test('词典源码里长的代号写在短的前面（顺序纪律本身也锁住）', () => {
  const source = readFileSync(new URL('../src/services/publicText.js', import.meta.url), 'utf8')
  const at = rule => {
    const index = source.indexOf(rule)
    assert.ok(index > 0, `找不到规则 ${rule}`)
    return index
  }
  // \b 已经能保证 dns_not_found 不被误啃，但顺序纪律仍要留着：
  // 哪天有人去掉词边界，先长后短是最后一道防线。
  assert.ok(at(String.raw`\bdns_not_found\b`) < at(String.raw`\bnot_found\b`), 'dns_not_found 必须排在 not_found 前面')
  assert.ok(at(String.raw`\bempty_body\b`) < at(String.raw`\bnot_found\b`))
  // 三条老规则还在、还在新规则前面，顺序没被调换。
  assert.ok(at('no_official_source') < at(String.raw`\bdns_not_found\b`))
  assert.ok(at('empty_results') < at(String.raw`\bdns_not_found\b`))
  assert.ok(at('tender_body_unverified:') < at(String.raw`\bdns_not_found\b`))
  assert.ok(at('provider_failed:[^\\s:]+:') < at(String.raw`\bdns_not_found\b`))
})

// --- 原有三条规则逐字不变（期望值取自改动前的实现：同一批输入跑改动前后，0 处差异）----

test('原有三条规则的翻译结果与改动前逐字一致', () => {
  const unchanged = [
    // ``no_official_source`` / ``empty_results`` 的中文说法在 2026-09 改过一次：
    // 原来两者同为「未找到已核实的官方来源」，与报告里的来源清单会直接冲突
    // （见 docs/待归档-价格没被采信的根因.md §3）。其余两条逐字未动。
    ['no_official_source', '这一次检索未取得官方来源'],
    ['empty_results', '这一次检索没有返回任何结果'],
    ['no_official_source|empty_results', '这一次检索未取得官方来源|这一次检索没有返回任何结果'],
    // 2026-09 第二轮：代号后面的空格一并吃掉（`tender_body_unverified:\s*`、
    // `provider_failed:[^\s:]+:\s*`），与后端 `report_library.GAP_CODE_LABELS` 逐字一致
    // ——导出报告那边一直是"吃掉空格"的写法，前端原来留一个空格，同一句话在页面和
    // 导出里差一个空格。改由同一批用例钉住两端。
    ['tender_body_unverified: 正文未取到', '采购公告正文待核实：正文未取到'],
    ['tender_body_unverified:', '采购公告正文待核实：'],
    ['provider_failed:tavily: 检索中断', '检索服务未完成：检索中断'],
    ['provider_failed:bing: 调用超时', '检索服务未完成：调用超时'],
  ]
  for (const [input, expected] of unchanged) {
    assert.equal(translate(input), expected, `「${input}」的翻译结果变了`)
  }
})

// --- 真实整句 ----------------------------------------------------------------

test('产物里的整句缺口能整句翻译', () => {
  // 原文照抄 output/acceptance-57/cold-run.json 的 run.gaps。
  const real = '该来源连续两次无法读取（empty_body），其他来源可正常访问；保留URL及原始失败原因待补查（不等于未检索到产品）：http://ztb.usx.edu.cn/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=1297393898&wbfileid=369421'
  const output = translate(real)
  assert.ok(!output.includes('empty_body'), output)
  assert.ok(output.includes('该来源连续两次无法读取（来源正文为空）'), output)
  // URL 与其余说明文字原样保留。
  assert.ok(output.includes('http://ztb.usx.edu.cn/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=1297393898&wbfileid=369421'))
  assert.ok(output.includes('不等于未检索到产品'))
})

test('其余四条真实代号也整句可翻', () => {
  const real = [
    ['该来源连续两次无法读取（RemoteDisconnected），其他来源可正常访问；保留URL及原始失败原因待补查（不等于未检索到产品）：https://bbs.eeworld.com.cn/thread-1000000-1-1.html', 'RemoteDisconnected', '连接被对方中断'],
    ['该来源连续两次无法读取（dns_not_found），其他来源可正常访问；保留URL及原始失败原因待补查（不等于未检索到产品）：https://cgzx.zjhu.edu.cn/2025/0305/c408a100000/page.htm', 'dns_not_found', '域名解析失败'],
    ['该来源连续两次无法读取（empty_shell），其他来源可正常访问；保留URL及原始失败原因待补查（不等于未检索到产品）：https://wap.scjgj.sh.gov.cn/businessCheck/verifKey.do?serial=1000000000000000000000000000000000', 'empty_shell', '页面只有空壳'],
    ['该来源连续两次无法读取（tls_BAD_ECPOINT），其他来源可正常访问；保留URL及原始失败原因待补查（不等于未检索到产品）：https://ggzyfw.beijing.gov.cn/jyxxzbjggg/20231206/1000000.html', 'tls_BAD_ECPOINT', '与来源网站的安全连接未建立'],
  ]
  for (const [input, code, chinese] of real) assertTranslated(input, code, chinese)
})

// --- 防误伤 ------------------------------------------------------------------

test('正常的英文正文不会被改写', () => {
  // 带空格的 not found 不是代号形态，一个字符都不该动。
  assert.equal(translate('The requested document was not found on this server.'), 'The requested document was not found on this server.')
  assert.equal(translate('no results were found'), 'no results were found')
  // 标准英文报错里的 WinError / Error 不是代号，产物里实测被形状兜底误伤过，这条盯着它。
  assert.equal(
    translate('检索服务未完成： <urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接。>'),
    '检索服务未完成： <urlopen error [WinError 10054] 远程主机强迫关闭了一个现有的连接。>',
  )
  assert.equal(translate('检索服务未完成： HTTP Error 403: Forbidden'), '检索服务未完成： HTTP Error 403: Forbidden')
  // 正常的英文括注、格式名、型号、单位不动。
  assert.equal(translate('（PDF）与（OCR）'), '（PDF）与（OCR）')
  assert.equal(translate('需补充可读 PDF、Office 或 OCR 原件。'), '需补充可读 PDF、Office 或 OCR 原件。')
  assert.equal(translate('型号：Eppendorf 5810R、Thermo Multifuge X1R'), '型号：Eppendorf 5810R、Thermo Multifuge X1R')
  assert.equal(translate('这是一台离心机，最高转速 18500 rpm，容量 6×50 mL。'), '这是一台离心机，最高转速 18500 rpm，容量 6×50 mL。')
})

// --- 同一文件里的既有行为回归 --------------------------------------------------

test('候选 ID、文档 ID、M01、硬约束、eligible 那几条规则仍然工作', () => {
  assert.equal(translate('cand-1 与 doc-1'), '中科中佳 H1850R 与 采购需求.pdf')
  assert.equal(translate('M01/M02 的硬约束'), '选型 的必须满足的条件')
  assert.equal(translate('M07'), '选型')
  assert.equal(translate('fact_id/evidence_id 与 evidence_id'), '来源记录 与 来源记录')
  assert.equal(translate('硬约束 与 软约束'), '必须满足的条件 与 优先考虑的条件')
  assert.equal(translate('eligible'), '符合条件')
  assert.equal(translate('ineligible'), '不符合条件')
  assert.equal(translate('no_recommendation'), '暂不推荐')
  assert.equal(translate('needs_review'), '需要复核')
  assert.equal(translate('unknown'), '资料未提供')
  assert.equal(translate('conflicted'), '存在冲突')
  assert.equal(translate('budget_max'), '预算上限')
  assert.equal(translate('throughput'), '样品处理量')
  assert.equal(translate('capability'), '功能要求')
  assert.equal(candidateName(state.candidates, 'cand-1'), '中科中佳 H1850R')
  assert.equal(candidateName(state.candidates, 'gone'), '待核对设备')
})

test('空值与非字符串输入仍然安全', () => {
  assert.equal(translate(''), '')
  assert.equal(translate(null), '')
  assert.equal(translate(undefined), '')
  assert.equal(translate(0), '')
  assert.equal(translate('   '), '   ')
})
