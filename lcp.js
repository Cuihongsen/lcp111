#!/usr/bin/env node
/* eslint-disable no-console */
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawn } = require("child_process");

const THRESHOLDS = {
  LCP: { good: 2500, ni: 4000 },
  INP: { good: 200, ni: 500 },
  CLS: { good: 0.1, ni: 0.25 },
  TBT: { good: 200, ni: 600 },
  FCP: { good: 1800, ni: 3000 },
  TTFB: { good: 800, ni: 1800 },
};

function grade(metric, value) {
  if (value === null || value === undefined) return "N/A";
  const t = THRESHOLDS[metric];
  if (!t) return "N/A";
  if (value <= t.good) return "GOOD";
  if (value <= t.ni) return "NI";
  return "POOR";
}

function median(values) {
  const xs = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  if (xs.length % 2 === 1) return xs[mid];
  return (xs[mid - 1] + xs[mid]) / 2;
}

function percentile(values, p) {
  const xs = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!xs.length) return null;
  if (xs.length === 1) return xs[0];
  const idx = (xs.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.min(lo + 1, xs.length - 1);
  const frac = idx - lo;
  return xs[lo] + (xs[hi] - xs[lo]) * frac;
}

function readUrls(urlsFile, singleUrl) {
  if (singleUrl) return [singleUrl.trim()];
  if (!urlsFile) throw new Error("Provide --url or --urls-file");
  const lines = fs.readFileSync(urlsFile, "utf8").split(/\r?\n/);
  return lines.map((s) => s.trim()).filter((s) => s && !s.startsWith("#"));
}

function sanitizeFilename(url, uniqueSuffix = "") {
  let s = url.replace(/^https?:\/\//, "");
  s = s.replace(/[/:?&=#]+/g, "_");
  s = s.slice(0, 150);
  if (uniqueSuffix) s = `${s}__${uniqueSuffix.slice(0, 30)}`;
  return s;
}

function runCmd(command, args, timeoutSec) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      shell: process.platform === "win32",
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`Command timeout after ${timeoutSec}s`));
    }, timeoutSec * 1000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

function findLighthouseBin(preferNpx) {
  return preferNpx ? "npx" : "lighthouse";
}

function auditNumeric(lhr, auditId) {
  const audit = (lhr.audits || {})[auditId];
  if (!audit) return null;
  const v = audit.numericValue;
  return Number.isFinite(v) ? Number(v) : null;
}

function auditItems(lhr, auditId) {
  const audit = (lhr.audits || {})[auditId] || {};
  const details = audit.details || {};
  return Array.isArray(details.items) ? details.items : [];
}

function extractMetrics(lhr) {
  const lcp = auditNumeric(lhr, "largest-contentful-paint");
  const cls = auditNumeric(lhr, "cumulative-layout-shift");
  const tbt = auditNumeric(lhr, "total-blocking-time");
  const fcp = auditNumeric(lhr, "first-contentful-paint");
  const ttfb = auditNumeric(lhr, "server-response-time");
  let inp = auditNumeric(lhr, "interaction-to-next-paint");
  if (inp === null) inp = auditNumeric(lhr, "experimental-interaction-to-next-paint");

  const perfScore = Number.isFinite(lhr?.categories?.performance?.score)
    ? Number(lhr.categories.performance.score)
    : null;

  /* 1. LCP Element Information */
  // LCP Element audit structure is complex: details -> items (list) -> item (table) -> items (rows) -> item -> node
  const lcpAudit = (lhr.audits || {})["largest-contentful-paint-element"];
  let lcpNode = null;
  if (lcpAudit && lcpAudit.details && lcpAudit.details.items && lcpAudit.details.items.length) {
    const firstTable = lcpAudit.details.items[0];
    if (firstTable.items && firstTable.items.length && firstTable.items[0].node) {
      lcpNode = firstTable.items[0].node;
    }
  }

  const lcpElementInfo = lcpNode
    ? {
      selector: lcpNode.selector || "",
      nodeLabel: lcpNode.nodeLabel || "",
      snippet: lcpNode.snippet || "",
      url: lcpNode.url || lcpNode.sourceURL || lcpNode.requestUrl || "",
    }
    : null;

  /* 2. Top Blocking Resource */
  const rbItems = auditItems(lhr, "render-blocking-resources");
  const rbTop = rbItems
    .slice(0, 5)
    .map((it) => ({
      url: it.url,
      wastedMs: it.wastedMs,
    }))
    .sort((a, b) => b.wastedMs - a.wastedMs)[0]; // The single worst resource

  /* 3. INP Target Information */
  // INP is often found in the 'interaction-to-next-paint' audit details
  // The 'items' array usually contains the events. We look for the one with highest duration.
  // Note: Lighthouse structure for INP can vary, but usually it's in details.items
  let inpTarget = null;
  const inpItems = auditItems(lhr, "interaction-to-next-paint");
  // Find the item with the longest processing duration or total duration
  if (inpItems && inpItems.length > 0) {
    const worstInp = inpItems.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
    if (worstInp && worstInp.data) {
      // Sometimes the element info is in `data` (old LHR) or direct properties
      inpTarget = worstInp.data.selector || worstInp.data.nodeLabel;
    } else {
      // Newer Lighthouse versions might just have selector/nodeLabel on the item
      inpTarget = worstInp.selector || worstInp.nodeLabel;
    }
  }

  // Fallback: check experimental-interaction-to-next-paint if standard one is empty
  if (!inpTarget) {
    const expInpItems = auditItems(lhr, "experimental-interaction-to-next-paint");
    if (expInpItems && expInpItems.length > 0) {
      const worstExp = expInpItems.sort((a, b) => (b.duration || 0) - (a.duration || 0))[0];
      inpTarget = worstExp.selector || worstExp.nodeLabel;
    }
  }

  return {
    perfScore,
    lcp,
    inp,
    cls,
    tbt,
    fcp,
    ttfb,
    diagnostics: {
      lcpElement: lcpElementInfo,
      renderBlockingTop: rbTop,
      inpTarget: inpTarget || "",
    }
  };
}

function fmtMs(v) {
  if (!Number.isFinite(v)) return "";
  if (v >= 1000) return `${(v / 1000).toFixed(2)}s`;
  return `${Math.round(v)}ms`;
}

async function lighthouseOnce({
  url,
  outDir,
  device,
  timeoutSec,
  preferNpx,
  chromeFlags,
  userDataDir,
  runId,
}) {
  fs.mkdirSync(outDir, { recursive: true });
  const uniqueId = runId ? `${device}__${runId}` : device;
  const filename = `${sanitizeFilename(url, uniqueId)}.lhr.json`;
  const lhrPath = path.join(outDir, filename);

  let effectiveFlags = chromeFlags;
  if (userDataDir && !effectiveFlags.includes("--user-data-dir")) {
    effectiveFlags = `${effectiveFlags} --user-data-dir=${userDataDir}`;
  }

  const cmd = findLighthouseBin(preferNpx);
  const args = [
    url,
    "--quiet",
    "--output=json",
    `--output-path=${lhrPath}`,
    "--only-categories=performance",
    `--form-factor=${device}`,
    `--chrome-flags=${effectiveFlags}`,
  ];
  if (preferNpx) args.unshift("lighthouse");

  const { code, stdout, stderr } = await runCmd(cmd, args, timeoutSec);
  if (code !== 0) {
    throw new Error(`Lighthouse failed (rc=${code}). stderr:\n${stderr.trim()}\nstdout:\n${stdout.trim()}`);
  }

  const lhr = JSON.parse(fs.readFileSync(lhrPath, "utf8"));
  const metrics = extractMetrics(lhr);

  return {
    url,
    device,
    lhrPath,
    metrics,
  };
}

async function runUrlRepeats({
  url,
  repeats,
  outDir,
  device,
  timeoutSec,
  preferNpx,
  chromeFlags,
  userDataDir,
}) {
  const runs = [];
  const errors = [];
  for (let i = 0; i < repeats; i += 1) {
    try {
      const runId = repeats > 1 ? `run${i + 1}` : "";
      const result = await lighthouseOnce({
        url,
        outDir,
        device,
        timeoutSec,
        preferNpx,
        chromeFlags,
        userDataDir,
        runId,
      });
      runs.push(result);
    } catch (err) {
      errors.push(err.message);
    }
  }

  if (!runs.length) {
    const errMsg = errors.slice(0, 3).join("; ") || "unknown error";
    return { url, device, error: errMsg, allErrors: errors };
  }

  const collect = (k) => runs.map((r) => r.metrics[k]).filter((v) => Number.isFinite(v));
  const medianLcp = median(collect("lcp"));
  // Find representative run (closest to median LCP)
  let bestRun = runs[0];
  let minDiff = Infinity;
  for (const r of runs) {
    if (!Number.isFinite(r.metrics.lcp)) continue;
    const diff = Math.abs(r.metrics.lcp - medianLcp);
    if (diff < minDiff) {
      minDiff = diff;
      bestRun = r;
    }
  }

  const metrics = {
    perfScore: median(collect("perfScore")),
    lcp: medianLcp,
    inp: median(collect("inp")),
    cls: median(collect("cls")),
    tbt: median(collect("tbt")),
    fcp: median(collect("fcp")),
    ttfb: median(collect("ttfb")),
    // Use diagnostics from the representative run
    diagnostics: bestRun.metrics.diagnostics,
  };

  return {
    url,
    device,
    repeats,
    metrics,
    grades: {
      LCP: grade("LCP", metrics.lcp),
      INP: grade("INP", metrics.inp),
      CLS: grade("CLS", metrics.cls),
      TBT: grade("TBT", metrics.tbt),
      FCP: grade("FCP", metrics.fcp),
      TTFB: grade("TTFB", metrics.ttfb),
    },
    sampleLhr: runs[0].lhrPath,
    errors,
  };
}

function summarize(results) {
  const ok = results.filter((r) => !r.error);
  const collect = (k) => ok.map((r) => r.metrics[k]).filter((v) => Number.isFinite(v));
  const summary = {
    count: results.length,
    success: ok.length,
    failed: results.length - ok.length,
    avg: {},
    p75: {},
    worst: {},
  };

  for (const key of ["lcp", "inp", "cls", "tbt", "fcp", "ttfb", "perfScore"]) {
    const arr = collect(key);
    if (!arr.length) continue;
    summary.avg[key] = arr.reduce((a, b) => a + b, 0) / arr.length;
    summary.p75[key] = percentile(arr, 0.75);
  }

  const topWorst = (key, n = 5) => ok
    .map((r) => [r.url, r.metrics[key]])
    .filter(([, v]) => Number.isFinite(v))
    .sort((a, b) => b[1] - a[1])
    .slice(0, n);

  for (const metric of ["lcp", "ttfb", "fcp", "tbt", "cls", "inp"]) {
    summary.worst[metric] = topWorst(metric);
  }
  return summary;
}

function parseArgs(argv) {
  const args = {
    device: "mobile",
    repeats: 1,
    concurrency: 2,
    timeoutSec: 180,
    output: "lcp_output",
    preferNpx: false,
    chromeFlags: "--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage",
    userDataDir: "",
    urlsFile: "",
    url: "",
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--urls-file") args.urlsFile = argv[++i];
    else if (arg === "--url") args.url = argv[++i];
    else if (arg === "--device") args.device = argv[++i];
    else if (arg === "--repeats") args.repeats = Number(argv[++i]);
    else if (arg === "--concurrency") args.concurrency = Number(argv[++i]);
    else if (arg === "--timeout-sec") args.timeoutSec = Number(argv[++i]);
    else if (arg === "--output") args.output = argv[++i];
    else if (arg === "--prefer-npx") args.preferNpx = true;
    else if (arg === "--chrome-flags") args.chromeFlags = argv[++i];
    else if (arg === "--user-data-dir") args.userDataDir = argv[++i];
  }
  return args;
}

async function runPool(items, concurrency, worker) {
  const results = [];
  let index = 0;
  const active = new Set();

  const enqueue = async () => {
    if (index >= items.length) return;
    const item = items[index++];
    const p = worker(item)
      .then((res) => results.push(res))
      .finally(() => active.delete(p));
    active.add(p);
  };

  while (index < items.length || active.size) {
    while (active.size < concurrency && index < items.length) {
      await enqueue();
    }
    if (active.size) {
      await Promise.race(active);
    }
  }
  return results;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.urlsFile && !args.url) {
    throw new Error("必须提供 --urls-file 或 --url 参数之一");
  }
  if (!Number.isFinite(args.repeats) || args.repeats < 1) {
    throw new Error("--repeats must be >= 1");
  }
  if (!Number.isFinite(args.concurrency) || args.concurrency < 1) {
    throw new Error("--concurrency must be >= 1");
  }

  const urls = readUrls(args.urlsFile, args.url);
  const outDir = path.resolve(args.output);
  fs.mkdirSync(outDir, { recursive: true });
  fs.mkdirSync(path.join(outDir, "lhr"), { recursive: true });

  const userDataDir = args.userDataDir
    || (process.platform === "win32" ? path.join(outDir, "chrome-profile") : "");
  if (userDataDir) fs.mkdirSync(userDataDir, { recursive: true });

  console.log(
    `Run Lighthouse: urls=${urls.length} device=${args.device} repeats=${args.repeats} concurrency=${args.concurrency}`,
  );
  console.log(`Output: ${outDir}`);

  const results = await runPool(urls, args.concurrency, (u) => runUrlRepeats({
    url: u,
    repeats: args.repeats,
    outDir: path.join(outDir, "lhr"),
    device: args.device,
    timeoutSec: args.timeoutSec,
    preferNpx: args.preferNpx,
    chromeFlags: args.chromeFlags,
    userDataDir,
  }));

  for (const r of results) {
    if (r.error) {
      console.log(`[FAIL] ${r.url}\n  ${r.error}\n`);
      continue;
    }
    const m = r.metrics;
    console.log(`[OK] ${r.url}`);
    console.log(
      `  LCP=${fmtMs(m.lcp)} (${r.grades.LCP})  INP=${fmtMs(m.inp)} (${r.grades.INP})  CLS=${m.cls ?? ""
      } (${r.grades.CLS})  TTFB=${fmtMs(m.ttfb)} (${r.grades.TTFB})`,
    );
    console.log("");
  }

  const summary = summarize(results);
  const report = {
    generatedAt: new Date().toISOString(),
    args,
    summary,
    results,
  };

  const jsonPath = path.join(outDir, "report.json");
  fs.writeFileSync(jsonPath, JSON.stringify(report, null, 2), "utf8");

  const csvPath = path.join(outDir, "report.csv");
  const headers = [
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
    "LCP元素/来源",
    "最大阻塞资源",
    "INP交互元素",
    "错误信息",
  ];
  const rows = [headers.join(",")];
  for (const r of results) {
    if (r.error) {
      rows.push([
        r.url,
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        `"${r.error.replace(/"/g, '""')}"`,
      ].join(","));
      continue;
    }
    const m = r.metrics;
    const dia = m.diagnostics || {};
    const lcpDesc = dia.lcpElement ? (dia.lcpElement.selector || dia.lcpElement.url) : "";
    const rbDesc = dia.renderBlockingTop ? `${Math.round(dia.renderBlockingTop.wastedMs)}ms - ${dia.renderBlockingTop.url}` : "";
    const inpDesc = dia.inpTarget || "";

    const score = Number.isFinite(m.perfScore) ? Math.round(m.perfScore * 100) : "";
    rows.push([
      r.url,
      score,
      fmtMs(m.lcp),
      r.grades.LCP,
      fmtMs(m.ttfb),
      r.grades.TTFB,
      fmtMs(m.fcp),
      r.grades.FCP,
      fmtMs(m.tbt),
      r.grades.TBT,
      m.cls ?? "",
      r.grades.CLS,
      fmtMs(m.inp),
      r.grades.INP,
      // Diagnostics columns
      `"${(lcpDesc || "").replace(/"/g, '""')}"`,
      `"${(rbDesc || "").replace(/"/g, '""')}"`,
      `"${(inpDesc || "").replace(/"/g, '""')}"`,
      "",
    ].join(","));
  }
  fs.writeFileSync(csvPath, `${rows.join(os.EOL)}${os.EOL}`, "utf8");

  console.log("=== Summary ===");
  console.log(JSON.stringify(summary, null, 2));
  for (const [metric, label, formatter] of [
    ["lcp", "LCP", fmtMs],
    ["ttfb", "TTFB", fmtMs],
    ["fcp", "FCP", fmtMs],
    ["tbt", "TBT", fmtMs],
    ["cls", "CLS", (v) => (Number.isFinite(v) ? v.toFixed(3) : "")],
    ["inp", "INP", fmtMs],
  ]) {
    console.log(`\n=== Worst ${label} Top5 ===`);
    for (const [u, v] of summary.worst[metric] || []) {
      console.log(`${formatter(v)}  ${u}`);
    }
  }

  console.log("\n✅ 分析完成！");
  console.log("\n📊 报告文件：");
  console.log(`  - 表格（CSV）: ${csvPath}`);
  console.log(`  - 详细数据（JSON）: ${jsonPath}`);
  console.log(`  - Lighthouse原始数据: ${path.join(outDir, "lhr")}`);
  console.log(`\n💡 提示：可以直接用Excel或WPS打开 ${csvPath} 查看表格`);
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
