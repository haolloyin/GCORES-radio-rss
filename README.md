# 机核播客 RSS

抓取机核（GCORES）播客列表，生成三个带时间轴的 RSS 订阅文件，并可通过 GitHub Actions 每小时自动刷新。

| 文件 | 内容 | 来源 |
| --- | --- | --- |
| `gcores.xml` | 机核出品（完整版：简介 + 参与者 + 正文 + 时间轴） | https://www.gcores.com/radios?filter=gcores |
| `gcores-not-detail.xml` | 机核出品（简介与正文完整，时间轴不含每个时间点的描述） | 同上 |
| `join.xml` | 入驻播客 | https://www.gcores.com/radios?filter=join |

## 订阅地址

仓库推到 GitHub 后（仓库需为 **public**，否则订阅器无法访问）：

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/gcores.xml
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/gcores-not-detail.xml
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/join.xml
```

> 也可以把仓库开成 GitHub Pages，用 Pages 地址订阅（`chapters` 的 Content-Type 会更规范）。

## 单集格式

- **标题**：`标题 | 分类`
  - 分类来源：机核出品取机核分类（游戏茶话会、核电波、Gadio Pro…），入驻播客取节目名（如「菠萝油子丨成年人听的动漫播客」）。
  - 分类名自身带竖线时只截取左侧，例如「菠萝油子丨成年人听的动漫播客」→「菠萝油子」。
  - 标题里自带的竖线会被替换成「·」，保证「标题 | 分类」只有一个分隔符（可用 `normalize_title_bar: false` 关闭）。
- **频道图标**：由 `config.json` 里每个 feed 的 `icon` 指定（机核用站点图标，入驻用指定图片），不会再随某一集的封面变化。
- **Shownotes**：简介 + 本期参与者（`participants: true` 时，纯文本、不带头像）+ 正文 + 时间轴 + 跳回机核原始节目页的链接（如 https://www.gcores.com/radios/222328 ）。
- **精简版**（`gcores-not-detail.xml`）：`chapter_detail: false`，简介、参与者、正文都保留，只是时间轴里每条只显示「时间点 + 标题」，去掉每个时间点后面那段描述文字。
- **时间轴**：两种形式同时提供，保证「时间点可点击跳转」。
  1. Shownotes 里的纯文本时间戳（`00:12`、`1:02:03`），Apple Podcasts、Pocket Casts、AntennaPod 等会自动识别为可点击的跳转锚点；
  2. Podcasting 2.0 的 `<podcast:chapters>`，指向仓库里的 `chapters/<单集ID>.json`，支持章节的客户端（AntennaPod、Pocket Casts、Fountain 等）会直接显示章节列表并可跳转。
- **enclosure**：机核官方音频直链（`alioss.gcores.com/uploads/audio/...`）；入驻播客为其原始托管地址（喜马拉雅 / 小宇宙）。
- **去重**：按 `gcores-radio-<单集ID>` 作为 guid 去重，按发布时间降序排列，只保留最新 `max_items` 集。

## 配置（config.json）

```jsonc
{
  "max_items": 100,          // 每个 RSS 保留多少集
  "pages": 3,                // 抓列表的前几页（每页 100 集）
  "page_size": 100,
  "workers": 8,              // 并发抓时间轴的线程数
  "normalize_title_bar": true,
  "feeds": {
    "gcores": {
      "file": "gcores.xml",
      "filter": "gcores",                  // 机核出品
      "icon": "https://…/icon.png",        // 频道固定图标
      "participants": true,                // Shownotes 里列参与者
      "chapter_detail": true,              // 时间轴里是否输出每个时间点的描述文字
      "ignore": ["SPEC", "录音笔"],        // 忽略的关键字
      ...
    },
    "gcores-not-detail": {
      "file": "gcores-not-detail.xml",
      "filter": "gcores",
      "chapter_detail": false,             // 时间轴只要时间点 + 标题
      "participants": true,
      "state_from": "gcores",              // 复用 gcores 的缓存，不重复抓时间轴
      "ignore": ["SPEC", "录音笔"],
      ...
    },
    "join": {
      "file": "join.xml",
      "filter": "join_podcast",            // 入驻播客
      "icon": "https://…/icon.png",
      "ignore": ["诗梳风"],
      ...
    }
  }
}
```

**对外地址**：`<podcast:chapters>` 需要一个公网可访问的 URL，脚本按下面的优先级取值：

1. 环境变量 `CHAPTERS_BASE_URL` / `FEED_BASE_URL`（GitHub Actions 里已自动注入）；
2. `config.json` 里的 `chapters_base_url` / `feed_base_url`；
3. 由 `GITHUB_REPOSITORY` 推导出的 `https://raw.githubusercontent.com/<repo>/main`。

三者都没有（比如本地直接跑）时，章节 JSON 仍会写到 `chapters/`，但 RSS 里不会带 `<podcast:chapters>` 标签，Shownotes 的文本时间戳照常可用。

**忽略规则**：`ignore` 里的关键字会同时对「分类名 / 节目名」和「单集标题」做不区分大小写的**子串**匹配，命中即不写入 RSS。所以：

- `"SPEC"` 能过滤掉分类为 `Gadio Spec` 的节目；
- `"录音笔"` 能过滤掉标题里的 `录音笔VOL.829`（这类节目分类是「会员专享」）；
- `"诗梳风"` 能过滤掉入驻播客里节目名为「诗梳风」的整档节目。

两个 feed 的 `ignore` 互相独立，按需增删即可。

## 本地运行

```bash
python3 gcores_rss.py                  # 三个 feed 都生成
python3 gcores_rss.py --feed join      # 只生成入驻播客
```

只依赖 Python 3.10+ 标准库，无需安装任何第三方包。

首次运行会为最近 100 集抓时间轴（约 1 分钟）；之后靠 `cache/*.json` 增量更新，只抓新单集，通常十几秒。

如果这一轮抓到的内容和上一轮完全一致，脚本会跳过写文件（日志里显示「内容无变化」），所以 Actions 每小时跑一次也不会产生一堆空提交。

## GitHub Actions

`.github/workflows/rss.yml` 已配置好：

- 每小时整点（`0 * * * *`）自动运行，也可在 Actions 页面手动 `Run workflow`；
- 运行后自动 `commit + push` 更新三个 XML、`chapters/`、`cache/`；
- 内容无变化时不会产生空提交。

> 仓库的 `Settings → Actions → General → Workflow permissions` 需要允许 **Read and write permissions**，否则 push 会失败。

## 已知限制

- **会员专享音频跳过**：机核 GPASS 会员专享节目（如录音笔）的音频直链需要登录换取签名，播客客户端无法播放，脚本会直接跳过这类单集（默认 `ignore` 里也包含「录音笔」）。
- **入驻播客通常没有时间轴**：机核的入驻节目由主播自行上传，基本不写时间轴，因此 `join.xml` 多数单集没有章节。
- **时间轴上限**：机核接口单集最多返回 100 条时间轴，超过的部分拿不到。
- **章节文件托管**：`<podcast:chapters>` 需要公网可访问的 URL，所以默认用 `raw.githubusercontent.com`；如果仓库是 private，章节链接会 404，但 Shownotes 里的文本时间戳仍然可用。
