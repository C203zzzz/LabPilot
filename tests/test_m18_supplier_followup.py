from fastapi.testclient import TestClient

from backend.main import create_app


def _task_and_candidate(client):
    task_id = client.post("/api/v1/tasks", json={"scenario": "采购检测设备"}).json()["task"]["id"]
    client.post(
        f"/api/v1/tasks/{task_id}/documents",
        files={"file": ("supplier.txt", "Tecan A-100", "text/plain")},
    )
    candidate = client.post(f"/api/v1/tasks/{task_id}/candidates").json()["items"][0]
    return task_id, candidate["id"]


def _confirm_final_and_create_inquiry(client, task_id, candidate_id):
    decision = client.put(
        f"/api/v1/tasks/{task_id}/final-decision",
        json={
            "status": "confirmed", "explicit_user_confirmation": True,
            "decided_by": "user-1", "note": "用户最终确认候选",
        },
    )
    assert decision.status_code == 200
    created = client.post(
        f"/api/v1/tasks/{task_id}/supplier-inquiries",
        json={"items": [{
            "candidate_id": candidate_id, "supplier": "供应商甲",
            "question": "请确认设备转速。", "reason": "现有证据缺失",
            "source_question_id": "m10-question-1", "fact_ids": [], "evidence_ids": [],
        }]},
    )
    assert created.status_code == 201
    return created.json()["items"][0]


def test_m18_full_reply_review_fact_draft_and_reevaluation_flow(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id, candidate_id = _task_and_candidate(client)
        initial = client.get(f"/api/v1/tasks/{task_id}/final-decision").json()
        assert initial["status"] == "pending_review"
        assert initial["explicit_user_confirmation"] is False

        inquiry = _confirm_final_and_create_inquiry(client, task_id, candidate_id)
        reply = client.post(
            f"/api/v1/tasks/{task_id}/supplier-inquiries/{inquiry['inquiry_id']}/replies",
            json={
                "supplier": "供应商甲", "received_at": "2026-09-18T10:30:00+08:00",
                "original_text": "A-100 的最高转速为 3000 rpm。",
                "source_location": "邮件正文第 2 段",
                "attachments": [{
                    "name": "A-100-spec.pdf", "document_id": "doc-supplier-spec-1",
                    "media_type": "application/pdf", "location": "page:3",
                }],
            },
        )
        assert reply.status_code == 201
        reply_body = reply.json()
        assert reply_body["review_status"] == "pending_review"
        assert reply_body["supplier"] == "供应商甲"
        assert reply_body["received_at"] == "2026-09-18T10:30:00+08:00"
        assert reply_body["original_text"].startswith("A-100")
        assert reply_body["attachments"][0]["document_id"] == "doc-supplier-spec-1"

        reviewed = client.post(
            f"/api/v1/tasks/{task_id}/supplier-replies/{reply_body['reply_id']}/review",
            json={
                "status": "confirmed", "explicit_user_confirmation": True,
                "reviewed_by": "user-1", "note": "已核对邮件与附件",
            },
        )
        assert reviewed.status_code == 200 and reviewed.json()["review_status"] == "confirmed"

        draft = client.post(
            f"/api/v1/tasks/{task_id}/supplier-replies/{reply_body['reply_id']}/fact-drafts",
            json={
                "candidate_id": candidate_id, "field_name": "speed", "value": 3000,
                "value_type": "number", "unit": "rpm",
            },
        )
        assert draft.status_code == 201
        fact = draft.json()
        assert fact["status"] == "pending_review"
        assert fact["source_reply_id"] == reply_body["reply_id"]
        assert fact["evidence"][0]["source_type"] == "supplier_reply"
        assert fact["evidence"][0]["document_id"] == f"supplier-reply:{reply_body['reply_id']}"

        requirement = {
            "scenario": "采购最高转速至少 2500 rpm 的检测设备", "status": "draft", "version": 1,
            "hard_constraints": [{"name": "speed", "value": {"min": 2500}, "kind": "hard"}],
            "soft_constraints": [], "unknowns": [],
        }
        assert client.post(
            f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": requirement}
        ).status_code == 200
        before = client.post(
            f"/api/v1/tasks/{task_id}/reevaluate", json={"candidate_ids": [candidate_id]}
        ).json()
        assert before["pending_fact_count"] == 1
        assert before["items"][0]["status"] == "needs_review"
        assert before["items"][0]["hard_constraints"][0]["missing_reason"] == "no_confirmed_fact"

        m06_review = client.post(
            f"/api/v1/tasks/{task_id}/facts/{fact['fact_id']}/review",
            json={"action": "confirm", "reviewer_id": "m06-reviewer", "note": "附件页码已复核"},
        )
        assert m06_review.status_code == 200 and m06_review.json()["status"] == "confirmed"
        after = client.post(
            f"/api/v1/tasks/{task_id}/reevaluate", json={"candidate_ids": [candidate_id]}
        ).json()
        assert after["pending_fact_count"] == 0
        assert after["items"][0]["status"] == "eligible"


def test_m18_rejects_implicit_decisions_and_out_of_order_operations(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "errors.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id, candidate_id = _task_and_candidate(client)
        implicit = client.put(
            f"/api/v1/tasks/{task_id}/final-decision",
            json={"status": "confirmed", "explicit_user_confirmation": False, "decided_by": "user-1"},
        )
        assert implicit.status_code == 422
        assert implicit.json()["error"]["code"] == "explicit_user_confirmation_required"

        blocked = client.post(
            f"/api/v1/tasks/{task_id}/supplier-inquiries",
            json={"items": [{
                "candidate_id": candidate_id, "supplier": "供应商甲", "question": "请确认转速",
                "reason": "缺失事实",
            }]},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "final_decision_not_confirmed"

        inquiry = _confirm_final_and_create_inquiry(client, task_id, candidate_id)
        mismatch = client.post(
            f"/api/v1/tasks/{task_id}/supplier-inquiries/{inquiry['inquiry_id']}/replies",
            json={
                "supplier": "另一供应商", "received_at": "2026-09-18T10:30:00+08:00",
                "original_text": "3000 rpm", "source_location": "邮件正文",
            },
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["error"]["code"] == "supplier_mismatch"

        reply = client.post(
            f"/api/v1/tasks/{task_id}/supplier-inquiries/{inquiry['inquiry_id']}/replies",
            json={
                "supplier": "供应商甲", "received_at": "2026-09-18T10:30:00+08:00",
                "original_text": "3000 rpm", "source_location": "邮件正文",
            },
        ).json()
        premature = client.post(
            f"/api/v1/tasks/{task_id}/supplier-replies/{reply['reply_id']}/fact-drafts",
            json={
                "candidate_id": candidate_id, "field_name": "speed", "value": 3000,
                "value_type": "number", "unit": "rpm",
            },
        )
        assert premature.status_code == 422
        assert premature.json()["error"]["code"] == "reply_not_confirmed"

        implicit_reply_review = client.post(
            f"/api/v1/tasks/{task_id}/supplier-replies/{reply['reply_id']}/review",
            json={"status": "confirmed", "explicit_user_confirmation": False, "reviewed_by": "user-1"},
        )
        assert implicit_reply_review.status_code == 422
        assert implicit_reply_review.json()["error"]["code"] == "explicit_user_confirmation_required"


def test_m18_rejects_unlocatable_attachment_and_cross_task_access(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "scope.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id, candidate_id = _task_and_candidate(client)
        other_task, _ = _task_and_candidate(client)
        inquiry = _confirm_final_and_create_inquiry(client, task_id, candidate_id)

        bad_attachment = client.post(
            f"/api/v1/tasks/{task_id}/supplier-inquiries/{inquiry['inquiry_id']}/replies",
            json={
                "supplier": "供应商甲", "received_at": "2026-09-18T10:30:00+08:00",
                "original_text": "见附件", "source_location": "邮件正文",
                "attachments": [{"name": "unknown.pdf"}],
            },
        )
        assert bad_attachment.status_code == 422
        assert bad_attachment.json()["error"]["code"] == "missing_source_locator"

        cross_task = client.post(
            f"/api/v1/tasks/{other_task}/supplier-inquiries/{inquiry['inquiry_id']}/replies",
            json={
                "supplier": "供应商甲", "received_at": "2026-09-18T10:30:00+08:00",
                "original_text": "3000 rpm", "source_location": "邮件正文",
            },
        )
        assert cross_task.status_code == 404
        assert cross_task.json()["detail"]["code"] == "inquiry_not_found"
