# 参与开发

## 起后端

Python 3.11+。Windows 用 `.venv/Scripts/`，Linux 和 macOS 用 `.venv/bin/`。

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"
.venv/Scripts/uvicorn backend.main:app --port 8000
```

需要读扫描件时再装解析栈。Docling 和 RapidOCR 比较大，首次调用会下载 OCR 模型：

```bash
.venv/Scripts/pip install -e ".[parsing]"
```

不配密钥也能起界面：落地页、新建任务、资料上传和连接设置都能打开。完整分析要在 `.env` 里配 `DEEPSEEK_API_KEY`（模板见 `.env.example`）。密钥只放本地，不要提交 `.env`。

## 起前端

Node.js 22.6+（`npm test` 依赖该版本起支持的 `node --test` glob）。

```bash
cd frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`，接口默认打到 `http://127.0.0.1:8000/api/v1`，用 `VITE_API_BASE` 覆盖。

## 跑测试

```bash
python -m pytest tests -q     # 后端
cd frontend && npm test       # 前端
```

没装 `.[parsing]` 时，两条本地 OCR 用例会按缺依赖跳过，这不是失败。CI（`.github/workflows/tests.yml`）跑的是同一套命令，环境里同样不带解析栈。

## 提交前

- 有代码改动就跑一遍上面的测试；只改文档可以跳过。
- 提交信息写一行标题，说清解决了什么问题。
- 不要提交密钥、Cookie、`.env`、`storage/` 下的运行数据，以及 `data/` 里除 `xlyg_prices.sqlite3` 以外的文件。

## 目录

```text
backend/     FastAPI 服务、检索与价格判据、SQLite 存储
frontend/    React 工作台
tests/       后端测试
scripts/     启动与一次性脚本
data/        随仓库发布的只读成交价库
docs/        架构说明、界面截图、演示视频
```
