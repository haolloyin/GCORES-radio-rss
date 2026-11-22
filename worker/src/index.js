/**
 * gcores-rss 定时触发器
 *
 * Cloudflare Cron Trigger（每小时）→ 调 GitHub API 的 workflow_dispatch，
 * 由 Actions 去跑 gcores_rss.py。
 *
 * 为什么不用 GitHub 自带的 schedule：
 * 那个是「尽力而为」的，低活跃仓库会被降频，实测一天只跑 4~6 次、
 * 而且时间完全漂移。workflow_dispatch 不受这个限流影响，派发即创建 run。
 */

const API = "https://api.github.com";

const DEFAULTS = {
  repo: "haolloyin/GCORES-radio-rss",
  workflow: "rss.yml",
  ref: "main",
};

export default {
  // Cron Trigger 入口
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env, `cron ${event.cron}`));
  },

  // 手动测试入口：GET /run 立刻派发一次；GET / 返回健康状态
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/run") {
      const result = await run(env, "manual");
      return json(result, result.ok ? 200 : 502);
    }
    return json({
      ok: true,
      service: "gcores-rss-cron",
      repo: env.REPO || DEFAULTS.repo,
      workflow: env.WORKFLOW || DEFAULTS.workflow,
      hint: "GET /run 可手动派发一次",
    });
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GH_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "gcores-rss-cron",
    "Content-Type": "application/json",
  };
}

async function latestRun(env) {
  const repo = env.REPO || DEFAULTS.repo;
  const wf = env.WORKFLOW || DEFAULTS.workflow;
  const res = await fetch(
    `${API}/repos/${repo}/actions/workflows/${wf}/runs?per_page=1`,
    { headers: ghHeaders(env) }
  );
  if (!res.ok) return null;
  const data = await res.json();
  return (data.workflow_runs || [])[0] || null;
}

const BUSY = ["queued", "in_progress", "waiting", "requested", "pending"];

async function run(env, source) {
  const repo = env.REPO || DEFAULTS.repo;
  const wf = env.WORKFLOW || DEFAULTS.workflow;
  const ref = env.REF || DEFAULTS.ref;

  // 上一轮还在排队/运行中就别再派发，免得堆一串重复的 run
  try {
    const last = await latestRun(env);
    if (last && BUSY.includes(last.status)) {
      const ageMin = (Date.now() - new Date(last.created_at).getTime()) / 60000;
      if (ageMin < 30) {
        const out = {
          ok: true,
          skipped: `已有 ${last.status} 的 run #${last.run_number}`,
          age_min: Math.round(ageMin),
          source,
        };
        console.log(JSON.stringify(out));
        return out;
      }
    }
  } catch (err) {
    // 查询失败不影响派发
    console.warn("查询最近一次运行失败：" + err.message);
  }

  let lastError = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    const res = await fetch(
      `${API}/repos/${repo}/actions/workflows/${wf}/dispatches`,
      { method: "POST", headers: ghHeaders(env), body: JSON.stringify({ ref }) }
    );

    if (res.status === 204) {
      const out = { ok: true, dispatched: `${repo}/${wf}@${ref}`, source, attempt };
      console.log(JSON.stringify(out));
      return out;
    }

    lastError = `${res.status} ${(await res.text()).slice(0, 300)}`;
    if (res.status === 401 || res.status === 403 || res.status === 404) break; // 重试也没用
    await new Promise((r) => setTimeout(r, attempt * 1000));
  }

  const out = { ok: false, error: lastError, source };
  console.error(JSON.stringify(out));
  return out;
}
