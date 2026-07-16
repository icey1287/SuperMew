# 前端质量与体积门禁

前端治理 Interface 由 `frontend/package.json` 中的脚本和
`.github/workflows/frontend-quality.yml` 共同定义。CI 使用锁文件安装依赖，并执行与本地相同的
格式、Lint、类型、单元测试、生产构建、体积、依赖审计和浏览器 E2E 门禁。

## 本地验证

```bash
cd frontend
npm ci
npm run format:check
npm run lint
npm run typecheck
npm run test:unit
npm run build:check
npm audit --audit-level=high
npm run test:e2e:install
npm run test:e2e
```

Playwright E2E 使用 route mock 验证未登录 Shell，以及
`create Run → GET Event stream → reducer → assistant Message` 的真实浏览器投影。测试不依赖本地
模型、数据库、Redis 或 Milvus。

## Bundle 预算

`npm run bundle:size` 读取 Vite manifest 与实际 gzip 结果，并阻断以下回归：

- entry JavaScript 原始体积超过 180 KiB；
- 任一 JavaScript chunk 超过 300 KiB；
- 任一 stylesheet 超过 140 KiB；
- 初始加载 JavaScript gzip 合计超过 220 KiB。

当前架构把认证后的 Chat、History 与 Document Settings 异步加载，并把 Framework、HTTP、
Markdown/Highlight 与其他 vendor 分成独立 chunk。修改 `manualChunks`、Markdown 语言集或静态
import 时必须重新运行真实体积门禁，不能只调整 Vite warning threshold。
