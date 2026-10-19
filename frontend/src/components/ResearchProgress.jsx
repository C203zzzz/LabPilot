/**
 * 「进展到哪」（Q11）的渲染层。
 *
 * 所有字段判读与「为什么没有百分比」的说明在 services/researchProgress.js 头部；
 * 这个文件只负责把 progressFacts 画出来。两者分开是为了让 progressFacts 能被
 * node --test 直接 import（测试跑不了 JSX）。
 */
import { progressFacts, PROGRESS_STATUS_LABELS } from '../services/researchProgress'

export default function ResearchProgress({ run, clock }) {
  const facts = progressFacts(run)
  const elapsed = run?.started_at ? Math.max(0, Math.floor((clock - new Date(run.started_at).getTime()) / 1000)) : 0
  return <section className="research-progress" aria-label="本轮检索进展">
    {/* aria-live 只挂在状态行上：秒数每秒都在变，挂在整块上会让读屏软件每秒念一次。 */}
    <div className="research-progress-heading" role="status" aria-live="polite">
      <strong>{PROGRESS_STATUS_LABELS[run?.status] || run?.status}{facts.currentStage ? ` · ${facts.currentStage}` : ''}</strong>
      <span aria-hidden="true">已用时 {elapsed} 秒</span>
    </div>
    {/* 当前阶段只在上面那行状态里说一次。阶段清单只列**已经走完的阶段**（后端 stage_timings
        的真实追加记录），不再补一行同名的「进行中」：那一行和状态行逐字同一个阶段名，又和
        下面检索线的「正在检索」说同一件事，同一屏就出现了两遍（用户截图指出）。
        正在查哪一条线由下面「两条检索线」回答，所以没有已走完阶段时这一块整个不出现。 */}
    {(facts.shown.length > 0 || facts.earlierCount > 0) && <ol className="research-progress-stages">
      {facts.earlierCount > 0 && <li className="research-progress-stage"><span className="stage-name">更早的 {facts.earlierCount} 个阶段已完成</span></li>}
      {facts.shown.map((entry, index) => <li className="research-progress-stage" key={`${entry.name}-${index}`}>
        <span className="stage-name">{entry.name}</span>
        <span className="stage-state">{entry.status === 'completed' ? '已完成' : '已中止'}{entry.duration_ms ? ` · ${Math.max(1, Math.round(entry.duration_ms / 1000))} 秒` : ''}</span>
      </li>)}
    </ol>}
    <p className="research-progress-lede">两条检索线</p>
    <dl className="research-progress-lanes">{facts.lanes.map(lane => <div key={lane.key}><dt>{lane.name}</dt><dd>{lane.text}</dd></div>)}</dl>
    <progress value={facts.done} max={facts.total} aria-label={`${facts.total} 条检索线已完成 ${facts.done} 条`} />
    <p className="research-progress-counts">检索线已完成 {facts.done} / {facts.total} 条；已执行 {facts.queries} 条检索式。当前用量：{facts.counts.map(c => `${c.name} ${c.used}${c.limit ? ` / ${c.limit}` : ''}`).join('，')}。</p>
  </section>
}
