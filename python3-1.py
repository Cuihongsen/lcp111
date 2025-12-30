#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import concurrent.futures as futures
import csv
import datetime as dt
import json
import os
import re
import subprocess
from typing import Any


# ---- 常用阈值（用于给出 GOOD/NI/POOR 评级；lab 指标，仅作回归对比） ----
THRESHOLDS = {
    "LCP":  {"good": 2500, "ni": 4000},   # ms
    "INP":  {"good": 200,  "ni": 500},    # ms (若能取到)
    "CLS":  {"good": 0.1,  "ni": 0.25},   # unitless
    "TBT":  {"good": 200,  "ni": 600},    # ms（经验阈值）
    "FCP":  {"good": 1800, "ni": 3000},   # ms（经验阈值）
    "TTFB": {"good": 800,  "ni": 1800},   # ms（经验阈值）
}


def grade(metric: str, value: float | None) -> str:
    if value is None:
        return "N/A"
    t = THRESHOLDS.get(metric)
    if not t:
        return "N/A"
    if value <= t["good"]:
        return "GOOD"
    if value <= t["ni"]:
        return "NI"
    return "POOR"


def median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs.sort()
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def percentile(xs: list[float], p: float) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs.sort()
    if len(xs) == 1:
        return xs[0]
    idx = (len(xs) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(xs) - 1)
    frac = idx - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def read_urls(urls_file: str | None, single_url: str | None) -> list[str]:
    if single_url:
        return [single_url.strip()]
    if not urls_file:
        raise ValueError("Provide --url or --urls-file")
    with open(urls_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]
    urls = []
    for s in lines:
        if not s or s.startswith("#"):
            continue
        urls.append(s)
    return urls


def sanitize_filename(url: str, unique_suffix: str = "") -> str:
    s = re.sub(r"^https?://", "", url)
    s = re.sub(r"[\/:?&=#]+", "_", s)
    s = s[:150]  # 留出空间给后缀
    if unique_suffix:
        s = f"{s}__{unique_suffix[:30]}"
    return s


def run_cmd(cmd: list[str], timeout_sec: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            text=True
        )
    except FileNotFoundError as exc:
        cmd_name = cmd[0] if cmd else "lighthouse"
        raise RuntimeError(
            f"Command not found: {cmd_name}. "
            "Please install Lighthouse (npm i -g lighthouse) or use --prefer-npx."
        ) from exc
    return p.returncode, p.stdout, p.stderr


def find_lighthouse_bin(prefer_npx: bool) -> list[str]:
    """
    返回 lighthouse 命令前缀：
    - prefer_npx=True -> ["npx", "lighthouse"]
    - 否则 -> ["lighthouse"]（要求全局装了）
    """
    if prefer_npx:
        return ["npx", "lighthouse"]
    return ["lighthouse"]


def audit_numeric(lhr: dict[str, Any], audit_id: str) -> float | None:
    a = (lhr.get("audits") or {}).get(audit_id)
    if not a:
        return None
    v = a.get("numericValue")
    return float(v) if isinstance(v, (int, float)) else None


def audit_items(lhr: dict[str, Any], audit_id: str) -> list[dict[str, Any]]:
    a = (lhr.get("audits") or {}).get(audit_id) or {}
    details = a.get("details") or {}
    items = details.get("items") or []
    if isinstance(items, list):
        return items
    return []


def extract_metrics(lhr: dict[str, Any]) -> dict[str, Any]:
    # core-ish
    lcp = audit_numeric(lhr, "largest-contentful-paint")
    cls = audit_numeric(lhr, "cumulative-layout-shift")
    tbt = audit_numeric(lhr, "total-blocking-time")
    fcp = audit_numeric(lhr, "first-contentful-paint")
    ttfb = audit_numeric(lhr, "server-response-time")

    # INP 有时是 interaction-to-next-paint，有时是 experimental-interaction-to-next-paint
    inp = audit_numeric(lhr, "interaction-to-next-paint")
    if inp is None:
        inp = audit_numeric(lhr, "experimental-interaction-to-next-paint")

    perf_score = None
    cats = lhr.get("categories") or {}
    perf = cats.get("performance") or {}
    score = perf.get("score")
    if isinstance(score, (int, float)):
        perf_score = float(score)

    # LCP element
    lcp_elem_items = audit_items(lhr, "largest-contentful-paint-element")
    lcp_element = lcp_elem_items[0] if lcp_elem_items else None
    lcp_element_info = None
    if isinstance(lcp_element, dict):
        lcp_element_info = {
            "selector": lcp_element.get("selector"),
            "nodeLabel": lcp_element.get("nodeLabel"),
            "snippet": lcp_element.get("snippet"),
            "url": lcp_element.get("url") or lcp_element.get("sourceURL") or lcp_element.get("requestUrl"),
        }

    # render blocking top
    rb_items = audit_items(lhr, "render-blocking-resources")
    rb_top = []
    for it in rb_items[:10]:
        if not isinstance(it, dict):
            continue
        rb_top.append({
            "url": it.get("url"),
            "resourceType": it.get("resourceType"),
            "wastedMs": it.get("wastedMs"),
            "totalBytes": it.get("totalBytes"),
        })

    # flags (用于“原因判断”)
    flags = {
        "lcpLazyLoaded": False,
        "needsPrioritizeLcpImage": False,
        "heavyBootup": False,
        "hasLongTasks": False,
        "heavyMainThread": False,
        "lotsUnusedJs": False,
    }

    lcp_lazy = (lhr.get("audits") or {}).get("lcp-lazy-loaded") or {}
    prioritize = (lhr.get("audits") or {}).get("prioritize-lcp-image") or {}
    bootup = (lhr.get("audits") or {}).get("bootup-time") or {}
    long_tasks = (lhr.get("audits") or {}).get("long-tasks") or {}
    mainthread = (lhr.get("audits") or {}).get("mainthread-work-breakdown") or {}
    unused_js = (lhr.get("audits") or {}).get("unused-javascript") or {}
    diagnostics = (lhr.get("audits") or {}).get("diagnostics") or {}
    third_party = (lhr.get("audits") or {}).get("third-party-summary") or {}

    audit_evidence = {
        "bootup-time": {
            "numericValue": bootup.get("numericValue"),
        },
        "long-tasks": {
            "items": (long_tasks.get("details") or {}).get("items"),
        },
        "mainthread-work-breakdown": {
            "numericValue": mainthread.get("numericValue"),
        },
        "unused-javascript": {
            "overallSavingsMs": (unused_js.get("details") or {}).get("overallSavingsMs"),
        },
        "render-blocking-resources": {
            "items": rb_items[:10],
        },
        "diagnostics": {
            "details": diagnostics.get("details"),
        },
        "third-party-summary": {
            "items": (third_party.get("details") or {}).get("items"),
        },
    }

    # 这些 audit 的 score=0 常表示“有问题”
    if lcp_lazy.get("score") == 0:
        flags["lcpLazyLoaded"] = True
    if prioritize.get("score") == 0:
        flags["needsPrioritizeLcpImage"] = True

    bootup_v = bootup.get("numericValue")
    if isinstance(bootup_v, (int, float)) and bootup_v > 2000:
        flags["heavyBootup"] = True

    lt_items = ((long_tasks.get("details") or {}).get("items") or [])
    if isinstance(lt_items, list) and len(lt_items) > 0:
        flags["hasLongTasks"] = True

    mt_v = mainthread.get("numericValue")
    if isinstance(mt_v, (int, float)) and mt_v > 4000:
        flags["heavyMainThread"] = True

    savings_ms = ((unused_js.get("details") or {}).get("overallSavingsMs"))
    if isinstance(savings_ms, (int, float)) and savings_ms > 500:
        flags["lotsUnusedJs"] = True

    return {
        "perfScore": perf_score,  # 0..1
        "lcp": lcp,
        "inp": inp,
        "cls": cls,
        "tbt": tbt,
        "fcp": fcp,
        "ttfb": ttfb,
        "lcpElement": lcp_element_info,
        "renderBlockingTop": rb_top,
        "flags": flags,
        "auditEvidence": audit_evidence,
    }


def build_lcp_reasons(m: dict[str, Any]) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    lcp = m.get("lcp")
    ttfb = m.get("ttfb")
    tbt = m.get("tbt")
    flags = m.get("flags") or {}

    if isinstance(ttfb, (int, float)) and ttfb > 1200:
        reasons.append({
            "level": "HIGH",
            "title": "TTFB 偏高（后端/网关/CDN 回源慢）",
            "detail": f"TTFB≈{ttfb/1000:.2f}s，先看 CDN 命中率/回源耗时/接口耗时/重定向链路。",
        })

    if flags.get("lcpLazyLoaded"):
        reasons.append({
            "level": "HIGH",
            "title": "LCP 元素被懒加载拖慢",
            "detail": "首屏最大元素不要 lazy（尤其是首屏大图/大模块）。",
        })

    if flags.get("needsPrioritizeLcpImage"):
        reasons.append({
            "level": "HIGH",
            "title": "LCP 图片未被优先加载（缺 preload / 优先级）",
            "detail": "对 LCP 图片做 preload / fetchpriority，提高首屏优先级，配合压缩裁剪与 CDN。",
        })

    rb = m.get("renderBlockingTop") or []
    if rb:
        rb_url = rb[0].get("url") or "未知资源"
        reasons.append({
            "level": "MED",
            "title": "存在渲染阻塞资源（CSS/同步 JS）",
            "detail": f"示例阻塞资源：{rb_url}",
        })

    if isinstance(tbt, (int, float)) and tbt > 600:
        reasons.append({
            "level": "MED",
            "title": "主线程阻塞（TBT 高）推迟渲染与 LCP",
            "detail": f"TBT≈{int(tbt)}ms，常见原因：bundle 大、初始化重、第三方脚本占用。",
        })

    if flags.get("heavyBootup"):
        reasons.append({
            "level": "MED",
            "title": "JS 启动/解析执行开销大（bootup-time 高）",
            "detail": "拆包、延迟非首屏代码、减少 polyfill/过度转译、第三方脚本延后。",
        })

    if flags.get("lotsUnusedJs"):
        reasons.append({
            "level": "LOW",
            "title": "未使用 JS 较多（可减包）",
            "detail": "减少首屏下载/解析量，间接改善 LCP/FCP/INP。",
        })

    if not reasons and isinstance(lcp, (int, float)) and lcp > 4000:
        reasons.append({
            "level": "MED",
            "title": "LCP 偏慢但未命中明确诊断项",
            "detail": "建议结合 Performance trace 看：LCP 资源请求、CSS 阻塞、长任务、图片解码绘制阶段。",
        })

    return reasons


def build_issue_list(m: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    flags = m.get("flags") or {}
    audit_evidence = m.get("auditEvidence") or {}
    render_blocking = m.get("renderBlockingTop") or []

    def add_issue(
        level: str,
        metric: str,
        title: str,
        detail: str,
        value: float | None,
        audit_id: str | None = None,
    ) -> None:
        issues.append({
            "level": level,
            "metric": metric,
            "title": title,
            "detail": detail,
            "value": value,
            "auditId": audit_id,
            "evidence": audit_evidence.get(audit_id) if audit_id else None,
        })

    lcp = m.get("lcp")
    if isinstance(lcp, (int, float)) and lcp > 4000:
        add_issue(
            "HIGH",
            "LCP",
            "LCP 偏慢",
            f"LCP≈{lcp/1000:.2f}s，需关注资源请求、渲染阻塞、解码绘制与主线程阻塞。",
            float(lcp),
            "largest-contentful-paint",
        )

    ttfb = m.get("ttfb")
    if isinstance(ttfb, (int, float)) and ttfb > 1200:
        add_issue(
            "HIGH",
            "TTFB",
            "TTFB 偏高（后端/网关/CDN 回源慢）",
            f"TTFB≈{ttfb/1000:.2f}s，先看 CDN 命中率/回源耗时/接口耗时/重定向链路。",
            float(ttfb),
            "server-response-time",
        )

    inp = m.get("inp")
    if isinstance(inp, (int, float)) and inp > 500:
        add_issue(
            "HIGH",
            "INP",
            "交互响应慢（INP 高）",
            f"INP≈{int(inp)}ms，排查长任务、主线程阻塞与第三方脚本。",
            float(inp),
            "interaction-to-next-paint",
        )

    cls = m.get("cls")
    if isinstance(cls, (int, float)) and cls > 0.25:
        add_issue(
            "MED",
            "CLS",
            "布局抖动明显（CLS 高）",
            f"CLS≈{cls:.3f}，检查图片/广告/懒加载占位、字体加载策略。",
            float(cls),
            "cumulative-layout-shift",
        )

    tbt = m.get("tbt")
    if isinstance(tbt, (int, float)) and tbt > 600:
        add_issue(
            "MED",
            "TBT",
            "主线程阻塞（TBT 高）",
            f"TBT≈{int(tbt)}ms，常见原因：bundle 大、初始化重、第三方脚本占用。",
            float(tbt),
            "total-blocking-time",
        )

    fcp = m.get("fcp")
    if isinstance(fcp, (int, float)) and fcp > 3000:
        add_issue(
            "MED",
            "FCP",
            "首次内容渲染慢（FCP 高）",
            f"FCP≈{fcp/1000:.2f}s，关注关键 CSS、首屏资源优先级与阻塞脚本。",
            float(fcp),
            "first-contentful-paint",
        )

    if flags.get("lcpLazyLoaded"):
        add_issue(
            "HIGH",
            "LCP",
            "LCP 元素被懒加载拖慢",
            "首屏最大元素不要 lazy（尤其是首屏大图/大模块）。",
            lcp if isinstance(lcp, (int, float)) else None,
            "lcp-lazy-loaded",
        )

    if flags.get("needsPrioritizeLcpImage"):
        add_issue(
            "HIGH",
            "LCP",
            "LCP 图片未被优先加载（缺 preload / 优先级）",
            "对 LCP 图片做 preload / fetchpriority，提高首屏优先级。",
            lcp if isinstance(lcp, (int, float)) else None,
            "prioritize-lcp-image",
        )

    if render_blocking:
        rb_url = render_blocking[0].get("url") or "未知资源"
        add_issue(
            "MED",
            "FCP",
            "存在渲染阻塞资源（CSS/同步 JS）",
            f"示例阻塞资源：{rb_url}",
            None,
            "render-blocking-resources",
        )

    if flags.get("heavyBootup"):
        add_issue(
            "MED",
            "TBT",
            "JS 启动/解析执行开销大（bootup-time 高）",
            "拆包、延迟非首屏代码、减少 polyfill/过度转译。",
            tbt if isinstance(tbt, (int, float)) else None,
            "bootup-time",
        )

    if flags.get("lotsUnusedJs"):
        add_issue(
            "LOW",
            "TBT",
            "未使用 JS 较多（可减包）",
            "减少首屏下载/解析量，间接改善 LCP/FCP/INP。",
            None,
            "unused-javascript",
        )

    level_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    issues.sort(key=lambda x: (level_order.get(x["level"], 99), -(x["value"] or 0)))
    return issues


def lighthouse_once(
    url: str,
    out_dir: str,
    device: str,
    timeout_sec: int,
    prefer_npx: bool,
    extra_chrome_flags: str,
    run_id: str = "",
) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    # 使用run_id避免重复运行时的文件覆盖
    unique_id = f"{device}"
    if run_id:
        unique_id = f"{device}__{run_id}"
    filename = sanitize_filename(url, unique_id) + ".lhr.json"
    lhr_path = os.path.join(out_dir, filename)

    cmd_prefix = find_lighthouse_bin(prefer_npx)
    cmd = cmd_prefix + [
        url,
        "--quiet",
        "--output=json",
        f"--output-path={lhr_path}",
        "--only-categories=performance",
        f"--form-factor={device}",
        # 让 Chrome headless 跑（你也可以去掉 headless，看可视化窗口）
        f'--chrome-flags={extra_chrome_flags}',
    ]

    rc, stdout, stderr = run_cmd(cmd, timeout_sec)
    if rc != 0:
        raise RuntimeError(f"Lighthouse failed (rc={rc}). stderr:\n{stderr.strip()}\nstdout:\n{stdout.strip()}")

    with open(lhr_path, "r", encoding="utf-8") as f:
        lhr = json.load(f)

    metrics = extract_metrics(lhr)
    reasons = build_lcp_reasons(metrics)
    issues = build_issue_list(metrics)

    return {
        "url": url,
        "device": device,
        "lhrPath": lhr_path,
        "metrics": metrics,
        "lcpReasons": reasons,
        "issues": issues,
    }


def run_url_repeats(
    url: str,
    repeats: int,
    out_dir: str,
    device: str,
    timeout_sec: int,
    prefer_npx: bool,
    extra_chrome_flags: str,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    errors: list[str] = []

    for i in range(repeats):
        try:
            # 使用序号作为run_id，避免重复运行时的文件覆盖
            run_id = f"run{i+1}" if repeats > 1 else ""
            r = lighthouse_once(url, out_dir, device, timeout_sec, prefer_npx, extra_chrome_flags, run_id=run_id)
            runs.append(r)
        except Exception as e:
            errors.append(str(e))

    if not runs:
        # 返回所有错误信息（最多显示前3个）
        error_msg = "; ".join(errors[:3])
        if len(errors) > 3:
            error_msg += f" ... (还有{len(errors)-3}个错误)"
        return {"url": url, "device": device, "error": error_msg or "unknown error", "allErrors": errors}

    # 多次取中位数（更稳）
    def collect(k: str) -> list[float]:
        xs = []
        for r in runs:
            v = r["metrics"].get(k)
            if isinstance(v, (int, float)):
                xs.append(float(v))
        return xs

    m = {
        "perfScore": median(collect("perfScore")),
        "lcp": median(collect("lcp")),
        "inp": median(collect("inp")),
        "cls": median(collect("cls")),
        "tbt": median(collect("tbt")),
        "fcp": median(collect("fcp")),
        "ttfb": median(collect("ttfb")),
        # 用第一条 run 的 LCP element / 阻塞资源做展示（通常相同）
        "lcpElement": runs[0]["metrics"].get("lcpElement"),
        "renderBlockingTop": runs[0]["metrics"].get("renderBlockingTop"),
        "flags": runs[0]["metrics"].get("flags"),
    }

    return {
        "url": url,
        "device": device,
        "repeats": repeats,
        "metrics": m,
        "grades": {
            "LCP": grade("LCP", m["lcp"]),
            "INP": grade("INP", m["inp"]),
            "CLS": grade("CLS", m["cls"]),
            "TBT": grade("TBT", m["tbt"]),
            "FCP": grade("FCP", m["fcp"]),
            "TTFB": grade("TTFB", m["ttfb"]),
        },
        "lcpReasons": build_lcp_reasons(m),
        "issues": build_issue_list(m),
        "sampleLhr": runs[0]["lhrPath"],
        "errors": errors,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if not r.get("error")]

    def collect(key: str) -> list[float]:
        xs = []
        for r in ok:
            v = (r.get("metrics") or {}).get(key)
            if isinstance(v, (int, float)):
                xs.append(float(v))
        return xs

    summary = {
        "count": len(results),
        "success": len(ok),
        "failed": len(results) - len(ok),
        "avg": {},
        "p75": {},
        "worst": {},
    }

    for k in ["lcp", "inp", "cls", "tbt", "fcp", "ttfb", "perfScore"]:
        arr = collect(k)
        if not arr:
            continue
        summary["avg"][k] = sum(arr) / len(arr)
        summary["p75"][k] = percentile(arr, 0.75)

    def top_worst(key: str, n: int = 5) -> list[tuple[str, float]]:
        rows = []
        for r in ok:
            v = (r.get("metrics") or {}).get(key)
            if isinstance(v, (int, float)):
                rows.append((r["url"], float(v)))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:n]

    for metric in ["lcp", "ttfb", "fcp", "tbt", "cls", "inp"]:
        summary["worst"][metric] = top_worst(metric)

    return summary


def fmt_ms(v: float | None) -> str:
    if v is None:
        return ""
    if v >= 1000:
        return f"{v/1000:.2f}s"
    return f"{int(round(v))}ms"


def main():
    ap = argparse.ArgumentParser(description="批量分析网站的LCP等性能指标")
    ap.add_argument("--urls-file", help="URL列表文件（txt格式，一行一个URL，支持#注释）")
    ap.add_argument("--url", help="单个URL（与--urls-file二选一）")
    ap.add_argument("--device", choices=["mobile", "desktop"], default="mobile", help="设备类型")
    ap.add_argument("--repeats", type=int, default=1, help="每个URL重复跑几次，取中位数（默认1次）")
    ap.add_argument("--concurrency", type=int, default=2, help="并发跑几个URL（默认2个）")
    ap.add_argument("--timeout-sec", type=int, default=180, help="单次跑 lighthouse 超时(秒，默认180)")
    ap.add_argument("--output", default="lcp_output", help="输出目录（默认lcp_output）")
    ap.add_argument("--prefer-npx", action="store_true", help="用 npx lighthouse 而不是全局 lighthouse")
    ap.add_argument("--chrome-flags", default="--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage", help="Chrome启动参数")
    args = ap.parse_args()
    
    # 参数验证
    if not args.urls_file and not args.url:
        ap.error("必须提供 --urls-file 或 --url 参数之一")
    if args.repeats < 1:
        ap.error("--repeats must be >= 1")
    if args.concurrency < 1:
        ap.error("--concurrency must be >= 1")

    urls = read_urls(args.urls_file, args.url)
    out_dir = os.path.abspath(args.output)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "lhr"), exist_ok=True)

    started = dt.datetime.now(dt.UTC).isoformat()
    print(f"Run Lighthouse: urls={len(urls)} device={args.device} repeats={args.repeats} concurrency={args.concurrency}")
    print(f"Output: {out_dir}")

    def worker(u: str) -> dict[str, Any]:
        return run_url_repeats(
            url=u,
            repeats=args.repeats,
            out_dir=os.path.join(out_dir, "lhr"),
            device=args.device,
            timeout_sec=args.timeout_sec,
            prefer_npx=args.prefer_npx,
            extra_chrome_flags=args.chrome_flags,
        )

    results: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        fut_map = {ex.submit(worker, u): u for u in urls}
        for fut in futures.as_completed(fut_map):
            u = fut_map[fut]
            try:
                r = fut.result()
                results.append(r)
                if r.get("error"):
                    print(f"[FAIL] {u}\n  {r['error']}\n")
                else:
                    m = r["metrics"]
                    issues = r.get("issues") or []
                    print(f"[OK] {u}")
                    print(f"  LCP={fmt_ms(m.get('lcp'))} ({r['grades']['LCP']})  "
                          f"INP={fmt_ms(m.get('inp'))} ({r['grades']['INP']})  "
                          f"CLS={m.get('cls') if m.get('cls') is not None else ''} ({r['grades']['CLS']})  "
                          f"TTFB={fmt_ms(m.get('ttfb'))} ({r['grades']['TTFB']})")
                    # 打印前2条问题（按严重性排序）
                    for issue in issues[:2]:
                        print(f"  - ({issue['level']}) {issue['title']}: {issue['detail']}")
                    print()
            except Exception as e:
                results.append({"url": u, "device": args.device, "error": str(e)})
                print(f"[FAIL] {u}\n  {e}\n")

    # 汇总
    summary = summarize(results)

    report = {
        "generatedAt": dt.datetime.now(dt.UTC).isoformat(),
        "startedAt": started,
        "args": vars(args),
        "summary": summary,
        "results": results,
    }

    # JSON
    json_path = os.path.join(out_dir, "report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # CSV - 简化的表格格式，每个URL一行，包含核心指标
    csv_path = os.path.join(out_dir, "report.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        # 核心指标列：URL, 性能分数, LCP, LCP评级, TTFB, TTFB评级, FCP, FCP评级, TBT, TBT评级, CLS, CLS评级, INP, INP评级, 主要问题
        w = csv.DictWriter(f, fieldnames=[
            "URL",
            "性能分数",
            "LCP",
            "LCP评级",
            "TTFB",
            "TTFB评级",
            "FCP",
            "FCP评级",
            "TBT",
            "TBT评级",
            "CLS",
            "CLS评级",
            "INP",
            "INP评级",
            "主要问题1",
            "主要问题2",
            "主要问题3",
            "错误信息",
        ])
        w.writeheader()

        for r in results:
            if r.get("error"):
                w.writerow({
                    "URL": r.get("url", ""),
                    "性能分数": "",
                    "LCP": "",
                    "LCP评级": "",
                    "TTFB": "",
                    "TTFB评级": "",
                    "FCP": "",
                    "FCP评级": "",
                    "TBT": "",
                    "TBT评级": "",
                    "CLS": "",
                    "CLS评级": "",
                    "INP": "",
                    "INP评级": "",
                    "主要问题1": "",
                    "主要问题2": "",
                    "主要问题3": "",
                    "错误信息": r.get("error", ""),
                })
                continue

            m = r.get("metrics") or {}
            issues = r.get("issues") or []
            top1 = issues[0]["title"] if len(issues) > 0 else ""
            top2 = issues[1]["title"] if len(issues) > 1 else ""
            top3 = issues[2]["title"] if len(issues) > 2 else ""

            score = m.get("perfScore")
            w.writerow({
                "URL": r.get("url", ""),
                "性能分数": "" if score is None else int(round(score * 100)),
                "LCP": fmt_ms(m.get("lcp")),
                "LCP评级": r["grades"].get("LCP", "N/A"),
                "TTFB": fmt_ms(m.get("ttfb")),
                "TTFB评级": r["grades"].get("TTFB", "N/A"),
                "FCP": fmt_ms(m.get("fcp")),
                "FCP评级": r["grades"].get("FCP", "N/A"),
                "TBT": fmt_ms(m.get("tbt")),
                "TBT评级": r["grades"].get("TBT", "N/A"),
                "CLS": "" if m.get("cls") is None else f"{m.get('cls'):.3f}",
                "CLS评级": r["grades"].get("CLS", "N/A"),
                "INP": fmt_ms(m.get("inp")),
                "INP评级": r["grades"].get("INP", "N/A"),
                "主要问题1": top1,
                "主要问题2": top2,
                "主要问题3": top3,
                "错误信息": "",
            })

    # 打印最差 Top5
    ok = [r for r in results if not r.get("error")]

    def top_worst(key: str, n: int = 5) -> list[tuple[str, float]]:
        rows = []
        for r in ok:
            v = (r.get("metrics") or {}).get(key)
            if isinstance(v, (int, float)):
                rows.append((r["url"], float(v)))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:n]

    print("=== Summary ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    for metric, label, formatter in [
        ("lcp", "LCP", lambda v: fmt_ms(v)),
        ("ttfb", "TTFB", lambda v: fmt_ms(v)),
        ("fcp", "FCP", lambda v: fmt_ms(v)),
        ("tbt", "TBT", lambda v: fmt_ms(v)),
        ("cls", "CLS", lambda v: f"{v:.3f}"),
        ("inp", "INP", lambda v: fmt_ms(v)),
    ]:
        print(f"\n=== Worst {label} Top5 ===")
        for u, v in top_worst(metric):
            print(f"{formatter(v)}  {u}")

    print(f"\n✅ 分析完成！")
    print(f"\n📊 报告文件：")
    print(f"  - 表格（CSV）: {csv_path}")
    print(f"  - 详细数据（JSON）: {json_path}")
    print(f"  - Lighthouse原始数据: {os.path.join(out_dir, 'lhr')}")
    print(f"\n💡 提示：可以直接用Excel或WPS打开 {csv_path} 查看表格")


if __name__ == "__main__":
    main()
