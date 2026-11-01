# pxb7-spider

递归抓取 [pxb7.com](https://www.pxb7.com/)（螃蟹游戏服务网）的商品信息，输出 **JSONL**：一行一个商品，逐条追加写入，支持断点续接、按分类单/多选或全量抓取。

## 工作原理

站点有两道门槛，本项目都绕开了，但没有做任何 JS 解密/签名逆向：

1. **阿里云 WAF**：`api-pc.pxb7.com` 对 `curl`/`requests`/`httpx` 返回滑块验证页。项目用 Playwright 启动真实 Chromium，让浏览器持有 WAF Cookie 并代发请求。
2. **前端请求拦截器**：页面把 `window.fetch` 换成了带 `timestamp__xxx` 签名的包装函数，从 `page.evaluate` 调用它会永久挂起。项目改用**原生 `XMLHttpRequest`**（未被劫持），在页面上下文里直接 POST，拿到的就是明文 JSON。

也就是说：浏览器只当传输层，业务数据全部走接口，速度远快于逐页渲染 DOM。

主要接口（均从页面内调用）：

| 用途 | 接口 |
| --- | --- |
| 大类 | `POST /api/product/web/gameClass/selectAll` |
| 全部游戏 | `POST /api/bff/web/game/select/all` |
| 列表分页 | `POST /api/search/product/v2/selectSearchPageList` |
| 商品详情 | `POST /api/product/web/product/detailPost`（只需 `productId`） |

## 安装

```bash
cd /data1/pxb7-spider
uv venv --python 3.12 .venv
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv pip install -e .
```

默认使用系统安装的 Chrome（`--browser-channel chrome`）。没有 Chrome 时：

```bash
.venv/bin/python -m playwright install chromium   # 可用 PLAYWRIGHT_DOWNLOAD_HOST 换镜像
python -m pxb7 crawl --game 10161 --browser-channel ""
```

## 用法

```bash
# 查看分类（游戏），支持关键字过滤 / JSON 输出
python -m pxb7 games --keyword 星穹
python -m pxb7 games --json > data/games.json

# 抓单个游戏
python -m pxb7 crawl --game 10161

# 抓多个游戏（可重复 --game，或逗号分隔）
python -m pxb7 crawl --game 10161,10013 --game 10032

# 抓某个大类下的全部游戏（1=网络游戏，2=手机游戏）
python -m pxb7 crawl --class-id 2 --limit 500

# 全站（量大，建议先看下面的“规模”一节）
python -m pxb7 crawl --all

# 交互式选择：先按关键字过滤，再输入编号，支持单选/多选
python -m pxb7 crawl --select multi     # 1,3,5 或 2-6 或 a=全部
python -m pxb7 crawl --select single

# 查看断点进度
python -m pxb7 status
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--out` | JSONL 路径，默认 `data/products.jsonl` |
| `--state` | 断点库，默认 `data/state.db` |
| `--concurrency` | 并发标签页数，默认 4 |
| `--delay-min/--delay-max` | 每次请求之间的随机间隔，默认 0.1~0.3s |
| `--max-pages` | 每个游戏最多翻几页（调试用） |
| `--limit` | 本次最多入队多少商品（调试用） |
| `--restart` | 忽略断点从头抓 |
| `--retry-failed` | 重试之前失败的商品 |
| `--keep-raw-detail` | 记录里附带详情接口原始 JSON（体积大） |
| `--headed` | 有头模式；被 WAF 卡住时可人工过一次验证 |

## 输出格式

`data/products.jsonl`，每行一个 JSON 对象：

```json
{
  "product_id": "2481815672341322309",
  "url": "https://www.pxb7.com/product/2481815672341322309/1",
  "game_id": "10161",
  "game_name": "崩坏星穹铁道",
  "biz_prod": 1,
  "price": 55000,
  "price_yuan": 550.0,
  "ori_price": 55000,
  "deposit_amount": 0,
  "title": "【BEOBM1587】70级，女主，五星角色：...",
  "product_name": "【BEOBM1587】70级，女主，五星角色：...",
  "product_unique_no": "BEOBM1587",
  "status": 1,
  "trade_status": 0,
  "attrs": {"按服务器": "米哈游官服", "按邮箱": "邮箱未绑定", "按主角": "女主"},
  "attr_list": ["米哈游官服", "邮箱未绑定", "女主"],
  "important": [],
  "main_image": "https://public-image.pxb7.com/...",
  "image_count": 20,
  "images": ["https://public-image.pxb7.com/..."],
  "hot_count": 113,
  "collect_count": 0,
  "seller_remark": null,
  "max_guaranteed_amount": 359640,
  "create_time": "2026-11-01 16:46:03",
  "shelve_up_time": "2026-11-01 16:50:11",
  "crawled_at": "2026-11-01T16:53:48+08:00"
}
```

- `price` / `ori_price` / `max_guaranteed_amount` 单位均为**分**，`price_yuan` 是换算后的人民币金额。
- `attrs` 来自详情接口，是「属性名 → 取值」的扁平字典。

## 断点续接

- SQLite（`--state`）记录每个游戏的 `next_page`、已翻页数，以及每个商品的 `pending/done/failed` 状态。
- 中断后**重跑同一条命令**即可续接：已完成的商品不会重复请求，未完成的继续。
- 续接时会从上次页码**回退一页**重读列表（新品会插到列表前面，避免漏抓），已抓过的商品按 `product_id` 跳过。
- 极端情况（写完 JSONL 但没来得及提交断点）可能产生极少量重复行，按 `product_id` 去重即可。

## 规模提醒

站点共 1638 个游戏，热门游戏（如崩坏星穹铁道）就有上万件商品，`--all` 是百万级请求量。建议：

- 先用 `--limit` / `--max-pages` 小样本确认输出符合预期；
- 长时间抓取时保持默认限速，必要时调大 `--delay-max`；
- 用 `nohup`/tmux 之外的方式托管进程时，直接重跑命令即可续接。

## 目录结构

```
pxb7/
  browser.py   # Playwright 标签页池、页内 XHR、WAF 检测与重试
  api.py       # 接口封装
  store.py     # JSONL 追加写入 + SQLite 断点库
  crawler.py   # 生产者/消费者调度、记录组装
  picker.py    # 交互式分类单/多选
  cli.py       # 命令行入口
probes/        # 调研脚本（保留，可独立运行）
 01_waf_probe.py      # 验证 WAF 行为
 02_page_probe.py     # 观察页面发出的 XHR
 03_api_probe.py      # 页面内 XHR 调接口并落盘样例
 04_fetch_vs_xhr.py   # 复现 fetch 挂起 vs XHR 正常
```

## 已知限制

- 只抓 `bizProd=1`（账号交易）。其他业务类型可用 `--biz-prod` 传入，但列表接口的 `type` 参数可能需要相应调整。
- 站点接口若改版，需要同步修改 `pxb7/api.py`。
- WAF 持续拦截时会以退出码 2 结束；用 `--headed` 打开一次浏览器人工通过验证即可。
