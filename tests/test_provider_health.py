from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.infrastructure.search_providers import SogouSearchProvider
from backend.main import create_app
from backend.services.deepseek_analysis import CANONICAL_FIELD_NAMES
from backend.services.vision import OpenAICompatibleVisionAdapter, VisionAdapterError, VisionResult
from fastapi.testclient import TestClient
from PIL import Image


def test_provider_status_does_not_expose_runtime_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "runtime-only")
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.get("/api/v1/providers")
    assert response.status_code == 200
    assert response.json()["status"] == "configured"
    assert "runtime-only" not in response.text


def test_reasoning_health_check_uses_in_memory_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.post("/api/v1/providers/reasoning/health-check", json={
            "provider": "OpenAI-compatible", "model": "deepseek-chat", "api_key": "runtime-only",
        })
    assert response.status_code == 200
    assert response.json()["status"] == "configured"
    assert "runtime-only" not in response.text


def test_search_health_check_requires_explicit_web_source(monkeypatch, tmp_path):
    monkeypatch.setattr(DeepSeekClient, "search_web", lambda self, **_: [{
        "url": "https://www.example.org/source", "title": "来源", "snippet": "正文",
    }])
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.post("/api/v1/providers/search/health-check", json={"api_key": "runtime-only"})
    assert response.status_code == 200
    assert response.json()["status"] == "configured"


def test_provider_health_without_key_is_configuration_error(tmp_path):
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        response = client.post("/api/v1/providers/reasoning/health-check", json={})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_not_configured"


def test_payment_required_keeps_runtime_client_and_reports_actionable_status(monkeypatch, tmp_path):
    def insufficient_balance(self, **_kwargs):
        raise DeepSeekError(
            "provider_payment_required",
            "DeepSeek 账户余额不足，请充值或恢复计费后重试",
        )

    monkeypatch.setattr(DeepSeekClient, "analyze", insufficient_balance)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        health = client.post("/api/v1/providers/reasoning/health-check", json={
            "model": "deepseek-flash", "api_key": "runtime-only",
        })
        providers = client.get("/api/v1/providers")
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.txt", "Model X100", "text/plain")},
        )
        analysis = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert health.status_code == 402
    assert health.json()["detail"]["code"] == "provider_payment_required"
    assert providers.json()["status"] == "payment_required"
    assert providers.json()["issue"]["code"] == "provider_payment_required"
    assert next(item for item in providers.json()["items"] if item["id"] == "reasoning")["status"] == "payment_required"
    assert next(item for item in providers.json()["items"] if item["id"] == "vision")["status"] == "payment_required"
    assert analysis.status_code == 402
    assert analysis.json()["detail"]["code"] == "provider_payment_required"
    assert "runtime-only" not in health.text + providers.text + analysis.text


def test_successful_retry_clears_payment_required_status(monkeypatch, tmp_path):
    calls = 0

    def recover_after_top_up(self, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DeepSeekError(
                "provider_payment_required",
                "DeepSeek 账户余额不足，请充值或恢复计费后重试",
            )
        return {"ok": True}

    monkeypatch.setattr(DeepSeekClient, "analyze", recover_after_top_up)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        first = client.post(
            "/api/v1/providers/reasoning/health-check",
            json={"api_key": "runtime-only"},
        )
        second = client.post(
            "/api/v1/providers/reasoning/health-check",
            json={"api_key": "runtime-only"},
        )
        providers = client.get("/api/v1/providers").json()

    assert first.status_code == 402
    assert second.status_code == 200
    assert providers["status"] == "configured"
    assert providers["issue"] is None
    assert next(item for item in providers["items"] if item["id"] == "reasoning")["status"] == "configured"


def test_successful_analysis_retry_clears_payment_required_status(monkeypatch, tmp_path):
    calls = 0

    def recover_during_analysis(self, *, system_prompt, user_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DeepSeekError(
                "provider_payment_required",
                "DeepSeek 账户余额不足，请充值或恢复计费后重试",
            )
        if "候选清单整理器" in system_prompt:
            return {"candidates": [{
                "brand": "Acme", "model": "X100",
                "sources": [{
                    "document_id": uploaded_id, "page_or_location": "page:1",
                    "excerpt": "Model X100",
                }],
            }]}
        return {"candidates": [{"model": "X100", "fields": []}]}

    monkeypatch.setattr(DeepSeekClient, "analyze", recover_during_analysis)
    uploaded_id = ""
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        first = client.post(
            "/api/v1/providers/reasoning/health-check",
            json={"api_key": "runtime-only"},
        )
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.txt", "Model X100", "text/plain")},
        ).json()
        uploaded_id = uploaded["id"]
        analysis = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        providers = client.get("/api/v1/providers").json()

    assert first.status_code == 402
    assert analysis.status_code == 200
    assert providers["status"] == "configured"
    assert providers["issue"] is None


def test_successful_health_check_configures_analysis_for_current_process(monkeypatch, tmp_path):
    def analyze(self, *, system_prompt, user_prompt):
        if "连接测试" in user_prompt:
            return {"ok": True}
        return {"candidates": [{
            "brand": "Acme", "model": "X100",
            "sources": [{"document_id": "doc", "page_or_location": "page:1", "excerpt": "Model X100"}],
            "fields": [],
        }]}

    monkeypatch.setattr(DeepSeekClient, "analyze", analyze)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/reasoning/health-check", json={"api_key": "runtime-only"}).status_code == 200
        created = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()
        task_id = created["task"]["id"]
        client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "Model X100", "text/plain")})
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
    assert response.status_code == 200
    assert response.json()["candidates"][0]["model"] == "X100"


def test_successful_health_check_reuses_same_api_for_vision(monkeypatch, tmp_path):
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "probe", lambda self: None)
    observed = {}

    def analyze_image(self, image_bytes, *, media_type, source_location):
        observed.update(model=self.model, endpoint=self.endpoint, timeout=self.timeout)
        return VisionResult(
            text="Acme X100",
            fields={"model": "X100"},
            source_refs=[{"location": source_location, "quote": "Acme X100"}],
        )

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "analyze", analyze_image)
    image_path = tmp_path / "spec.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        connected = client.post("/api/v1/providers/reasoning/health-check", json={
            "model": "vision-capable-model",
            "base_url": "https://gateway.example/v1",
            "api_key": "runtime-only",
        })
        assert connected.status_code == 200
        providers = client.get("/api/v1/providers").json()["items"]
        assert next(item for item in providers if item["id"] == "vision")["status"] == "ready_to_test"
        verified = client.post("/api/v1/providers/vision/health-check", json={})
        assert verified.status_code == 200
        providers = client.get("/api/v1/providers").json()["items"]
        assert next(item for item in providers if item["id"] == "vision")["status"] == "configured"
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.png", image_path.read_bytes(), "image/png")},
        ).json()
        result = client.post(f"/api/v1/tasks/{task_id}/documents/{uploaded['id']}/ocr")
    assert result.status_code == 200
    assert result.json()["ocr_status"] == "completed"
    assert observed == {
        "model": "vision-capable-model",
        "endpoint": "https://gateway.example/v1/chat/completions",
        "timeout": 45.0,
    }


def test_upload_uses_the_single_model_api_for_vision(monkeypatch, tmp_path):
    """One browser-configured API must drive both calls, upload included.

    The vision slot already reused the reasoning client, but only the explicit
    ``/ocr`` route handed that adapter down. ``upload_document`` called
    ``ingest_file`` with no adapter at all, so an uploaded scan was read by
    whatever adapter the app was constructed with (usually none): a user who
    configured a single multimodal model in the browser got reasoning from it
    and nothing at all for images.
    """
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})

    observed = {}

    def analyze_image(self, image_bytes, *, media_type, source_location):
        observed.update(model=self.model, endpoint=self.endpoint)
        return VisionResult(
            text="Acme X100",
            fields={"model": "X100"},
            source_refs=[{"location": source_location, "quote": "Acme X100"}],
        )

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "analyze", analyze_image)
    image_path = tmp_path / "spec.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        connected = client.post("/api/v1/providers/reasoning/health-check", json={
            "model": "vision-capable-model",
            "base_url": "https://gateway.example/v1",
            "api_key": "runtime-only",
        })
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        # Deliberately no /ocr call: the upload itself must use the unified API.
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.png", image_path.read_bytes(), "image/png")},
        )

    assert connected.status_code == 200
    assert uploaded.status_code == 201
    assert observed == {
        "model": "vision-capable-model",
        "endpoint": "https://gateway.example/v1/chat/completions",
    }


def test_runtime_vision_adapter_comes_from_the_thinking_model_client(monkeypatch, tmp_path):
    """删掉独立视觉端点后，视觉的唯一来源就是思考模型那个客户端。

    不注入 ``multimodal_adapter``、只给 ``runtime_provider_client`` 时，
    ``_runtime_vision_adapter()`` 现造的适配器必须原样使用该客户端的
    key/endpoint/model，而不是任何环境变量里的第二套配置。
    """
    for name in ("LAB_VISION_API_KEY", "LAB_VISION_ENDPOINT", "LAB_VISION_BASE_URL", "LAB_VISION_MODEL"):
        monkeypatch.setenv(name, "must-not-be-read")
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    captured = {}

    def analyze_image(self, image_bytes, *, media_type, source_location):
        captured.update(
            api_key=self.api_key, endpoint=self.endpoint,
            model=self.model, provider=self.provider,
        )
        return VisionResult(text="Acme X100", fields={"model": "X100"})

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "analyze", analyze_image)
    image_path = tmp_path / "spec.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/reasoning/health-check", json={
            "model": "vision-capable-model",
            "base_url": "https://gateway.example/v1",
            "api_key": "runtime-only",
        }).status_code == 200
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.png", image_path.read_bytes(), "image/png")},
        )

    assert uploaded.status_code == 201
    assert captured == {
        "api_key": "runtime-only",
        "endpoint": "https://gateway.example/v1/chat/completions",
        "model": "vision-capable-model",
        "provider": "runtime-reasoning-model-vision",
    }


def test_runtime_vision_chain_uses_the_configured_retry_budget(monkeypatch, tmp_path):
    """只配一个模型 API 的部署，读图走的是运行时现造的适配器。

    这条链才是真正读图的那条；它过去不传 ``json_retries``，默认值在这条链上
    形同虚设。
    """
    monkeypatch.delenv("LAB_VISION_JSON_RETRIES", raising=False)
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "probe", lambda self: None)
    observed = []

    def analyze_image(self, image_bytes, *, media_type, source_location):
        observed.append(self.json_retries)
        return VisionResult(text="Acme X100", fields={"model": "X100"})

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "analyze", analyze_image)
    image_path = tmp_path / "spec.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")

    def upload(client, task_id):
        return client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.png", image_path.read_bytes(), "image/png")},
        )

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/reasoning/health-check", json={"api_key": "runtime-only"}).status_code == 200
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        assert upload(client, task_id).status_code == 201
        assert observed == [1]  # 没设环境变量时的默认值，不是 0
        monkeypatch.setenv("LAB_VISION_JSON_RETRIES", "2")
        assert upload(client, task_id).status_code == 201
        assert observed == [1, 2]  # 环境变量能盖住默认值
        monkeypatch.setenv("LAB_VISION_JSON_RETRIES", "0")
        assert upload(client, task_id).status_code == 201
        assert observed == [1, 2, 0]  # 每次请求重新解析，0 表示确实不重试


def test_runtime_vision_chain_refuses_an_unusable_retry_setting(monkeypatch, tmp_path):
    """配置错误不许被兜底成"不重试"：这条链必须响亮失败，且浏览器要能读到。

    旧表现是异常一路冒到 Starlette 最外层的 ``ServerErrorMiddleware``：裸 500、
    不带 CORS 头，浏览器因此把响应判成网络失败、只能显示"服务未连接"。
    """
    monkeypatch.setenv("LAB_VISION_JSON_RETRIES", "9")
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    image_path = tmp_path / "spec.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")
    origin = "http://127.0.0.1:5173"

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/reasoning/health-check", json={"api_key": "runtime-only"}).status_code == 200
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        response = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.png", image_path.read_bytes(), "image/png")},
            headers={"Origin": origin},
        )

    assert response.status_code == 502
    assert response.headers["access-control-allow-origin"] == origin
    detail = response.json()["detail"]
    assert detail["code"] == "vision_provider_not_configured"
    assert detail["field"] == "LAB_VISION_JSON_RETRIES"
    assert "LAB_VISION_JSON_RETRIES" in detail["message"]
    assert "9" in detail["message"]
    assert "0～3" in detail["message"]


def test_runtime_vision_chain_keeps_the_timeout_floor_and_honours_the_setting(monkeypatch, tmp_path):
    """超时口子从独立端点搬到这条链上之后，默认值与校验必须一模一样。

    改动前这条链是 ``max(client.timeout, 45.0)``：没设 ``LAB_VISION_TIMEOUT``
    时适配器仍拿 45 秒；设了就在客户端超时之上抬高；非法值必须响亮失败，
    不许被兜底成 45。
    """
    monkeypatch.delenv("LAB_VISION_TIMEOUT", raising=False)
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    observed = []

    def analyze_image(self, image_bytes, *, media_type, source_location):
        observed.append(self.timeout)
        return VisionResult(text="Acme X100", fields={"model": "X100"})

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "analyze", analyze_image)
    image_path = tmp_path / "spec.png"
    Image.new("RGB", (40, 30), "white").save(image_path, format="PNG")

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/reasoning/health-check", json={"api_key": "runtime-only"}).status_code == 200
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]

        def upload():
            return client.post(
                f"/api/v1/tasks/{task_id}/documents",
                files={"file": ("spec.png", image_path.read_bytes(), "image/png")},
            )

        assert upload().status_code == 201
        assert observed == [45.0]  # 没设环境变量时仍是 45 秒下限
        monkeypatch.setenv("LAB_VISION_TIMEOUT", "90")
        assert upload().status_code == 201
        assert observed == [45.0, 90.0]
        monkeypatch.setenv("LAB_VISION_TIMEOUT", "181")
        response = upload()

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "vision_provider_not_configured"
    assert detail["field"] == "LAB_VISION_TIMEOUT"
    assert "LAB_VISION_TIMEOUT" in detail["message"]
    assert "181" in detail["message"]


def test_configuration_error_reaches_the_browser_on_every_vision_entry_point(monkeypatch, tmp_path):
    """``LAB_VISION_JSON_RETRIES`` 写错时，每个入口都要给出可读的 502。

    实验 4 的原始表现（``docs/245`` 第 77 行）：``GET /providers``、上传、分析一律
    500，而且是**不带 CORS 头**的裸 ``Internal Server Error``；缺
    ``Access-Control-Allow-Origin`` 时浏览器把响应判成网络失败，用户看到的是
    "暂时无法连接，请检查网络或重新启动应用"。这里把三个入口和健康检查钉在一起：
    状态码、CORS 头、code、人话（点名配置项与原值）缺一不可。
    """
    monkeypatch.setenv("LAB_VISION_JSON_RETRIES", "abc")
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    origin = "http://127.0.0.1:5173"
    expected = "配置项 LAB_VISION_JSON_RETRIES 的值 abc 不是整数，允许范围 0～3（视觉识别失败后的重试次数）"

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/reasoning/health-check", json={"api_key": "runtime-only"}).status_code == 200
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        responses = {
            "providers": client.get("/api/v1/providers", headers={"Origin": origin}),
            "upload": client.post(
                f"/api/v1/tasks/{task_id}/documents",
                files={"file": ("spec.txt", b"hello", "text/plain")},
                headers={"Origin": origin},
            ),
            "analysis": client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek", json={}, headers={"Origin": origin}),
            "vision_health_check": client.post("/api/v1/providers/vision/health-check", json={}, headers={"Origin": origin}),
        }

    for name, response in responses.items():
        assert response.status_code == 502, name
        assert response.headers.get("access-control-allow-origin") == origin, name
        detail = response.json()["detail"]
        assert detail["code"] == "vision_provider_not_configured", name
        assert detail["message"] == expected, name
        assert detail["field"] == "LAB_VISION_JSON_RETRIES", name


def test_vision_status_tells_available_apart_from_verified(monkeypatch, tmp_path):
    """``ready_to_test`` 既不是「已连接」也不是「未连接」，接口必须把两件事分开说。

    ``vision_ready = vision_available and runtime_vision_verified``（routes.py:454-455）：
    - ``ready_to_test`` = 适配器现在就能构造，但本进程还没成功读过一张真图；
      按钮可用，第一次识别会真的调模型 —— 前端把它渲染成「未连接」是错的。
    - ``configured`` 才代表已经通过真实图片验证 —— 没验证过的不许冒充。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})
    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "probe", lambda self: None)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        def vision():
            return next(item for item in client.get("/api/v1/providers").json()["items"] if item["id"] == "vision")

        # 1. 没有模型客户端也没有注入适配器：真的不可用。
        assert vision()["status"] == "unavailable"
        assert vision()["adapter_available"] is False
        assert vision()["verified"] is False
        unavailable_label = vision()["label"]
        # 2. 配上一个模型接口之后：适配器可构造，但还没验证过。
        assert client.post("/api/v1/providers/reasoning/health-check", json={
            "model": "vision-capable-model", "api_key": "runtime-only",
        }).status_code == 200
        ready = vision()
        assert ready["status"] == "ready_to_test"
        assert ready["adapter_available"] is True
        assert ready["verified"] is False
        assert ready["label"] != unavailable_label
        assert ready["detail"]
        # 3. 真实图片探针通过之后才算 configure。
        assert client.post("/api/v1/providers/vision/health-check", json={}).status_code == 200
        verified = vision()
        assert verified["status"] == "configured"
        assert verified["adapter_available"] is True
        assert verified["verified"] is True


def test_failed_image_probe_does_not_report_vision_as_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(DeepSeekClient, "analyze", lambda self, **_: {"ok": True})

    def fail_probe(self):
        raise VisionAdapterError("vision_probe_failed", "模型未能正确读取图像内容")

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "probe", fail_probe)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        connected = client.post(
            "/api/v1/providers/reasoning/health-check",
            json={"model": "text-only-model", "api_key": "runtime-only"},
        )
        failed = client.post("/api/v1/providers/vision/health-check", json={})
        providers = client.get("/api/v1/providers").json()["items"]

    assert connected.status_code == 200
    assert failed.status_code == 502
    assert failed.json()["detail"]["code"] == "vision_probe_failed"
    assert next(item for item in providers if item["id"] == "vision")["status"] == "ready_to_test"
    assert "runtime-only" not in failed.text


def test_analysis_only_calls_vision_for_pages_selected_by_adaptive_route(monkeypatch, tmp_path):
    import fitz

    pdf_path = tmp_path / "hybrid.pdf"
    pdf = fitz.open()
    native = pdf.new_page(width=500, height=700)
    native.insert_textbox(
        (40, 40, 460, 660),
        "Selectable technical specification for model X100 with enough native text. " * 5,
        fontsize=11,
    )
    pdf.new_page(width=500, height=700)
    pdf_path.write_bytes(pdf.tobytes())
    pdf.close()

    vision_calls = []

    def analyze_image(self, image_bytes, *, media_type, source_location):
        vision_calls.append(source_location)
        return VisionResult(
            text="Acme X100 scanned specification",
            fields={"candidate_mentions": [{"brand": "Acme", "model": "X100"}]},
            source_refs=[{"location": source_location, "quote": "Acme X100"}],
        )

    def analyze(self, *, system_prompt, user_prompt):
        if "连接测试" in user_prompt:
            return {"ok": True}
        if "候选清单整理器" in system_prompt:
            return {"candidates": [{
                "brand": "Acme", "model": "X100",
                "sources": [{
                    "document_id": uploaded_id,
                    "page_or_location": "page:2/rendered",
                    "excerpt": "Acme X100",
                }],
            }]}
        return {"candidates": [{
            "brand": "Acme", "model": "X100",
            "sources": [{
                "document_id": uploaded_id,
                "page_or_location": "page:2/rendered",
                "excerpt": "Acme X100",
            }],
            "fields": [{
                "field_name": field_name, "value": "unknown",
                "status": "unknown", "sources": [],
            } for field_name in CANONICAL_FIELD_NAMES],
        }]}

    monkeypatch.setattr(OpenAICompatibleVisionAdapter, "analyze", analyze_image)
    monkeypatch.setattr(DeepSeekClient, "analyze", analyze)
    uploaded_id = ""
    with TestClient(create_app(
        database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage",
    )) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("hybrid.pdf", pdf_path.read_bytes(), "application/pdf")},
        ).json()
        uploaded_id = uploaded["id"]
        assert [page["route"] for page in uploaded["pages"]] == ["native_text", "vision"]
        assert client.post(
            "/api/v1/providers/reasoning/health-check",
            json={"model": "vision-capable-model", "api_key": "runtime-only"},
        ).status_code == 200

        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        persisted = client.get(f"/api/v1/tasks/{task_id}/documents").json()["items"][0]

    assert response.status_code == 200
    assert vision_calls == ["page:2/rendered"]
    assert next(
        image for image in persisted["images"]
        if image["source_location"] == "page:2/rendered"
    )["ocr_status"] == "completed"
    assert not any(image["source_location"] == "page:1/rendered" for image in persisted["images"])


def test_successful_search_health_check_activates_model_search(monkeypatch, tmp_path):
    monkeypatch.setattr(DeepSeekClient, "search_web", lambda self, **_: [{
        "url": "https://www.example.org/product", "title": "产品页", "snippet": "模型工具摘要",
    }])
    monkeypatch.setattr(SogouSearchProvider, "fetch", lambda self, url, timeout: "产品正文 Model X100")
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        assert client.post("/api/v1/providers/search/health-check", json={"api_key": "runtime-only"}).status_code == 200
        created = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()
        task_id = created["task"]["id"]
        client.post(f"/api/v1/tasks/{task_id}/requirements/confirm", json={"requirement": created["requirement"]})
        response = client.post(f"/api/v1/tasks/{task_id}/search", json={"query": "Model X100 official", "fetch_body": True})
    assert response.status_code == 200
    source = response.json()["items"][0]["sources"][0]
    assert source["provider"] == "deepseek_web"
    assert source["body_status"] == "fetched"
