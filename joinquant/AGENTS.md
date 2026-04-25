# AGENTS

https://www.joinquant.com/algorithm/index/edit?algorithmId=7d0dd903f50139ebf7025417b71f49cb

模拟巨宽线上平台, 做到与线上平台的 API 完全一致.

strategys/ 下载文件修改必须是跟线上的聚合平台使用一模一样

Scope: whole repo.

Rules:
- Be concise.
- Prefer minimal diffs.
- Do not change strategy logic unless asked.
- Prefer fixing root cause over adding workarounds.
- Run Python with `.venv/bin/python`.
- Respect current data limits of `jqdatasdk`.
- For backtest issues, verify date range before changing code.
- Do not touch unrelated files.
