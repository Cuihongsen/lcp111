# Lighthouse 批量性能分析脚本

该脚本基于 Lighthouse 批量分析 URL 的性能指标（LCP/INP/CLS/TBT/FCP/TTFB），并输出问题诊断与汇总报告。

## 依赖

- Node.js + npm（用于运行 Lighthouse）
  - 全局安装：
    - `npm i -g lighthouse`
  - 或使用 `--prefer-npx` 走 `npx lighthouse`

## 安装依赖

### Node.js / Lighthouse

安装 Node.js 后，选择以下其一：

- 全局安装 Lighthouse：

```bash
npm i -g lighthouse
```

- 或使用 npx（无需全局安装）：

```bash
npx lighthouse --version
```

## 快速使用

1. 准备 URL 列表文件（每行一个 URL，支持 `#` 注释），例如 `urls.txt`。
2. 运行脚本：

```bash
node lcp.js --urls-file urls.txt --device mobile --repeats 1 --concurrency 2
```

或使用 npm 脚本：

```bash
npm start -- --urls-file urls.txt --device mobile --repeats 1 --concurrency 2
```

### 常用参数

- `--url`：单个 URL
- `--urls-file`：URL 列表文件
- `--device`：`mobile` 或 `desktop`
- `--repeats`：每个 URL 重复次数（取中位数）
- `--concurrency`：并发数
- `--prefer-npx`：使用 `npx lighthouse`
- `--output`：输出目录
- `--user-data-dir`：Chrome 用户数据目录（Windows 下可避免临时目录清理失败）

### 输出

- `report.json`：完整结构化数据
- `report.csv`：简化表格（含指标与主要问题）
- `lhr/`：Lighthouse 原始 JSON 报告
