"""One-confirmation workflow endpoints; legacy endpoints remain available."""
import time
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, FileResponse
from pathlib import Path

from backend.infrastructure.deepseek import DeepSeekClient
from backend.infrastructure.lease_heartbeat import LeaseHeartbeat
from backend.services.procurement_research import ProcurementResearch, ResearchStopped
from backend.services.report_library import export_reports, library_items

#: 一轮研究"状态已是终态"之后，收尾还要写库多久的等待上限（秒）。
#: ``status`` 写成 ``partial`` 比"这一轮真的写完了"来得早：收尾还要落阶段账与报告总述。
#: 按需查价与报告导出都写同一个 run，必须等这段走完（见 ``run_exclusive``）。
ROUND_SETTLE_SECONDS = 30.0


def procurement_router(repo, search):
    router = APIRouter(prefix='/api/v1/tasks')
    service = ProcurementResearch(repo, search, DeepSeekClient.from_env)

    def task(task_id):
        if repo.get('tasks', task_id) is None:
            raise HTTPException(404, detail='任务不存在')

    def invoke(action, *args):
        task(args[0])
        try:
            return action(*args)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @contextmanager
    def run_exclusive(run_id, busy_detail):
        """拿到这一轮的执行租约，才允许写这个 run（按需查价、报告导出共用这一处）。

        为什么必须有这一步（2026-10-01 ``sample-a-fix1`` 端到端实测）：``status`` 写成
        ``partial`` 时这一轮并**没有**真的写完，收尾还要落阶段账与报告总述，而那些写带的
        是"收尾开始时读到的那份 run 副本"。按需查价要跑几分钟、期间反复写同一个 run，
        只要收尾的写排在它后面，用户刚花过钱查到的结论就被整份盖回去——接口 200 带完整
        结果、库里却还是"没查过"，第二次点击又花一次钱。租约是既有机制：``execute()``
        全程持有它、最后一句才释放，拿到它就等于"这一轮连同收尾都写完了"，
        不可能再有旧快照排在后面。等不到就响亮报错，绝不假装成功往下走。
        """
        claim = 'research:' + run_id
        deadline = time.monotonic() + ROUND_SETTLE_SECONDS
        owner = repo.claim_analysis(claim)
        while owner is None and time.monotonic() < deadline:
            time.sleep(0.1)
            owner = repo.claim_analysis(claim)
        if owner is None:
            raise HTTPException(409, detail=busy_detail)
        heartbeat = LeaseHeartbeat(repo, claim, owner)
        try:
            # 这条线程自己的写也挂上这份租约，``persist`` 的租约守卫因此对它生效。
            service.execution.claim = (claim, owner)
            yield
        finally:
            service.execution.claim = None
            heartbeat.close()
            repo.release_analysis(claim, owner)

    @router.get('/{task_id}/documents/{document_id}/original')
    def original(task_id: str, document_id: str):
        task(task_id)
        document = next((d for d in repo.list('documents', task_id) if d['id'] == document_id), None)
        if not document or not document.get('original_path') or not Path(document['original_path']).is_file():
            raise HTTPException(404, detail='原始资料不存在')
        return FileResponse(document['original_path'], media_type=document.get('media_type') or 'application/octet-stream', content_disposition_type='inline', filename=document['source_name'])

    @router.get('/{task_id}/documents/{document_id}/pages/{page}.png')
    def evidence_region(task_id: str, document_id: str, page: int, region: str = ''):
        task(task_id)
        document = next((d for d in repo.list('documents', task_id) if d['id'] == document_id), None)
        if not document or document.get('document_type') != 'pdf':
            raise HTTPException(404, detail='PDF资料不存在')
        import fitz
        with fitz.open(document['original_path']) as pdf:
            if not 1 <= page <= len(pdf):
                raise HTTPException(404, detail='页码不存在')
            sheet = pdf[page - 1]
            stored = next((p for p in document.get('pages', []) if p['number'] == page), {})
            area = next((r for r in stored.get('regions', []) if r.get('id') == region), None)
            if area and area.get('bbox'):
                b = area['bbox']
                rect = fitz.Rect(b['l'], sheet.rect.height-b['t'], b['r'], sheet.rect.height-b['b']) if b.get('coord_origin') == 'BOTTOMLEFT' else fitz.Rect(b['l'],b['t'],b['r'],b['b'])
                sheet.draw_rect(rect, color=(.95,.3,.05), width=2)
            return Response(sheet.get_pixmap(matrix=fitz.Matrix(1.5,1.5)).tobytes('png'), media_type='image/png')

    @router.get('/{task_id}/baseline')
    def baseline(task_id: str):
        return invoke(service.baseline, task_id)

    @router.get('/{task_id}/intake')
    def intake(task_id: str):
        from backend.services.procurement_intake import read_intake
        task(task_id)
        return read_intake(repo, task_id)

    @router.post('/{task_id}/intake')
    def intake_reply(task_id: str, body: dict):
        from backend.services.procurement_intake import respond
        from backend.infrastructure.deepseek import DeepSeekError
        task(task_id)
        with service.lock:
            try:
                return respond(repo, task_id, body, service.client_factory)
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc)) from exc
            except DeepSeekError as exc:
                # 额度用尽这件事三个出口说同一句话（研究车道缺口、问答接口、这里）。
                # 原来这一句**完全没提额度**，用户只看到"服务暂不可用"，不知道自己
                # 是被用量上限挡住的、也不知道等多久能再试。
                from backend.infrastructure.deepseek import BUDGET_EXCEEDED_MESSAGE
                if exc.code == 'provider_budget_exceeded':
                    raise HTTPException(429, detail='本次回答尚未保存。' + BUDGET_EXCEEDED_MESSAGE) from exc
                raise HTTPException(503, detail='本次回答尚未保存，需求整理服务暂不可用。请保留输入，稍后重试。') from exc

    @router.post('/{task_id}/baseline/save')
    def save_baseline(task_id: str, body: dict):
        task(task_id)
        try:
            return service.confirm(task_id, body, start=False)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.post('/{task_id}/research-runs/{run_id}/start/{lane}')
    def start_lane(task_id: str, run_id: str, lane: str):
        return invoke(service.start_lane, task_id, run_id, lane)

    @router.post('/{task_id}/baseline/confirm')
    def confirm(task_id: str, body: dict):
        return invoke(service.confirm, task_id, body)

    @router.get('/{task_id}/research-runs/{run_id}')
    def status(task_id: str, run_id: str):
        # 读接口给的是「补上厂家产品参数之后」的 run：价格记录上的配置本来只由价格
        # 通道算一次，而产品参数页常常是那之后才抓回来的（见 service.display）。
        return invoke(service.display, task_id, run_id)

    @router.post('/{task_id}/research-runs/{run_id}/cancel')
    def cancel(task_id: str, run_id: str):
        return invoke(service.cancel, task_id, run_id)

    @router.post('/{task_id}/research-runs/{run_id}/supplier-reply')
    def supplier_reply(task_id: str, run_id: str, body: dict):
        return invoke(service.supplier_update, task_id, run_id, body)

    @router.post('/{task_id}/research-runs/{run_id}/continue')
    def resume(task_id: str, run_id: str):
        return invoke(service.resume, task_id, run_id)

    @router.get('/{task_id}/research-runs/{run_id}/supplement')
    def supplement_status(task_id: str, run_id: str):
        from backend.services.supplemental_research import eligibility
        run = invoke(service.get, task_id, run_id)
        return eligibility(repo, run)

    @router.post('/{task_id}/research-runs/{run_id}/supplement/prompt')
    def supplement_prompt(task_id: str, run_id: str):
        from backend.services.supplemental_research import prompt_packet
        run = invoke(service.get, task_id, run_id)
        try:
            return prompt_packet(repo, run)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.post('/{task_id}/research-runs/{run_id}/supplement/import')
    def supplement_import(task_id: str, run_id: str, body: dict):
        from backend.services.supplemental_research import import_leads, budget_root
        task(task_id)
        with service.lock:
            root = budget_root(service, invoke(service.get, task_id, run_id))
            claim = 'research:' + root['id']
            owner = repo.claim_analysis(claim)
            if not owner:
                raise HTTPException(409, detail='本轮正在研究或核验补证，请稍后重试')
            from backend.infrastructure.lease_heartbeat import LeaseHeartbeat
            heartbeat = LeaseHeartbeat(repo, claim, owner)
            try:
                service.execution.claim = (claim, owner)
                return import_leads(service, task_id, run_id, body)
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc)) from exc
            finally:
                service.execution.claim = None
                heartbeat.close()
                repo.release_analysis(claim, owner)

    @router.post('/{task_id}/research-runs/{run_id}/alternatives/{candidate_id}/price')
    def lookup_alternative_price(task_id: str, run_id: str, candidate_id: str, body: dict | None = None):
        """按需查价：只查路径里那一个候选（设计见 第二节）。

        * 路径里带 ``candidate_id`` 而不是下标——一次请求只可能命中一个候选，
          "用户只查其中一个"由接口形状保证，不靠前端自觉；
        * 请求体可以整个不传，或传 ``{}``；只有 ``force`` 一个可选键，
          默认 ``false``：已经查过的候选再点一次直接命中已有结果，不重复花钱；
        * ``candidate_id`` 不在本轮候选里 → 404，而且**一次付费调用都没发生**
          （``lookup_candidate_price`` 在找到候选之前不碰任何额度）；
        * 这一轮还在检索/收尾时**不许**点：它的写带着自己那份 run 副本，会盖掉点击结果。
          两种情形分开说（"还在进行"与"正在收尾"是两件事），且都在花钱之前拒绝
          （见 ``run_exclusive``；2026-10-01 ``sample-a-fix1`` 实测）。
        """
        from backend.services.procurement_research import UnknownCandidate
        payload = body if isinstance(body, dict) else {}
        task(task_id)
        try:
            current = invoke(service.get, task_id, run_id)
            if current['status'] in {'queued', 'running'}:
                raise HTTPException(409, detail='本轮研究仍在进行，请等它结束后再查价')
            with run_exclusive(run_id, '本轮研究正在收尾，请稍后重试'):
                with service.lock:
                    run = service.get(task_id, run_id)
                    return service.lookup_candidate_price(run, candidate_id,
                                                          force=bool(payload.get('force')))
        except UnknownCandidate as exc:
            raise HTTPException(404, detail='本轮候选里没有这个候选，请刷新后重试') from exc
        except ResearchStopped as exc:
            # 控流（额度用尽、租约失效）不是"查到了"，也不许当 200 出去：如实说这次没查成，
            # 并说清库里可能只留了一部分——用户据此决定是刷新重试还是补额度。
            raise HTTPException(409, detail='这次查价没有完成：%s已落库的部分保留，请刷新后重试。'
                                % exc) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @router.get('/{task_id}/research-runs/{run_id}/report')
    @router.post('/{task_id}/research-runs/{run_id}/report')
    def report(task_id: str, run_id: str):
        # 导出报告这一路也会整份写回 run（``report()`` 里那一次 save 与报告总述阶段的
        # persist）。它与按需查价写的是同一条记录，必须像点击一样独占这一轮，
        # 否则"一边导出、一边点查价"时，先读后写的那一份会把后写的结论盖掉。
        task(task_id)
        current = invoke(service.get, task_id, run_id)
        if current['status'] in {'queued', 'running'}:
            raise HTTPException(409, detail='本轮仍在检索，请完成或停止后再生成报告')
        with run_exclusive(run_id, '本轮研究正在收尾，请稍后重试'):
            result = invoke(service.report, task_id, run_id)
        records = {result['report_id']: repo.get('reports', result['report_id'])}
        items = [x for x in library_items(repo) if x['id'] == result['report_id']]
        return Response(export_reports(items, records, Path(repo.path).parent / 'research-snapshots'), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="procurement-report.zip"'})

    return router, service
