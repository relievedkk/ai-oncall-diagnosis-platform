"use strict";

const get = (id) => document.getElementById(id);
const state = { cases: [], selected: null, running: false };
const labels = {
  diagnosed: "诊断结论待复核", insufficient_evidence: "证据不足",
  budget_exhausted: "预算耗尽", dependency_failed: "依赖失败",
  no_active_alerts: "无活跃告警", legacy_unverified: "未经结构化校验",
};
const text = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
function append(parent, tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = text(value);
  if (className) element.className = className;
  parent.append(element);
  return element;
}
function selectCase(item) {
  if (state.running) return;
  state.selected = item;
  get("case-id").textContent = item.id;
  get("case-title").textContent = item.title || item.id;
  get("case-description").textContent = text(item.description || item.task || "合成工具观测，尚未人工复核。");
  get("result").hidden = true;
  get("run").disabled = false;
  get("run-status").textContent = "就绪 · 仅运行脚本重放，不调用真实模型";
  for (const button of get("cases").children) {
    button.setAttribute("aria-pressed", String(button.dataset.caseId === item.id));
  }
}
function renderResult(data) {
  const result = data.diagnosis_result || {};
  get("outcome").textContent = labels[result.outcome] || result.outcome || "结果待复核";
  get("summary").textContent = result.summary || "没有可用的诊断总结。";
  get("observations").replaceChildren();
  for (const item of result.observations || []) {
    append(get("observations"), "p", item.text);
    append(get("observations"), "code", (item.evidence_refs || []).join(" · ") || "无支持引用");
  }
  if (!(result.observations || []).length) append(get("observations"), "p", "无已收集观测。");
  get("hypotheses").replaceChildren();
  for (const item of result.hypotheses || []) {
    append(get("hypotheses"), "p", item.description);
    append(get("hypotheses"), "code", `状态：${item.status} | 支持：${(item.supporting_refs || []).join(", ") || "无"} | 反证：${(item.opposing_refs || []).join(", ") || "无"}`);
  }
  if (!(result.hypotheses || []).length) append(get("hypotheses"), "p", "没有已记录候选原因。");
  get("trace").replaceChildren();
  for (const [index, item] of (data.trace || []).entries()) {
    const row = append(get("trace"), "li", "");
    append(row, "span", String(index + 1).padStart(2, "0"), "step-number");
    const content = append(row, "div", "");
    append(content, "strong", item.tool || item.name || item.node || item.type || "运行步骤");
    append(content, "pre", item);
  }
  if (!(data.trace || []).length) append(get("trace"), "li", "该次运行未产生工具轨迹。");
  get("limitations").replaceChildren();
  for (const [label, values] of [
    ["缺失证据", result.missing_evidence], ["后续检查", result.next_checks],
    ["范围限制", result.limitations], ["校验问题", result.validation_issues],
  ]) for (const item of values || []) append(get("limitations"), "p", `${label}：${text(item)}`);
  append(get("limitations"), "p", "合成重放未测量语义准确率；脚本调用数与本机耗时不能代表真实模型性能。人工语义评分未完成时保持为空。");
  get("raw-result").textContent = JSON.stringify(data, null, 2);
  get("result").hidden = false;
}
async function run() {
  if (!state.selected || state.running) return;
  state.running = true;
  get("run").disabled = true;
  get("strategy").disabled = true;
  get("result").hidden = true;
  get("run-status").textContent = "正在重放 · 收集工具证据与诊断轨迹…";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  try {
    const query = new URLSearchParams({ case_id: state.selected.id, strategy: get("strategy").value });
    const response = await fetch(`/api/replay?${query}`, { signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    renderResult(data);
    get("run-status").textContent = "重放完成 · 合成来源 / 诊断结论需人工复核";
  } catch (error) {
    get("run-status").textContent = `重放未完成：${error.name === "AbortError" ? "本机请求超时，可稍后重试" : error.message}`;
  } finally {
    clearTimeout(timer);
    state.running = false;
    get("run").disabled = false;
    get("strategy").disabled = false;
  }
}
get("run").addEventListener("click", run);
async function init() {
  try {
    const response = await fetch("/api/cases");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.cases = data.cases;
    get("case-count").textContent = `${state.cases.length} 个合成案例`;
    for (const [index, item] of state.cases.entries()) {
      const button = append(get("cases"), "button", "", "case-button");
      button.type = "button";
      button.dataset.caseId = item.id;
      append(button, "span", String(index + 1).padStart(2, "0"), "case-number");
      const content = append(button, "span", "");
      append(content, "strong", item.title || item.id);
      append(content, "small", "SYNTHETIC · 待人工复核");
      button.addEventListener("click", () => selectCase(item));
    }
    if (state.cases.length) selectCase(state.cases[0]);
    else get("run-status").textContent = "没有可用的内置案例。";
  } catch (error) {
    get("run-status").textContent = `案例加载失败：${error.message}`;
  }
}
init();
