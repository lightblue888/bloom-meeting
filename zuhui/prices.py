# -*- coding: utf-8 -*-
"""各家官网的 API 标价（每百万 token），只用来估算一场会花了多少钱（zuhui/usage.py）。

- 按接口地址的域名认服务商（hosts），所以"OpenAI 兼容接口"填 DeepSeek 的地址也按 DeepSeek 算；
  这里没有的服务商、型号（本机模型、自己填的接口、新出的型号）只记 token、不算钱。
- 型号写接口认的原名；带日期后缀的（claude-haiku-4-5-20251001）按最长的前缀认。
- checked 是照着官网价格页（source）核对的那天；价格常变，改这里就行，改完从下一次调用起按新价算
  （已经记下的钱不重算：每次调用当时就按当时的价算好了）。
- 字段：in = 没命中缓存的输入，cache_read = 命中缓存的输入，cache_write = 写缓存，out = 输出（含思考）。
  没写 cache_read / cache_write 的，按服务商的 cache_read_ratio / cache_write_ratio 乘 in 算（默认 0.1 / 1.0）。
  tiers：按这一次请求的输入总长分档，upto 以内用这一档，最后一档不写 upto。
  peak / off：分时段计价，时段写在服务商的 peak 里（北京时间、周几、几点到几点）。
- 计价货币按服务商自己的：国内几家是人民币（CNY），Anthropic 是美元（USD）。
- Kimi、智谱 GLM 的官网价格页 2026-11-18 没能读到原文（页面是脚本渲染的，读到的是空表），先没列，用它们时只显示 token。
"""

PRICES = {
    "deepseek": {
        "name": "DeepSeek",
        "hosts": ["api.deepseek.com"],
        "currency": "CNY",
        "checked": "2026-11-18",
        "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
        # 高峰：北京时间周一到周五 9:00-12:00、14:00-18:00（官网说法定节假日除外，这里不认节假日，按工作日算）
        "peak": {"utc_offset": 8, "weekdays": [0, 1, 2, 3, 4], "hours": [[9, 12], [14, 18]]},
        "cache_write_ratio": 1.0,
        "models": {
            "deepseek-flash": {"peak": {"in": 2, "cache_read": 0.04, "out": 8},
                               "off": {"in": 1, "cache_read": 0.02, "out": 4}},
            "deepseek-v4-pro": {"peak": {"in": 9, "cache_read": 0.3, "out": 27},
                                "off": {"in": 4.5, "cache_read": 0.15, "out": 13.5}},
        },
    },
    "qwen": {
        "name": "通义千问",
        "hosts": ["dashscope.aliyuncs.com"],
        "currency": "CNY",
        "checked": "2026-11-18",
        "source": "https://help.aliyun.com/zh/model-studio/model-pricing",
        # 华北2（北京）地域的原价（限时折扣不算）。隐式缓存命中按输入单价的 20%、创建按 100%
        # （https://help.aliyun.com/zh/model-studio/context-cache，"通常为 20%"，个别型号不一样）
        "cache_read_ratio": 0.2,
        "cache_write_ratio": 1.0,
        "models": {
            "qwen4-max": {"in": 10, "out": 30},
            "qwen3.8-max": {"in": 12, "out": 36},
            "qwen3.7-max": {"in": 12, "out": 36},
            "qwen3.8-flash": {"in": 0.8, "out": 2.7},
            "qwen3.7-flash": {"tiers": [{"upto": 32_000, "in": 0.2, "out": 0.8}, {"upto": 256_000, "in": 0.6, "out": 2.4},
                                        {"in": 1.2, "out": 4.8}]},
            "qwen3.6-flash": {"tiers": [{"upto": 256_000, "in": 1.2, "out": 7.2}, {"in": 4.8, "out": 28.8}]},
            "qwen3.7-plus": {"tiers": [{"upto": 256_000, "in": 2, "out": 8}, {"in": 6, "out": 24}]},
            "qwen3.6-plus": {"tiers": [{"upto": 256_000, "in": 2, "out": 12}, {"in": 8, "out": 48}]},
        },
    },
    "anthropic": {
        "name": "Anthropic",
        "hosts": ["api.anthropic.com"],
        "currency": "USD",
        "checked": "2026-11-18",
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        # cache_write 是 5 分钟缓存的写入价
        "models": {
            "claude-opus-5-5": {"in": 4, "cache_write": 5, "cache_read": 0.2, "out": 20},
            "claude-sonnet-5-5": {"in": 2, "cache_write": 2.5, "cache_read": 0.1, "out": 10},
            "claude-haiku-5-5": {"tiers": [{"upto": 100_000, "in": 0.1, "cache_write": 0.125, "cache_read": 0.01, "out": 0.5},
                                           {"in": 0.5, "cache_write": 0.625, "cache_read": 0.05, "out": 2.5}]},
            "claude-opus-5": {"in": 5, "cache_write": 6.25, "cache_read": 0.5, "out": 25},
            "claude-sonnet-5": {"in": 2, "cache_write": 2.5, "cache_read": 0.2, "out": 10},
            "claude-opus-4-8": {"in": 5, "cache_write": 6.25, "cache_read": 0.5, "out": 25},
            "claude-opus-4-7": {"in": 5, "cache_write": 6.25, "cache_read": 0.5, "out": 25},
            "claude-opus-4-6": {"in": 5, "cache_write": 6.25, "cache_read": 0.5, "out": 25},
            "claude-opus-4-5": {"in": 5, "cache_write": 6.25, "cache_read": 0.5, "out": 25},
            "claude-sonnet-4-6": {"in": 3, "cache_write": 3.75, "cache_read": 0.3, "out": 15},
            "claude-sonnet-4-5": {"in": 3, "cache_write": 3.75, "cache_read": 0.3, "out": 15},
            "claude-haiku-4-5": {"in": 1, "cache_write": 1.25, "cache_read": 0.1, "out": 5},
        },
    },
}
