"""Durable, task-scoped analysis nodes; successful calls survive interruption."""
import hashlib
import json
import copy
from contextvars import ContextVar
from datetime import datetime, timezone

from backend.infrastructure.deepseek import DeepSeekError

current_checkpoint = ContextVar('analysis_checkpoint', default=None)


class AnalysisCheckpoint:
    def __init__(self, repo, task_id, owner=None):
        self.repo, self.task_id = repo, task_id
        self.owner = owner
        self.control_id = 'analysis-control-' + task_id
        self.shared_keys = {}

    def control(self):
        return self.repo.get('procurement_records', self.control_id) or {}

    def set_control(self, **values):
        record = {**self.control(), **values, 'id': self.control_id, 'kind': 'analysis_control',
                  'task_id': self.task_id, 'created_at': datetime.now(timezone.utc).isoformat()}
        self.repo.put('procurement_records', self.control_id, record, task_id=self.task_id)

    def check(self):
        if self.owner and self.repo.active_analysis(self.task_id) != self.owner:
            raise DeepSeekError('analysis_lease_lost', '分析执行权已过期，请继续恢复已保存节点')
        if self.control().get('cancelled'):
            raise DeepSeekError('analysis_cancelled', '分析已停止，完成的文件与区域已保存，可继续未完成部分')

    def key(self, client, kwargs):
        # Version invalidates incompatible extraction logic; no credentials enter storage.
        data = ['ownership-v3-detail-v2', getattr(client, 'endpoint', ''), getattr(client, 'model', ''), kwargs]
        key = 'analysis-node-' + hashlib.sha256((self.task_id + json.dumps(data, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
        documents = self.repo.list('documents', self.task_id)
        mapping = {d['id']: 'document-sha256:' + d['sha256'] for d in documents if d.get('sha256')}
        if mapping and len(set(mapping.values())) == len(mapping):
            normalized = self.remap(data, mapping, prompt=True)
            shared = 'analysis-cache-' + hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self.shared_keys[key] = (shared, mapping)
        return key

    @staticmethod
    def remap(value, mapping, prompt=False):
        if isinstance(value, dict):
            return {k: AnalysisCheckpoint.remap(v, mapping, prompt) for k, v in value.items()}
        if isinstance(value, list):
            return [AnalysisCheckpoint.remap(v, mapping, prompt) for v in value]
        if isinstance(value, str):
            if value in mapping:
                return mapping[value]
            if prompt:
                for old, new in mapping.items():
                    value = value.replace(json.dumps(old), json.dumps(new))
        return value

    def get(self, key):
        local = self.repo.get('procurement_records', key)
        if local and local.get('status') == 'completed':
            return local
        if key in self.shared_keys:
            shared, mapping = self.shared_keys[key]
            cached = self.repo.get('procurement_records', shared)
            if cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached['created_at'])).total_seconds() < 86400:
                result = self.remap(copy.deepcopy(cached['result']), {v: k for k, v in mapping.items()})
                self.save(key, 'completed', result=result, scope={'cache_hit': True, 'cache_id': shared})
                return {'status': 'completed', 'result': result}
        return local

    def save(self, key, status, *, result=None, code=None, scope=None):
        self.repo.put('procurement_records', key, {'id': key, 'kind': 'analysis_node',
                      'task_id': self.task_id, 'created_at': datetime.now(timezone.utc).isoformat(),
                      'status': status, 'result': result, 'code': code, 'scope': scope or {}}, task_id=self.task_id)
        if status == 'completed' and key in self.shared_keys and not (scope or {}).get('cache_hit'):
            shared, mapping = self.shared_keys[key]
            self.repo.put('procurement_records', shared, {'id': shared, 'kind': 'analysis_cache',
                          'task_id': self.task_id, 'created_at': datetime.now(timezone.utc).isoformat(),
                          'result': self.remap(copy.deepcopy(result), mapping)}, task_id=self.task_id)

    def progress(self):
        nodes = [r for r in self.repo.list('procurement_records', self.task_id) if r.get('kind') == 'analysis_node']
        control = self.control()
        if control.get('status') == 'running' and not self.repo.active_analysis(self.task_id):
            control = {**control, 'status': 'interrupted'}
        return {'control': control, 'nodes': [{k: n.get(k) for k in ('id', 'status', 'code', 'scope')} for n in nodes]}
