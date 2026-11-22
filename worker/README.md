# Cloudflare Worker 定时触发器

用 Cloudflare 的 Cron Trigger 每小时调一次 GitHub API，让 Actions 跑 `gcores_rss.py`。

为什么需要它：GitHub 自带的 `on: schedule` 是尽力而为的，低活跃仓库会被降频，实测两天只跑了 6 次、时间完全漂移（间隔 4～8 小时）。`workflow_dispatch` 走的是另一条路，派发即创建 run，不受这个限流影响。

```
Cloudflare Cron (每小时 :00)
        │
        ▼
POST /repos/haolloyin/GCORES-radio-rss/actions/workflows/rss.yml/dispatches
        │
        ▼
GitHub Actions 跑 gcores_rss.py → commit & push
```

## 一、建一个 GitHub PAT

1. 打开 <https://github.com/settings/personal-access-tokens/new>
2. **Token name**：`gcores-rss-cron`
3. **Expiration**：建议 `No expiration`（否则到期后定时器会静默失效，记得回来续）
4. **Repository access** → `Only select repositories` → 选 `GCORES-radio-rss`
5. **Permissions** → `Repository permissions` 里把 **Actions** 设为 `Read and write`（`Metadata` 会自动变成 Read-only，不用管）
6. Generate token，复制 `github_pat_...`（只显示一次）

## 二、部署 Worker

### 方式 A：网页控制台（不用装任何东西）

1. 打开 <https://dash.cloudflare.com/> → 左侧 **Workers & Pages** → **Create** → **Worker**
2. 名字填 `gcores-rss-cron` → **Deploy**
3. 点 **Edit code**，把 `worker/src/index.js` 的内容整段粘进去 → **Deploy**
4. 回到 Worker 详情 → **Settings** → **Variables and Secrets** → **Add**
   - Type 选 **Secret**，Name 填 `GH_TOKEN`，Value 粘贴上一步的 PAT → Save
5. 同一页往下 **Trigger Events** → **Cron Triggers** → **Add Cron Trigger**
   - 填 `0 * * * *` → Add
6. 打开 `https://gcores-rss-cron.<你的子域>.workers.dev/run` 测试，返回 `{"ok": true, "dispatched": ...}` 就成功了，去 Actions 页面能看到一次 `workflow_dispatch` 的 run

> `REPO` / `WORKFLOW` / `REF` 在代码里已有默认值，网页方式可以完全不配。
> 如果 `workers.dev` 打不开（国内网络可能被墙），不影响定时任务——Cron 是 Cloudflare 内部触发，不需要你访问它。

### 方式 B：wrangler CLI

```bash
cd worker
npx wrangler login
npx wrangler secret put GH_TOKEN     # 粘贴 PAT
npx wrangler deploy                  # 会连同 wrangler.toml 里的 cron 一起部署
```

改完代码重新 `npx wrangler deploy` 即可。

## 三、验证

- Worker 日志：控制台 → 你的 Worker → **Logs**，每小时会有一条 `{"ok":true,"dispatched":...}`
- Actions 页面：Event 列显示 `workflow_dispatch`，每小时一条
- 如果返回 401：PAT 没填对或过期
- 如果返回 404：PAT 没有这个仓库的权限，或 `rss.yml` 路径不对
- 如果返回 403：PAT 缺 `Actions: Read and write`

## 四、想停掉

- 临时停：Worker → Settings → Trigger Events → 删掉 Cron Trigger
- 彻底停：直接删掉这个 Worker
- GitHub 侧的 `on: schedule` 我留了一份当兜底，两边都停才会完全不再刷新
