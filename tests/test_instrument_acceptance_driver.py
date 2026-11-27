"""Guard blind isolation and artifact integrity without calling paid providers."""
import hashlib
import io
import json
from zipfile import ZipFile

import pytest

from scripts.run_instrument_acceptance import validate_manifest, verify_export, verify_gate


def item(i, category):
    return {'id': f'sample-{i}', 'sha256': str(i)*64, 'category': category,
            'single_model_verified': True, 'pages': 2}


def test_blind_requires_distinct_categories_and_at_most_five():
    with pytest.raises(ValueError, match='不同品类'):
        validate_manifest({'files': [item(1, '离心机'), item(2, '离心机')]}, blind=True)
    with pytest.raises(ValueError, match='1至5'):
        validate_manifest({'files': [item(i, f'类别{i}') for i in range(6)]}, blind=True)
    assert len(validate_manifest({'files': [item(i, f'类别{i}') for i in range(5)]}, blind=True)) == 5


def test_unreviewed_pdf_cannot_be_registered():
    with pytest.raises(ValueError, match='单主机'):
        validate_manifest({'files': [{**item(1, '灭菌器'), 'single_model_verified': False}]})


def test_gate_rejects_missing_frontend_and_tampered_evidence(tmp_path):
    evidence = tmp_path/'ui-result.json'
    evidence.write_text('{"passed":true}', encoding='utf-8')
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    gate = {'stage': 'development', 'status': 'passed', 'code_fingerprint': {'app': '123'},
            'unresolved_fixable_defects': [], 'original_review_completed': True,
            'workflow_verified': True, 'cross_template_regression_verified': True,
            'evidence': [{'path': str(evidence), 'sha256': digest}]}
    with pytest.raises(ValueError, match='frontend_e2e_verified'):
        verify_gate(gate, 'development', {'app': '123'})
    gate['frontend_e2e_verified'] = True
    verify_gate(gate, 'development', {'app': '123'})
    evidence.write_text('{"passed":false}', encoding='utf-8')
    with pytest.raises(ValueError, match='被修改'):
        verify_gate(gate, 'development', {'app': '123'})


def test_zip_crc_alone_does_not_prove_original_snapshot_integrity():
    content = io.BytesIO()
    with ZipFile(content, 'w') as archive:
        archive.writestr('report.md', 'report')
        archive.writestr('evidence/original.bin', b'wrong')
        archive.writestr('report-来源清单.json', json.dumps([{'original_status': 'included',
            'original_file': 'evidence/original.bin', 'sha256': hashlib.sha256(b'right').hexdigest(), 'size': 5}]))
    with pytest.raises(ValueError, match='校验失败'):
        verify_export(content.getvalue())
