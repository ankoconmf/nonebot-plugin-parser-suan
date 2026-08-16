# nonebot-plugin-parser · 定制版

NoneBot2 链接分享解析插件(Alconna 版)。基于 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser) 的个人定制分支。

## 支持平台

- **视频 / 图文**: B站、抖音(含直播 / 回放 / 图集 / 实况图)、快手、微博、小红书、YouTube、TikTok、Twitter、AcFun、NGA
- **画师 / 商品 / 社区**: pixiv、Instagram、BOOTH、GoodSmile、HeyBox

## 相对上游的定制点

- 抖音默认**直连 web detail API**(带 `open.douyin.com` Origin/Referer 伪装),浏览器(DrissionPage)仅在 API 失败时兜底,并拦截页面真实发出的 detail 请求以过风控
- 新增 pixiv / Instagram / BOOTH / GoodSmile / HeyBox 解析器
- 新增抖音直播 / 回放房间解析(封面、主播、观看数)
- 解析卡片按杂志 / 画报风格定制

## 安装

```bash
pip install git+https://github.com/ankoconmf/nonebot-plugin-parser-suan.git
```

> 需要 Python 3.10+ 与 NoneBot2。

可选依赖:
- `htmlrender` — HTML 卡片渲染(`pip install "nonebot-plugin-parser[htmlrender]"` 或 `nonebot-plugin-htmlrender>=0.6.7`)
- `ytdlp` — YouTube/TikTok 下载(`yt-dlp`)
- `emosvg` — emoji SVG 渲染(需系统 cairo)

浏览器兜底(抖音 API 失败时)依赖 `DrissionPage`(已包含在必装依赖中),首次使用会自动下载浏览器内核。

## 使用

发送支持平台的(BV号 / 链接 / 小程序 / 卡片)即可自动解析。

其他命令:
- `bm BV号 <分集>` 下载B站音频
- `ym 链接` 下载油管音频
- `blogin` 扫码获取B站凭据

## 配置

环境变量前缀 `PARSER_`。常用项:

| 配置项 | 说明 |
| --- | --- |
| `parser_proxy` | 全局代理 |
| `parser_browser_path` | 浏览器可执行文件路径(DrissionPage 兜底用),默认自动探测 |
| `parser_headless` | 无头模式,默认 `true` |
| `parser_pixiv_ck` / `parser_pixiv_refresh_token` | pixiv 凭据 |
| `parser_instagram_rapidapi_key` / `parser_instagram_rapidapi_host` | Instagram RapidAPI 凭据 |
| `parser_bili_ck` / `parser_ytb_ck` / `parser_xhs_ck` | 各平台 cookie |
| `parser_render_type` | 渲染器:`common` / `default` / `htmlrender` |
| `parser_custom_font` / `parser_custom_font_weight` | 卡片自定义字体 |
| `parser_emoji_cdn` / `parser_emoji_style` | 卡片 emoji 渲染 |
| `parser_append_url` | 是否追加原始链接 |
| `parser_disabled_platforms` | 禁用的平台列表 |

## License

[MIT](LICENSE)
