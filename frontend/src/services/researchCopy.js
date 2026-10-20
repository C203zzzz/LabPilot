const urlPattern = /https?:\/\/[^\s<>"'，；。（）]+/g

// 额度用尽的**同一句话**：与后端 ``backend/infrastructure/deepseek.py`` 的
// ``BUDGET_EXCEEDED_MESSAGE`` 逐字相同。三个出口（研究缺口、问答接口、需求访谈接口）
// 说的都是这件事，三处各写一句的后果实测过——同一件事三种说法，其中一种读起来像
// 产品坏了。``tests/test_budget_messaging.py`` 逐字比对两边，改一边不改另一边会红。
// 三件事缺一不可：①这是用量上限、不是故障；②什么时候能再试；③已取得的结果不会丢。
export const BUDGET_EXCEEDED_MESSAGE = '本小时的模型调用额度已用完——这是用量上限，不是故障。额度按调用时间滚动恢复（最早的那次调用满一小时后自动让出名额），届时直接重试即可；已经取得的结果都保留着，不会丢。'

export function researchIssue(value) {
  const raw = String(value || '')
  const urls = [...new Set(raw.match(urlPattern) || [])]
  let message
  if (/no_official_source/.test(raw)) message = '未找到可核实的厂家原始资料，产品参数还不能确认。'
  else if (/tender_body_unverified/.test(raw)) message = '找到采购公告线索，但尚未核实公告正文与该型号的成交信息。'
  // 通道失败与"没有结果"分开说：一次通道失败不许说成"没查到"。
  else if (/search_channel_failed/.test(raw)) message = '有检索通道这一次没有跑通（这不是“没有搜到”），相关结论仍缺少依据。已有结果已保留。'
  else if (/empty_results_all_channels/.test(raw)) message = '这一次查询所有跑过的检索通道都没有返回结果，尚不能据此作出价格或替代判断。'
  else if (/empty_results/.test(raw)) message = '部分检索没有找到相关资料，尚不能据此作出价格或替代判断。'
  else if (/provider_budget_exceeded|调用.*上限|小时.*额度|共享额度|budget_exceeded/.test(raw)) message = BUDGET_EXCEEDED_MESSAGE
  else if (/限制自动访问|access_denied|http_40[13]|http_429/.test(raw)) message = '网站限制自动读取，尚未取得可核实内容。可以尝试手动打开来源。'
  else if (/tls_|SSL|CERTIFICATE|BAD_ECPOINT|UNEXPECTED_EOF/.test(raw)) message = '与来源网站的安全连接未能建立，尚未读取资料。可以尝试手动打开来源。'
  else if (/dns_|gaierror|NameResolution/.test(raw)) message = '来源网站暂时无法连接，尚未读取资料。可以尝试手动打开来源。'
  else if (/empty_body|body_unverified|正文.*未取得/.test(raw)) message = '来源网页没有返回可读取的正文，内容尚未核实。'
  else if (/provider_|Timeout|timeout|timed.out|http_5\d\d|HTTPError|URLError/.test(raw)) message = '部分检索或读取未完成，相关结论仍缺少依据。已有结果已保留。'
  else if (/没有完成全部关键参数|(?:核查其他品牌|替代产品核查)未完成/.test(raw)) message = '替代品的关键参数尚未核全，目前不能判断能否替代。'
  // 阶段名与缺口文案现在统一成状态行那套词序（``核查同款价格`` / ``核查其他品牌``，
  // 见 ``backend/services/procurement_research.py``）。两种词序这里都认：库里已经存下来的
  // 老缺口文案（``同款价格核查未完成`` / ``替代产品核查未完成``）也要照旧换成人话。
  else if (/未取得可核实.*公开价格|(?:核查同款价格|同款价格核查)未完成/.test(raw)) message = '尚未取得可核实的同型号价格，当前无法判断销售报价是否合理。'
  // 下面五条插在形状分支**之前**、所有中文措辞分支**之后** —— 它们同样只能从形状分支和
  // 最后的 else 里捞人，不可能改变上面任何一条既有分支的结果。
  // 加它们的理由：这五个代号原来全被下一行的 snake_case 形状分支收走、塌成同一句
  // 「部分资料尚未完成核验，暂不能用于采购结论。」，用户读那一列分不出「页面过大」和
  // 「来源已不存在」。长在前短在后（沿用 publicText 的纪律）。用 \b 锚定，避免啃掉
  // dns_not_found 这类带前缀的词（dns_ 那条分支在本行之前，已经先接走了）。
  else if (/\bunsupported_source_url\b/.test(raw)) message = '这类来源地址不支持自动读取，保留线索待补查。'
  else if (/\bparse_incomplete\b/.test(raw)) message = '正文解析不完整（可能是扫描件或图片版），需补充可读的 PDF、Office 或 OCR 原件。'
  else if (/\bpage_too_large\b/.test(raw)) message = '页面过大，未能完整读取，请提供具体明细页。'
  else if (/\bempty_shell\b/.test(raw)) message = '页面只返回了空壳，没有可读取的正文，内容尚未核实。可以尝试手动打开来源。'
  else if (/\bnot_found\b/.test(raw)) message = '来源已不存在，保留URL待补查。'
  else if (/[A-Za-z]+_[A-Za-z_]+/.test(raw)) message = '部分资料尚未完成核验，暂不能用于采购结论。'
  // 传输层异常类名。后端会把 failure_kind / type(cause).__name__ 原样拼进缺口，
  // 例如「该来源连续两次无法读取（RemoteDisconnected），其他来源可正常访问……」，
  // 上面那些分支一条都不认，于是掉到 else 把英文整段印给用户看（全量实测 9 条）。
  // 这一条**必须留在最后一条 else if**：放在前面会抢走本来命中上面几条中文措辞规则的
  // 缺口，改变现有输出；放在这里它只能从 else 兜底分支里捞人，除了这些代号本身，
  // 不会有任何行为变化。
  else if (/RemoteDisconnected|ConnectionReset|ConnectionAborted|ConnectionRefused/.test(raw)) message = '与来源网站的连接被中途断开，尚未读取资料。可以尝试手动打开来源。'
  // else 分支保持原样（认不出来就保留原文，只去掉 URL）。它不是死代码：全量实测
  // 369 条走兜底的缺口里，除了上面那 9 条代号，剩下的是 48 条型号名开头的中文说明
  // （PF-22／MH2100R／HT190R：发现来源的参数低于要求……）和一批本来就写得很清楚的
  // 中文句子（「本轮未取得可核实的同型号成交单价。」等）。把它换成一个通用中文说法
  // 会把这些人话全丢掉，所以不动它。
  else message = raw.replace(urlPattern,'').replace(/\s*[:：；]\s*$/, '').trim()
  return {message: message || '相关资料尚未完成核验。', urls}
}

export function groupResearchIssues(values) {
  const groups = new Map()
  for (const value of values || []) {
    const issue = researchIssue(value)
    const existing = groups.get(issue.message)
    if (existing) existing.urls = [...new Set([...existing.urls,...issue.urls])]
    else groups.set(issue.message,issue)
  }
  return [...groups.values()]
}
