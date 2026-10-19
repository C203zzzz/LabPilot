/**
 * 内部术语与标识符的对外翻译层。
 *
 * 原本是 TaskWorkspace.jsx 里的局部函数，逐字搬到这里（实现未改一行），
 * 理由有两个：
 *   1. 报告总述的文字必须和报告正文走同一套翻译 —— 总述的 narrative 是模型
 *      生成的，而我们喂给模型的上下文里全是 unknown、configuration、
 *      evidence_grade 这类字段名，它很可能把这些英文词直接写进句子里；
 *      不翻译就会漏给用户看。共用同一份实现，才不会两边各翻译一套。
 *   2. 搬到 service 后 node --test 能直接测「哪些内部串不会漏出去」。
 *
 * 行为约束：既有报告正文（section.summary / item.label / item.text /
 * section.title）全部经过它，任何改动都会影响现有页面显示，所以这里只做搬移。
 *
 * 补充（2026-09）：三条老规则之后又追加了一层「产品自己生成的失败代号」词典。
 * 追加纪律 —— 老规则一条没删、没改语义、没换顺序，新规则一律排在老的后面，
 * 新规则内部长的排短的之前（原因见函数里那段注释）。
 */

/** 候选 ID 在页面上要显示成品牌 + 型号；查不到时不编造，只说待核对。 */
export function candidateName(candidates, id) {
  const candidate = candidates.find(item => item.id === id)
  return candidate ? [candidate.brand, candidate.model].filter(Boolean).join(' ') : '待核对设备'
}

export function publicText(value, state) {
  let text = String(value || '')
  // 空结果/通道失败按**通道**说（与后端 ``report_library`` 逐字相同）：研究链路每次
  // 只传一个通道，说成"整轮检索没有结果"就把内部事实说粗了（``docs/209`` §3）。
  // 长的排前面：带通道名的三条必须排在裸 ``empty_results`` 之前。
  const channelList = names => String(names || '').split(/[,\s]+/).filter(Boolean).join('、')
  text = text
    .replace(/empty_results_all_channels:\s*([A-Za-z0-9_.,-]+)/g, (_, names) =>
      `这一次查询实际跑过的 ${String(names).split(/[,\s]+/).filter(Boolean).length} 个检索通道（${channelList(names)}）全部没有返回结果`)
    .replace(/empty_results:\s*([A-Za-z0-9_.,-]+)/g, (_, names) =>
      `检索通道 ${channelList(names)} 这一次查询没有返回结果（这一句只说列出的通道，不代表整轮检索没有结果）`)
    .replace(/search_channel_failed:\s*([A-Za-z0-9_.,-]+)/g, (_, names) =>
      `检索通道 ${channelList(names)} 这一次查询失败，没有取得结果；这是通道故障，不是“没有结果”`)
    .replace(/no_official_source/g, '这一次检索未取得官方来源').replace(/empty_results/g, '这一次检索没有返回任何结果').replace(/tender_body_unverified:\s*/g, '采购公告正文待核实：').replace(/provider_failed:[^\s:]+:\s*/g, '检索服务未完成：')
  // 产品自己生成的失败代号也会随 run['gaps'] 走到页面上（后端把 failure_kind 原样拼进
  // 缺口文字，例如「该来源连续两次无法读取（empty_body）」），对采购人员没有意义。
  // 两条纪律：
  //   1. 长的必须排在短的前面。dns_not_found 里含 not_found，not_found 规则要是排在
  //      前面，就会先啃掉后半截、留下「dns_来源已不存在」。empty_body / empty_shell
  //      与既有的 empty_results 共享 empty_ 前缀，同理先长后短。
  //      上面那两个代号各有各的说法：``no_official_source`` 是"有结果但没有官方类型"，
  //      ``empty_results`` 是"一条结果都没有"，说成同一句会让报告自相矛盾。
  //   2. 只认下划线形态：用 \b 卡住词边界，正文里正常的英文「not found」（带空格）
  //      不会被改写，也不会把 task_not_found 这种带前缀的词啃掉半截。
  // 取值不封顶的两族用前缀一次吃掉，不逐个数状态码：
  text = text.replace(/\bunsupported_source_url\b/g, '这类来源地址暂不支持自动读取')
    .replace(/\bparse_incomplete\b/g, '正文解析不完整')
    .replace(/\bpage_too_large\b/g, '页面过大')
    .replace(/\bdns_not_found\b/g, '域名解析失败')
    .replace(/\bempty_body\b/g, '来源正文为空')
    .replace(/\bempty_shell\b/g, '页面只有空壳')
    .replace(/\bnot_found\b/g, '来源已不存在')
    .replace(/\bgaierror\b/g, '域名解析失败')
    .replace(/\bherror\b/g, '域名解析失败')
    .replace(/\bRemoteDisconnected\b/g, '连接被对方中断')
    .replace(/\bhttp_\d+/g, '来源返回错误状态')
    .replace(/\btls_[A-Za-z0-9_.]+/g, '与来源网站的安全连接未建立')
  // 最后一层兜底：后端有一处直接把 type(cause).__name__ 拼进缺口（procurement_research.py
  // 的 856 / 1527 行），类名是任意的，数不完。这里按 Python 异常的命名形状认
  // （*Error / *Timeout / *Disconnected …），**并且**要求它占满整个「代号位」——
  // 前面是行首、全角括号或冒号，后面是词边界。
  // 为什么非要加位置条件：只用形状会在真实数据上误伤标准英文报错，
  // 实测抓到两处 —— 检索告警里的 `<urlopen error [WinError 10054] …>` 和
  // `HTTP Error 403: Forbidden`。WinError 的形状和 RemoteDisconnected 一模一样，
  // 光看形状分不开它们，只能靠「它是不是单独占着代号位」来分。
  // 代价：夹在中文句子中间、前后没有分隔符的类名认不出来，那属于后端源头的事。
  text = text.replace(/(^|[（(：:])(\s*)[A-Za-z0-9]*(?:Error|Exception|Timeout|Disconnected|Refused|Reset|Aborted|IncompleteRead|Failure|Warning|Interrupted)\b/g, '$1$2来源读取未完成')
  // 聚合站代号与"检索/研究分两次跑"的自注句。这两类原先只在后端出口漏（页面上是人话、
  // 导出成机器腔），后端修好之后前端这层也补上，作为最后一道防线；措辞与后端
  // ``report_library.GAP_CODE_LABELS`` 逐字相同，两边由
  // 同一批用例钉住。
  text = text.replace(/(^|[^\w.])120shop(?=[^\w.]|$)/g, '$1医价网（120shop.cn）')
    .replace(/(^|[^\w.])bidcenter(?=[^\w.]|$)/g, '$1采招网（bidcenter.com.cn）')
    .replace(/(^|[^\w.])chinamae(?=[^\w.]|$)/g, '$1中国机电设备采购平台（chinamae.com）')
    .replace(/（这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单）/g, '')
  for (const item of state.candidates || []) text = text.replaceAll(item.id, candidateName(state.candidates, item.id))
  for (const item of state.documents || []) if (item.id) text = text.replaceAll(item.id, item.source_name || '采购资料')
  text = text.replace(/M\d{2}(?:\/M\d{2})*/g, '选型').replace(/fact_id(?:\/evidence_id)?|evidence_id/g, '来源记录').replace(/硬约束/g, '必须满足的条件').replace(/软约束/g, '优先考虑的条件')
  const labels = { eligible: '符合条件', ineligible: '不符合条件', no_recommendation: '暂不推荐', needs_review: '需要复核', unknown: '资料未提供', conflicted: '存在冲突', budget_max: '预算上限', throughput: '样品处理量', capability: '功能要求' }
  return text.replace(/\b(eligible|ineligible|no_recommendation|needs_review|unknown|conflicted|budget_max|throughput|capability)\b/g, value => labels[value])
}
