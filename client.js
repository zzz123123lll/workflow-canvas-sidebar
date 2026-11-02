window.__ModuleLoader__.load({
  id: "dsh-workflow-canvas-sidebar",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    var React = require("react");
    var h = React.createElement;
    var useState = React.useState;
    var useEffect = React.useEffect;
    var useRef = React.useRef;

    var PANEL_ID = "workflow-panel";
    var AI_ID = "dsh-workflow-canvas-sidebar/ai";
    var API = "/dsh-workflow/api";
    var NODE_W = 212;
    var NODE_H = 96;
    var C = {
      bg: "#080c12", panel: "#0d131b", card: "#111a26", cardHover: "#152031",
      border: "#22303f", borderSoft: "#1a2533", text: "#e8eef6", sub: "#8a98ab",
      faint: "#5d6b7e", accent: "#ffb020", teal: "#3dd6c4", danger: "#ff5d5d", grid: "#16202d",
    };
    var CAT = { "规格": "#3dd6c4", "脚本": "#5aa9ff", "制作": "#ffb020", "质检": "#b58cff", "审核": "#ff8a8a", "发布": "#4fd18b", "反馈": "#ff7ab6", "其他": "#8a98ab" };
    var ARTIFACT_STATUS = {
      verified: { label: "已验证", color: "#3dd6c4" },
      legacy: { label: "历史文件·证据待补", color: "#8a98ab" },
      changed: { label: "已变化", color: "#ffb020" },
      missing: { label: "文件缺失", color: "#ff5d5d" },
    };
    var ARTIFACT_KIND = { image: "图片", text: "文本", video: "视频", audio: "音频", pdf: "PDF", file: "文件" };
    function catColor(c) { return CAT[c] || CAT["其他"]; }
    function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

    function fetchJson(path, opts) {
      return fetch(API + path, opts).then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok || data.ok === false) { var error = new Error(data.error || ("HTTP " + r.status)); error.status = r.status; throw error; }
          return data;
        });
      });
    }
    function postJson(path, body) {
      return fetchJson(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    }
    function cloneGraph(g) { return JSON.parse(JSON.stringify(g)); }
    function edgeKey(e) { return e.from + "->" + e.to; }
    function bezierPath(a, b) {
      var px = a.x + NODE_W, py = a.y + NODE_H / 2;
      var qx = b.x, qy = b.y + NODE_H / 2;
      var dx = Math.max(70, Math.abs(qx - px) * 0.45);
      return "M " + px + " " + py + " C " + (px + dx) + " " + py + ", " + (qx - dx) + " " + qy + ", " + qx + " " + qy;
    }
    function statusColor(st) {
      if (st === "passed" || st === "completed" || st === "creation_ready") return C.teal;
      if (st === "running" || st === "pausing" || st === "paused" || st === "awaiting_input" || st === "awaiting_agent" || st === "awaiting_approval" || st === "current") return C.accent;
      if (st === "failed" || st === "blocked") return C.danger;
      return C.faint;
    }
    function statusLabel(status) {
      return { queued: '已受理，等待执行', running: "执行中", pausing: "正在暂停", paused: "已暂停", pending: "待执行", passed: "已完成", completed: "全部完成", skipped: "已跳过", awaiting_input: "等你回复", awaiting_agent: "等待处理", awaiting_approval: "等待审核", failed: "执行失败", blocked: "遇到问题", cancelled: "已停止", creation_ready: "阶段完成", stale: "结果已过期", not_run: "尚未执行" }[status] || "未开始";
    }
    var ACTIVE_RUN_STATUSES = ["running", "pausing", "paused", "awaiting_input", "awaiting_agent", "awaiting_approval", "stopping"];
    function runActive(status) { return ACTIVE_RUN_STATUSES.indexOf(status) !== -1; }

    function runFollowup(run) {
      if (!run || !run.run_id) return null;
      if (run.status === "completed" || run.status === "creation_ready") return { type: "results", runId: run.run_id };
      if (["awaiting_input", "awaiting_agent", "failed"].indexOf(run.status) === -1) return null;
      var node = ((run.nodes || []).filter(function (n) { return n.status === run.status; })[0]);
      return node ? { type: "workspace", runId: run.run_id, nodeId: node.id } : null;
    }
    function canContinueNode(node, readonly) {
      return !readonly && !!node && node.status === "failed";
    }
    function primaryArtifact(data) {
      var rows = data || {};
      return ((rows.primary || []).length ? rows.primary : (rows.artifacts || []).filter(function (a) { return a.is_primary; }))[0] || null;
    }
    function resultRunSelection(runs, requested) {
      return requested || (((runs || [])[0] || {}).run_id || "");
    }
    function isInlineTextArtifact(artifact) {
      return !!artifact && artifact.kind === "text" && artifact.status !== "missing";
    }
    function phaseLabel(phase, tool) {
      return { preparing: "准备技能与资料", starting: "启动执行器", connecting: "等待 DeepSeek 响应", model: "DeepSeek 会话执行中", tool: "正在调用工具" + (tool ? " · " + tool : ""), worker_finished: "DeepSeek 已返回，等待验收", validating: "检查实际产物", archiving: "保存与归档", waiting_input: "等待你的回复", completed: "产物已验收", failed: "执行遇到问题", cancelled: "执行已停止" }[phase] || "等待执行记录";
    }
    function executionModel(run, lastSync, error, currentTime) {
      var nodes = (run && run.nodes) || [], total = nodes.length;
      var complete = nodes.filter(function (n) { return n.status === "passed" || n.status === "skipped"; }).length;
      var skipped = nodes.filter(function (n) { return n.status === "skipped"; }).length;
      var connected = !!lastSync && !error && currentTime - lastSync < 8;
      var active = nodes.filter(function (n) { return n.status === "running"; });
      var fresh = active.filter(function (n) {
        var live = n.live || {}, supervisor = currentTime - Number(n.supervisor_heartbeat_at || 0) < 10;
        var worker = live.worker_alive && currentTime - Number(live.heartbeat_at || 0) < 10;
        return connected && supervisor && (["preparing", "validating", "archiving", "worker_finished"].indexOf(live.phase) !== -1 || worker);
      });
      var mode = !connected ? "unknown" : !run ? "idle" : run.status === "running" ? (fresh.length ? "running" : "unconfirmed") : run.status;
      return { mode: mode, connected: connected, total: total, complete: complete, skipped: skipped, percent: total ? Math.round(complete * 100 / total) : 0,
        active: active, freshIds: fresh.map(function (n) { return n.id; }),
        deepseekActive: fresh.some(function (n) { var l = n.live || {}; return l.worker_alive && l.event_count > 0 && ["model", "tool"].indexOf(l.phase) !== -1; }),
        title: mode === "unknown" ? (!lastSync && !error ? "正在连接执行服务" : "连接中断 · 执行状态待确认") : mode === "idle" ? "尚未运行" : mode === "unconfirmed" ? "等待执行状态确认" : statusLabel(mode) };
    }
    function graphMatchesRun(graph, run) {
      if (!run) return true;
      var actual = run.nodes || [], draft = graph.nodes || [];
      if (actual.length !== draft.length) return false;
      if (!actual.every(function (n) { return draft.some(function (d) { return d.id === n.id && (!n.module || d.module === n.module); }); })) return false;
      return !run.edges || JSON.stringify(run.edges.map(edgeKey).sort()) === JSON.stringify((graph.edges || []).map(edgeKey).sort());
    }
    function workspaceMessageLabel(m) {
      if (m.kind === "task") return "任务要求";
      if (m.role === "user") return "我";
      if (m.kind === "legacy_summary") return "助手 · 历史执行摘要";
      if (m.kind === "summary" || /summary$/.test(m.kind || "")) return "助手 · 执行摘要";
      if (m.kind === "question") return "助手 · 提问";
      return "助手 · 公开回复";
    }
    function workspaceSessionId(data) { return (data && (data.session_id || (data.node || {}).session_id)) || ""; }
    function messageTimeText(m) {
      var value = m.at || m.created_at || m.timestamp;
      if (!value) return "";
      if (typeof value === "number" && value < 100000000000) value *= 1000;
      var date = new Date(value);
      return isNaN(date.getTime()) ? "" : date.toLocaleString();
    }
    function knowledgeParseLabel(entry) {
      var labels = { parsed: "已解析", text: "可读取", stored: "仅存储", pending: "待解析", unsupported: "不支持文本提取", failed: "解析失败", image: "图片资料", preview_only: "可预览 · 未识别图片文字", no_text: "未提取到文字 · 扫描件需识别", too_large: "超过文本解析大小限制", empty: "文件没有可用正文" };
      return (labels[entry.parse_status] || "解析状态未确认") + (entry.usable ? " · 可用于执行" : "");
    }
    function readableDocument(text) {
      // React text children escape HTML; documents never become executable HTML.
      var lines = String(text || "").replace(/\r\n/g, "\n").split("\n"), blocks = [], i = 0;
      function inline(value) {
        return String(value).split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map(function (part, index) {
          if (/^\*\*/.test(part)) return h("strong", { key: index }, part.slice(2, -2));
          if (/^`/.test(part)) return h("code", { key: index, style: { background: C.card, padding: "1px 4px", borderRadius: 3 } }, part.slice(1, -1));
          return part;
        });
      }
      function cells(line) { return line.trim().replace(/^\||\|$/g, "").split("|").map(function (s) { return s.trim(); }); }
      while (i < lines.length) {
        var line = lines[i], key = "doc-" + i;
        if (!line.trim()) { i++; continue; }
        if (/^```/.test(line)) {
          var code = []; i++;
          while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
          i++; blocks.push(h("pre", { key: key, style: { padding: 12, background: C.card, overflow: "auto", borderRadius: 7, fontSize: 12 } }, code.join("\n"))); continue;
        }
        var heading = line.match(/^(#{1,6})\s+(.+)$/);
        if (heading) {
          blocks.push(h("h" + heading[1].length, { key: key, style: { fontSize: heading[1].length === 1 ? 23 : heading[1].length === 2 ? 18 : 15, lineHeight: 1.5, margin: "18px 0 10px", color: C.text } }, inline(heading[2]))); i++; continue;
        }
        if (line.indexOf("|") !== -1 && i + 1 < lines.length && /-/.test(lines[i+1]) && /^[\s|:-]+$/.test(lines[i+1])) {
          var header = cells(line), body = []; i += 2;
          while (i < lines.length && lines[i].trim() && lines[i].indexOf("|") !== -1) body.push(cells(lines[i++]));
          var cellStyle = { border: "1px solid " + C.border, padding: "8px 10px", verticalAlign: "top", textAlign: "left" };
          blocks.push(h("div", { key: key, style: { overflowX: "auto", margin: "12px 0" } }, h("table", { style: { width: "100%", borderCollapse: "collapse", fontSize: 13 } },
            h("thead", null, h("tr", null, header.map(function (v, n) { return h("th", { key: n, style: Object.assign({}, cellStyle, { background: C.card }) }, inline(v)); }))),
            h("tbody", null, body.map(function (row, n) { return h("tr", { key: n }, row.map(function (v, c) { return h("td", { key: c, style: cellStyle }, inline(v)); })); }))))); continue;
        }
        if (/^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
          var ordered = /^\s*\d+\./.test(line), items = [];
          while (i < lines.length && (ordered ? /^\s*\d+\.\s+/.test(lines[i]) : /^\s*[-*]\s+/.test(lines[i]))) items.push(lines[i++].replace(/^\s*(?:[-*]|\d+\.)\s+/, ""));
          blocks.push(h(ordered ? "ol" : "ul", { key: key, style: { paddingLeft: 24, margin: "10px 0" } }, items.map(function (v, n) { return h("li", { key: n, style: { marginBottom: 5 } }, inline(v)); }))); continue;
        }
        if (/^>\s?/.test(line)) { blocks.push(h("blockquote", { key: key, style: { margin: "12px 0", padding: "8px 12px", borderLeft: "3px solid " + C.teal, background: C.card } }, inline(line.replace(/^>\s?/, "")))); i++; continue; }
        if (/^\s*[-*_]{3,}\s*$/.test(line)) { blocks.push(h("hr", { key: key, style: { border: "none", borderTop: "1px solid " + C.border } })); i++; continue; }
        blocks.push(h("p", { key: key, style: { margin: "8px 0", whiteSpace: "pre-wrap" } }, inline(line))); i++;
      }
      return h("div", { "data-document-preview": "1", style: { color: C.text, lineHeight: 1.8, overflowWrap: "anywhere", fontSize: 14 } }, blocks);
    }
    function Icon() {
      return h("svg", { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" },
        h("rect", { x: 3, y: 3, width: 6, height: 6, rx: 1.5 }),
        h("rect", { x: 15, y: 15, width: 6, height: 6, rx: 1.5 }),
        h("path", { d: "M9 6h4a2 2 0 0 1 2 2v4" }),
        h("path", { d: "M6 9v4a2 2 0 0 0 2 2h4" })
      );
    }

    function AiPanel(props) {
      var _t = useState(""), text = _t[0], setText = _t[1];
      var _s = useState([]), sent = _s[0], setSent = _s[1];
      var _e = useState(""), err = _e[0], setErr = _e[1];

      function send() {
        var prompt = text.trim();
        if (!prompt) return;
        try {
          var ws = props.ctx && props.ctx.get ? props.ctx.get("uiWorkspace") : null;
          if (ws && typeof ws.startSession === "function") {
            ws.startSession(undefined, { agentPreset: "cordis", prompt: prompt, whenDraftExists: "append" });
            setSent(function (prev) { return prev.concat([prompt]).slice(-8); });
            setText("");
            setErr("");
            return;
          }
          setErr("当前 DSH 不支持从面板直接发送，请在主对话框粘贴指令。");
        } catch (e) {
          setErr("发送失败：" + e.message);
        }
      }

      function example(p) { setText(p); }

      return h("div", {
        style: { height: "100%", display: "flex", flexDirection: "column", background: C.panel, color: C.text, fontFamily: "'Noto Sans SC','Microsoft YaHei',sans-serif", padding: 12, boxSizing: "border-box" },
      },
        h("div", { style: { fontSize: 14, fontWeight: 800 } }, "AI 指令"),
        h("div", { style: { fontSize: 10.5, color: C.faint, marginTop: 5, lineHeight: 1.7 } },
          "告诉贾维斯要对画布做什么。发送后指令进入对话，贾维斯会调用画布 API 修改并自动刷新。"),
        h("div", { style: { marginTop: 10, display: "flex", flexWrap: "wrap", gap: 5 } },
          ["加一个发布检查模块", "把 QA 连到审核", "删除数据回填", "从渲染节点重跑"].map(function (x) {
            return h("button", {
              key: x, onClick: function () { example(x); },
              style: { padding: "4px 8px", borderRadius: 99, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" },
            }, x);
          })
        ),
        h("textarea", {
          value: text, onChange: function (e) { setText(e.target.value); },
          placeholder: "例如：在 QA 后面加一个「发布检查」模块，并接到审核前面",
          style: { flex: 1, marginTop: 10, minHeight: 120, padding: 9, borderRadius: 8, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 12, resize: "none", outline: "none", lineHeight: 1.6 },
        }),
        err ? h("div", { style: { color: C.danger, fontSize: 10.5, marginTop: 6 } }, err) : null,
        h("div", { style: { marginTop: 8, display: "flex", gap: 8 } },
          h("button", {
            onClick: send,
            style: { flex: 1, padding: "8px 0", borderRadius: 8, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontWeight: 700, cursor: "pointer", fontSize: 12 },
          }, "发送给贾维斯")
        ),
        sent.length ? h("div", { style: { marginTop: 12 } },
          h("div", { style: { fontSize: 10, color: C.faint, marginBottom: 5 } }, "已发送"),
          sent.map(function (x, i) { return h("div", { key: i, style: { fontSize: 10, color: C.sub, padding: "4px 7px", marginBottom: 4, borderRadius: 6, background: C.card, border: "1px solid " + C.borderSoft } }, x); })
        ) : null
      );
    }

    function requestId() { return window.crypto.randomUUID(); }
    function friendlyRunName(row) {
      row = row || {};
      var name = row.episode || row.name || '未命名任务';
      if (row.run_id && name.indexOf(row.run_id + '-') === 0) name = name.slice(row.run_id.length + 1);
      return name;
    }
    function friendlyArtifactName(row) {
      var name = String((row || {}).logical_path || (row || {}).path || '').split('/').pop();
      return name.replace(/^run-[a-f0-9]{32}-/, '');
    }

    function CanvasApp(props) {
      var _projects = useState([]), projects = _projects[0], setProjects = _projects[1];
      var _project = useState(null), project = _project[0], setProject = _project[1];
      var _projectOpen = useState(true), projectOpen = _projectOpen[0], setProjectOpen = _projectOpen[1];
      var _overview = useState(null), overview = _overview[0], setOverview = _overview[1];
      var projectRef = useRef(null), projectEpochRef = useRef(0);
      var _projectDraft = useState(null), projectDraft = _projectDraft[0], setProjectDraft = _projectDraft[1];
      var _workflowProposal = useState(null), workflowProposal = _workflowProposal[0], setWorkflowProposal = _workflowProposal[1];
      var _environment = useState(null), environment = _environment[0], setEnvironment = _environment[1];
      var _toolPaths = useState({}), toolPaths = _toolPaths[0], setToolPaths = _toolPaths[1];
      var projectImportRef = useRef(null), canvasRootRef = useRef(null);
      var _presentation = useState(false), presentation = _presentation[0], setPresentation = _presentation[1];

      var _g = useState({ nodes: [], edges: [] }), graph = _g[0], setGraph = _g[1];
      var _m = useState([]), modules = _m[0], setModules = _m[1];
      var _skills = useState([]), skillCatalog = _skills[0], setSkillCatalog = _skills[1];
      var _moduleForm = useState(null), moduleForm = _moduleForm[0], setModuleForm = _moduleForm[1];
      var _moduleError = useState(""), moduleError = _moduleError[0], setModuleError = _moduleError[1];
        var _moduleHist = useState(null), moduleHistory = _moduleHist[0], setModuleHistory = _moduleHist[1];
      var _v = useState({ x: 40, y: 30, k: 1 }), view = _v[0], setView = _v[1];
      var _sn = useState(null), selectedNode = _sn[0], setSelectedNode = _sn[1];
      var _se = useState(null), selectedEdge = _se[0], setSelectedEdge = _se[1];
      var _cf = useState(null), connectFrom = _cf[0], setConnectFrom = _cf[1];
      var _mw = useState({ x: 0, y: 0 }), mouseWorld = _mw[0], setMouseWorld = _mw[1];
      var _d = useState(false), dirty = _d[0], setDirty = _d[1];
      var _b = useState(false), busy = _b[0], setBusy = _b[1];
      var _msg = useState(""), msg = _msg[0], setMsg = _msg[1];
      var _run = useState(null), run = _run[0], setRun = _run[1];
      var _sr = useState(false), stopAtReview = _sr[0], setStopAtReview = _sr[1];
      var _q = useState(""), search = _q[0], setSearch = _q[1];
      var _hist = useState([]), history = _hist[0], setHistory = _hist[1];
      var _wfs = useState([]), workflows = _wfs[0], setWorkflows = _wfs[1];
      var _awf = useState({ id: "", name: "" }), activeWorkflow = _awf[0], setActiveWorkflow = _awf[1];
      var _wfo = useState(false), wfOpen = _wfo[0], setWfOpen = _wfo[1];
      var wfImportRef = useRef(null);
      var _logs = useState([]), logs = _logs[0], setLogs = _logs[1];
      var importRef = useRef(null);
      var _tpl = useState([]), templates = _tpl[0], setTemplates = _tpl[1];
      var _ai = useState(false), aiOpen = _ai[0], setAiOpen = _ai[1];
      var _aiText = useState(""), aiText = _aiText[0], setAiText = _aiText[1];
      var _aiLog = useState([]), aiLog = _aiLog[0], setAiLog = _aiLog[1];
      var _help = useState(null), helpModule = _help[0], setHelpModule = _help[1];
      var _guide = useState(false), guideOpen = _guide[0], setGuideOpen = _guide[1];
      var _kb = useState(false), kbOpen = _kb[0], setKbOpen = _kb[1];
      var _kbE = useState([]), kbEntries = _kbE[0], setKbEntries = _kbE[1];
      var _kbT = useState(""), kbTask = _kbT[0], setKbTask = _kbT[1];
      var _kbTs = useState([]), kbTasks = _kbTs[0], setKbTasks = _kbTs[1];
      var kbRequestRef = useRef(0), kbReadRef = useRef(0);
      var _kbC = useState(""), kbContent = _kbC[0], setKbContent = _kbC[1];
        var _kbImg = useState(null), kbImage = _kbImg[0], setKbImage = _kbImg[1];
      var _kbQ = useState(""), kbSearch = _kbQ[0], setKbSearch = _kbQ[1];
      var _kbPath = useState(""), kbPath = _kbPath[0], setKbPath = _kbPath[1];
      var _kbOriginal = useState(""), kbOriginal = _kbOriginal[0], setKbOriginal = _kbOriginal[1];
      var _kbEdit = useState(false), kbEdit = _kbEdit[0], setKbEdit = _kbEdit[1];
      var _kbEditText = useState(""), kbEditText = _kbEditText[0], setKbEditText = _kbEditText[1];
      var _kbMeta = useState(null), kbMeta = _kbMeta[0], setKbMeta = _kbMeta[1];
      var _taskO = useState(false), taskOpen = _taskO[0], setTaskOpen = _taskO[1];
      var _treeO = useState(false), treeOpen = _treeO[0], setTreeOpen = _treeO[1];
      var _linO = useState(false), lineageOpen = _linO[0], setLineageOpen = _linO[1];
      var _taskN = useState(""), taskNode = _taskN[0], setTaskNode = _taskN[1];
      var _taskC = useState(""), taskContent = _taskC[0], setTaskContent = _taskC[1];
      var _taskR = useState(""), taskRun = _taskR[0], setTaskRun = _taskR[1];
      var _taskI = useState(null), taskInfo = _taskI[0], setTaskInfo = _taskI[1];
      var _ws = useState(null), ws = _ws[0], setWs = _ws[1];
      var _wsErr = useState(""), wsErr = _wsErr[0], setWsErr = _wsErr[1];
      var _wsText = useState(""), wsText = _wsText[0], setWsText = _wsText[1];
      var _wsLoading = useState(false), wsLoading = _wsLoading[0], setWsLoading = _wsLoading[1];
      var _resultsOpen = useState(false), resultsOpen = _resultsOpen[0], setResultsOpen = _resultsOpen[1];
      var _results = useState(null), resultsData = _results[0], setResultsData = _results[1];
      var _deliveryDraft = useState(null), deliveryDraft = _deliveryDraft[0], setDeliveryDraft = _deliveryDraft[1];
      var _primaryText = useState(null), primaryText = _primaryText[0], setPrimaryText = _primaryText[1];
      var _resultsError = useState(""), resultsError = _resultsError[0], setResultsError = _resultsError[1];
      var _resultRun = useState(""), resultRun = _resultRun[0], setResultRun = _resultRun[1];
      var _resultNodes = useState([]), resultNodes = _resultNodes[0], setResultNodes = _resultNodes[1];
      var _viewRun = useState(""), viewingRun = _viewRun[0], setViewingRun = _viewRun[1];
      var selectedRunRef = useRef("");
      var resultsSeqRef = useRef(0);
      var catalogRetryRef = useRef(null);
      var resultCtxRef = useRef(null);
      var initializedRef = useRef(false);
      var workspaceMutationRef = useRef(null);
      var taskCtxRef = useRef(null);
      var logSeqRef = useRef(0);
      var wsSeqRef = useRef(0);
      var _arts = useState([]), artifactRows = _arts[0], setArtifactRows = _arts[1];
      var _artM = useState(null), artifactModal = _artM[0], setArtifactModal = _artM[1];
      var _artS = useState("idle"), artifactState = _artS[0], setArtifactState = _artS[1];
      var _artE = useState(""), artifactError = _artE[0], setArtifactError = _artE[1];
      var artifactSeqRef = useRef(0);
      var _histO = useState(false), histOpen = _histO[0], setHistOpen = _histO[1];
      var _histD = useState(null), histDetail = _histD[0], setHistDetail = _histD[1];
      var _tick = useState(0), tick = _tick[0], setTick = _tick[1];
      var runStartRef = useRef(0);
      var startingRef = useRef(false);
      var seenRunIdRef = useRef("");
      var dismissedRunRef = useRef("");
      var startTriesRef = useRef(0);
      var _rev = useState([]), revisions = _rev[0], setRevisions = _rev[1];
      var _crev = useState(""), canvasRevision = _crev[0], setCanvasRevision = _crev[1];
      var _sync = useState(0), runtimeSync = _sync[0], setRuntimeSync = _sync[1];
      var _runtimeError = useState(""), runtimeError = _runtimeError[0], setRuntimeError = _runtimeError[1];
      var _monitor = useState(false), monitorOpen = _monitor[0], setMonitorOpen = _monitor[1];
      var runtimePollRef = useRef(false);

      var _dlg = useState(null), dialog = _dlg[0], setDialog = _dlg[1];
      var viewportRef = useRef(null);
      var kbAttachRef = useRef(null);
      var dragRef = useRef(null);
      var dragMovedRef = useRef(false);
      var panRef = useRef(null);
      var undoRef = useRef([]);
      var redoRef = useRef([]);
      var stateRef = useRef({});
      var connectingRef = useRef(null);
      var actionsRef = useRef({});
      stateRef.current = { graph: graph, view: view, modules: modules, dirty: dirty, run: run, kbTask: kbTask, moduleForm: moduleForm, viewingRun: viewingRun };
      connectingRef.current = connectFrom;

      function projectPath(path) {
        var current = projectRef.current;
        return path + (current ? (path.indexOf('?') === -1 ? '?' : '&') + 'project_id=' + encodeURIComponent(current.project_id) : '');
      }
      function commandId() { return window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2); }
      function postRunCommand(path, body) {
        var current = projectRef.current;
        var expected = {};
        if (/\/(reply|revise|discuss|rerun|continue)$/.test(path)) {
          var source = ws && ws.run && ws.run.run_id === body.run_id ? ws.run : stateRef.current.run;
          if (source && source.run_id === body.run_id && source.state_revision !== undefined) expected.expected_state_revision = source.state_revision;
        }
        return postJson(path, Object.assign({ request_id: commandId() }, expected, body, current ? { project_id: current.project_id } : {}));
      }
      function refreshProjects() {
        fetchJson('/environment').then(function (r) { setEnvironment(r); setToolPaths(r.paths || {}); }).catch(function () {});
        return fetchJson('/projects').then(function (r) { setProjects(r.projects || []); return r.projects || []; });
      }
      function saveToolConfiguration() {
        setBusy(true);
        postJson('/environment', { paths: toolPaths }).then(function (r) {
          setEnvironment(r); setToolPaths(r.paths || {}); setMsg(r.video_ready ? '视频工具配置已就绪，可继续当前步骤。' : '配置已保存，请补齐仍缺少的工具。');
        }).catch(function (e) { setMsg(e.message); }).finally(function () { setBusy(false); });
      }
      function downloadJsonObject(value, filename) {
        var blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' });
        var link = document.createElement('a'); var objectUrl = URL.createObjectURL(blob);
        link.href = objectUrl; link.download = filename; link.click();
        setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 30000);
      }
      function exportProjectPackage(materials) {
        var current = projectRef.current, epoch = projectEpochRef.current;
        if (!current) return;
        fetchJson('/projects/export?project_id=' + encodeURIComponent(current.project_id) + '&materials=' + (materials ? '1' : '0'))
          .then(function (value) { if (epoch === projectEpochRef.current) downloadJsonObject(value, 'workflow-canvas-project.json'); })
          .catch(function (e) { setMsg(e.message); });
      }
      async function importProjectPackage(event) {
        var file = event.target.files && event.target.files[0]; event.target.value = '';
        if (!file) return;
        if (file.size > 30 * 1024 * 1024) { setMsg('项目包超过30MB，请分开分享资料。'); return; }
        setBusy(true);
        try {
          var data = JSON.parse(await file.text());
          var result = await postJson('/projects/import', { package: data, request_id: commandId() });
          await refreshProjects(); selectProject(result.project.project_id); setProjectOpen(true); setMsg(result.notice);
        } catch (e) { setMsg('项目导入未完成：' + e.message); }
        finally { setBusy(false); }
      }
      function downloadDiagnostics() {
        fetchJson('/diagnostics').then(function (value) { downloadJsonObject(value, 'workflow-canvas-diagnostics.json'); })
          .catch(function (e) { setMsg(e.message); });
      }
      function togglePresentation() {
        if (presentation) {
          if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(function () {});
          setPresentation(false); return;
        }
        setPresentation(true);
        var root = canvasRootRef.current;
        if (root && root.requestFullscreen) root.requestFullscreen().catch(function () {});
      }
      function rememberProjectView(value) {
        try {
          var current = projectRef.current;
          if (!current) return;
          var key = 'dsh-canvas-view:' + current.project_id;
          if (value) window.localStorage.setItem(key, JSON.stringify(value));
          else window.localStorage.removeItem(key);
        } catch (e) {}
      }
      function selectProject(id) {
        var epoch = ++projectEpochRef.current;
        projectRef.current = null;
        selectedRunRef.current = ''; setViewingRun('');
        if (typeof kbRequestRef !== "undefined" && kbRequestRef) ++kbRequestRef.current;
        taskCtxRef.current = null; resultCtxRef.current = null;
        ++wsSeqRef.current; ++resultsSeqRef.current; ++artifactSeqRef.current; ++logSeqRef.current;
        setTaskOpen(false); setResultsOpen(false); setArtifactModal(null); setHistOpen(false);
        setWorkflowProposal(null);
        setRun(null); setWs(null); setHistory([]); setOverview(null); setDirty(false); setProjectDraft(null);
        startingRef.current = false; initializedRef.current = true;
        setKbTask(''); setKbEntries([]); setKbOpen(false);
        if (!id) {
          setProject(null); window.localStorage.removeItem('dsh-canvas-project'); load(); return;
        }
        fetchJson('/projects/overview?project_id=' + encodeURIComponent(id)).then(function (r) {
          if (epoch !== projectEpochRef.current) return;
          projectRef.current = r.project; setProject(r.project); setOverview(r);
          setGraph(r.project.graph); setSelectedNode(null); setRun(r.current_run);
          setHistory((r.runs || []).slice().reverse()); setKbTask(id);
          window.localStorage.setItem('dsh-canvas-project', id);
          setMsg('已进入项目：' + r.project.name); setTimeout(fitView, 80);
          try {
            var savedView = JSON.parse(window.localStorage.getItem('dsh-canvas-view:' + id) || 'null');
            var savedRun = savedView && (r.runs || []).filter(function (row) { return row.run_id === savedView.runId; })[0];
            if (savedRun && savedView.type === 'workspace' && (savedRun.nodes || []).some(function (n) { return n.id === savedView.nodeId; })) {
              openWorkspace(savedView.runId, savedView.nodeId);
              if (savedView.generation) loadWorkspace(savedView.runId, savedView.nodeId, savedView.generation);
            } else if (savedRun && savedView.type === 'results') openResults(savedView.runId);
          } catch (e) {}
        }).catch(function (e) {
          if (epoch !== projectEpochRef.current) return;
          if (e.message === '项目不存在') {
            setProject(null); window.localStorage.removeItem('dsh-canvas-project');
            setProjectOpen(true); setMsg('上次项目不在当前数据目录中，请选择项目。');
          } else setMsg(e.message);
        });
      }
      function createProject() {
        setProjectDraft({ name: '', goal: '', delivery: '', start: 'blank', template: '' });
      }
      function editProject() {
        if (projectRef.current) setProjectDraft(Object.assign({}, projectRef.current));
      }
      async function saveProjectDraft() {
        var draft = projectDraft; if (!draft) return;
        var epoch = projectEpochRef.current;
        setBusy(true);
        try {
          var body = { request_id: requestId(), name: draft.name, goal: draft.goal, delivery: draft.delivery };
          if (draft.project_id) Object.assign(body, { project_id: draft.project_id, expected_revision: draft.revision });
          else if (draft.start === 'copy') body.graph = cloneGraph(stateRef.current.graph);
          else if (draft.start === 'template') {
            if (!draft.template) throw new Error('请选择一个模板');
            var templateData = await fetchJson('/workflows/export?id=' + encodeURIComponent(draft.template));
            body.graph = cloneGraph(templateData.workflow.graph);
          } else body.graph = { nodes: [], edges: [] };
          var r = await postJson('/projects/' + (draft.project_id ? 'update' : 'create'), body);
          await refreshProjects();
          if (epoch !== projectEpochRef.current) return;
          setProjectDraft(null); selectProject(r.project.project_id); setProjectOpen(true);
        } catch (e) { if (epoch === projectEpochRef.current) setMsg(e.message); }
        finally { setBusy(false); }
      }
      function suggestProjectWorkflow() {
        var current = projectRef.current, epoch = projectEpochRef.current;
        if (!current || busy) return;
        setBusy(true);
        postJson('/projects/suggest', { project_id: current.project_id, expected_revision: current.revision, request_id: commandId() })
          .then(function (r) {
            if (epoch !== projectEpochRef.current) return;
            setMsg('流程建议已受理；完成后在项目总览查看并确认，项目画布尚未改变。');
            openWorkspace(r.run_id, 'workflow_proposal'); setProjectOpen(false);
          }).catch(function (e) { if (epoch === projectEpochRef.current) setMsg(e.message); })
          .finally(function () { setBusy(false); });
      }
      function viewWorkflowProposal(runId) {
        var current = projectRef.current, epoch = projectEpochRef.current;
        if (!current) return;
        fetchJson('/projects/proposal?project_id=' + encodeURIComponent(current.project_id) + '&run_id=' + encodeURIComponent(runId))
          .then(function (r) { if (epoch === projectEpochRef.current) setWorkflowProposal(r); })
          .catch(function (e) { if (epoch === projectEpochRef.current) setMsg(e.message); });
      }
      function applyWorkflowProposal() {
        var current = projectRef.current, proposal = workflowProposal, epoch = projectEpochRef.current;
        if (!current || !proposal || !proposal.can_apply || busy) return;
        setBusy(true);
        postJson('/projects/apply-proposal', { project_id: current.project_id, run_id: proposal.run_id,
          sha256: proposal.sha256, expected_revision: current.revision, request_id: commandId() })
          .then(function () { if (epoch === projectEpochRef.current) { selectProject(current.project_id); setProjectOpen(false); setMsg('建议已写入项目画布；可编辑步骤、输入和交付要求，再点击运行。'); } })
          .catch(function (e) { if (epoch === projectEpochRef.current) setMsg(e.message); })
          .finally(function () { setBusy(false); });
      }
      function archiveProject() {
        var current = projectRef.current, epoch = projectEpochRef.current;
        if (!current || current.archived || busy) return;
        postJson('/projects/archive', { project_id: current.project_id, expected_revision: current.revision, request_id: commandId() })
          .then(function () { if (epoch === projectEpochRef.current) { selectProject(current.project_id); refreshProjects(); } })
          .catch(function (e) { if (epoch === projectEpochRef.current) setMsg(e.message); });
      }
      function chooseProjectRun(operation, runId, extra) {
        var current = projectRef.current, epoch = projectEpochRef.current;
        if (!current || busy) return;
        setBusy(true);
        postJson('/projects/' + operation, Object.assign({ project_id: current.project_id,
          expected_revision: current.revision, expected_work_revision: current.work_revision || 0, request_id: commandId(), run_id: runId }, extra || {}))
          .then(function () { if (epoch === projectEpochRef.current) { selectProject(current.project_id); setProjectOpen(true); } })
          .catch(function (e) { if (epoch === projectEpochRef.current) setMsg(e.message); })
          .finally(function () { setBusy(false); });
      }
      function openProjects() {
        setProjectOpen(true); refreshProjects().catch(function (e) { setMsg(e.message); });
        var current = projectRef.current, epoch = projectEpochRef.current;
        if (current) fetchJson('/projects/overview?project_id=' + encodeURIComponent(current.project_id)).then(function (r) {
          if (epoch === projectEpochRef.current) setOverview(r);
        }).catch(function (e) { setMsg(e.message); });
      }
      function load() {
        var epoch = projectEpochRef.current;

        fetchJson("/skills").then(function (r) { setSkillCatalog(r.skills || []); }).catch(function () {});
        Promise.all([fetchJson("/canvas"), fetchJson("/modules"), fetchJson("/workflows")])
          .then(function (rs) {
            setModules(rs[1].modules || []);
            setWorkflows(rs[2].workflows || []);
            if (epoch !== projectEpochRef.current || projectRef.current) return;
            var g0 = rs[0].graph || { nodes: [], edges: [] };
            setGraph(g0);
            setCanvasRevision(rs[0].revision || "");
            setWorkflows(rs[2].workflows || []);
            var activeMeta = (rs[2].workflows || []).filter(function (w) { return w.id === rs[2].active; })[0];
            setActiveWorkflow({ id: rs[2].active || "", name: (activeMeta && activeMeta.name) || rs[2].active || "工作流" });
            setModules(rs[1].modules || []);
            if (g0.nodes && g0.nodes.length) setSelectedNode(g0.nodes[0].id);
            setDirty(false);
            setTimeout(fitView, 80);
          }).catch(function () {
            if (epoch !== projectEpochRef.current) return;
            setMsg('服务暂未连接，正在恢复模块与流程目录…');
            clearTimeout(catalogRetryRef.current);
            catalogRetryRef.current = setTimeout(load, 2500);
          });
        fetchJson(projectPath("/wf/history")).then(function (r) { if (epoch === projectEpochRef.current) setHistory(r.runs || []); }).catch(function () {});
        fetchJson("/canvas/templates").then(function (r) { setTemplates(r.templates || []); }).catch(function () {});
        fetchJson("/canvas/revisions").then(function (r) { setRevisions(r.revisions || []); }).catch(function () {});
      }

      useEffect(function () {
        load();
        refreshProjects().then(function () {
          var id = window.localStorage.getItem('dsh-canvas-project');
          if (id) selectProject(id);
        }).catch(function (e) { setMsg(e.message); });
        pollRuntime();
        var runtimeTimer = setInterval(pollRuntime, 1500);
        var clockTimer = setInterval(function () { setTick(function (x) { return x + 1; }); }, 1000);
        var t = setInterval(function () {
          if (!projectRef.current && !selectedRunRef.current && !dragRef.current && !panRef.current && !stateRef.current.dirty) {
            fetchJson("/canvas").then(function (r) {
              if (!projectRef.current && !stateRef.current.dirty && !dragRef.current) {
                setGraph(r.graph || { nodes: [], edges: [] });
                setCanvasRevision(r.revision || "");
              }
            }).catch(function () {});
          }
        }, 6000);
        return function () { clearInterval(t); clearInterval(runtimeTimer); clearInterval(clockTimer); clearTimeout(catalogRetryRef.current); };
      }, []);

      useEffect(function () {
        if (!selectedNode) { setLogs([]); return; }
        var ctx = taskCtxRef.current;
        var useTask = !!(taskOpen && ctx && ctx.nodeId === selectedNode);
        var rid = useTask ? ctx.runId : ((stateRef.current.run && stateRef.current.run.run_id) || "");
        if (!rid) { setLogs([]); return; }
        var seq = ++logSeqRef.current;
        fetchJson("/wf/logs?run_id=" + encodeURIComponent(rid) + "&node=" + encodeURIComponent(selectedNode))
          .then(function (r) { if (seq === logSeqRef.current) setLogs((r.events || []).slice(-6)); })
          .catch(function () { if (seq === logSeqRef.current) setLogs([]); });
      }, [selectedNode, graph, taskOpen, taskNode]);

      useEffect(function () {
        if (!taskOpen || !taskRun || !taskNode) return;
        // Commands are queued before their child process updates state. Keep
        // following the latest version even when that first response is passed.
        if (ws && ws.readonly) return;
        var timer = setInterval(function () {
          var ctx = taskCtxRef.current;
          if (ctx) loadWorkspace(ctx.runId, ctx.nodeId);
        }, 2500);
        return function () { clearInterval(timer); };
      }, [taskOpen, taskNode, taskRun, ws && ws.readonly]);

      useEffect(function () {
        try {
          if (!window.localStorage.getItem("dsh-canvas-guide-seen")) setGuideOpen(true);
        } catch (e) {}
      }, []);

      useEffect(function () {
        function onKey(e) {
          if (stateRef.current.moduleForm) return;
          var tag = (e.target && e.target.tagName) || "";
          if (tag === "INPUT" || tag === "TEXTAREA") return;
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (actionsRef.current.save) actionsRef.current.save().catch(function () {}); }
          else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); (e.shiftKey ? actionsRef.current.redo : actionsRef.current.undo) && (e.shiftKey ? actionsRef.current.redo() : actionsRef.current.undo()); }
          else if (e.key === "Delete" || e.key === "Backspace") actionsRef.current.deleteSelected && actionsRef.current.deleteSelected();
          else if (e.key === "Escape") { setConnectFrom(null); setSelectedEdge(null); setSelectedNode(null); }
        }
        window.addEventListener("keydown", onKey);
        return function () { window.removeEventListener("keydown", onKey); };
      }, []);

      useEffect(function () {
        var el = viewportRef.current;
        if (!el) return;
        function onWheel(e) {
          e.preventDefault();
          var rect = el.getBoundingClientRect();
          var mx = e.clientX - rect.left, my = e.clientY - rect.top;
          var v = stateRef.current.view;
          var wx = (mx - v.x) / v.k, wy = (my - v.y) / v.k;
          var nk = clamp(v.k * (e.deltaY < 0 ? 1.12 : 0.89), 0.35, 2.2);
          setView({ k: nk, x: mx - wx * nk, y: my - wy * nk });
        }
        el.addEventListener("wheel", onWheel, { passive: false });
        return function () { el.removeEventListener("wheel", onWheel); };
      }, []);

      function pollRuntime() {
        if (runtimePollRef.current) return;
        runtimePollRef.current = true;
        var controller = new AbortController();
        var timeout = setTimeout(function () { controller.abort(); }, 5000);
        var epoch = projectEpochRef.current;
        var selectedId = selectedRunRef.current;
        var endpoint = selectedId ? "/wf/run-state?run_id=" + encodeURIComponent(selectedId) : "/wf/status";
        fetchJson(projectPath(endpoint), { signal: controller.signal }).then(function (r) {
          if (epoch !== projectEpochRef.current || selectedId !== selectedRunRef.current) return;
          setRuntimeSync(Date.now() / 1000); setRuntimeError("");
          if (startingRef.current) {
            if (!r.run || r.run.run_id === seenRunIdRef.current) {
              startTriesRef.current += 1;
              if (startTriesRef.current <= 20) return;
              setMsg("没有确认到新的执行，请查看执行监控后重试");
            }
            startingRef.current = false;
          }
          var nextRun = r.run || null;
          if (nextRun && dismissedRunRef.current && nextRun.run_id === dismissedRunRef.current) nextRun = null;
          else if (nextRun && dismissedRunRef.current) dismissedRunRef.current = "";
          seenRunIdRef.current = (nextRun && nextRun.run_id) || "";
          setRun(nextRun);
          if (!initializedRef.current && nextRun) { initializedRef.current = true; if (!runFollowup(nextRun)) openResults(nextRun.run_id); }
          if (r.run) setMsg(function (previous) { return /已提交运行|已收到回复/.test(String(previous)) ? "" : previous; });
        }).catch(function () { setRuntimeError("无法连接执行服务"); })
          .finally(function () { clearTimeout(timeout); runtimePollRef.current = false; });
      }

      useEffect(function () {
        if (run && run.run_id && run.status === "running") setMonitorOpen(true);
      }, [run && run.run_id]);

      useEffect(function () {
        if (taskCtxRef.current || resultCtxRef.current) return;
        var action = runFollowup(run);
        if (!action) return;
        if (action.type === "workspace") openWorkspace(action.runId, action.nodeId);
        if (action.type === "results") openResults(action.runId);
      }, [run && run.run_id, run && run.status]);

      useEffect(function () {
        if (run && run.task_id) setKbTask(run.task_id);
      }, [run && run.task_id]);

      useEffect(function () {
        if (kbOpen) loadKbEntries(kbTask, "");
      }, [kbOpen, kbTask]);

      function nodeById(id) {
        var ns = stateRef.current.graph.nodes || [];
        for (var i = 0; i < ns.length; i++) if (ns[i].id === id) return ns[i];
        return null;
      }
      function moduleById(id) {
        var ms = stateRef.current.modules || [];
        for (var i = 0; i < ms.length; i++) if (ms[i].id === id) return ms[i];
        return null;
      }
      function declaredList(graph, nodeId, field) {
        var nodes = (graph && graph.nodes) || [];
        var node = nodes.filter(function (n) { return n.id === nodeId; })[0];
        if (!node) return [];
        if (Array.isArray(node[field])) return node[field];
        var module = moduleById(node.module) || {};
        return Array.isArray(module[field]) ? module[field] : [];
      }
      function patternKey(value) {
        return String(value == null ? "" : value).replace(/\\/g, "/").trim()
          .replace(/\{stem\}/g, "<stem>").replace(/\{run_id\}/g, "<run>");
      }
      function ancestorsOf(graph, nodeId) {
        var incoming = {};
        ((graph && graph.edges) || []).forEach(function (e) { (incoming[e.to] = incoming[e.to] || []).push(e.from); });
        var seen = {}, stack = (incoming[nodeId] || []).slice();
        while (stack.length) {
          var current = stack.pop();
          if (seen[current]) continue;
          seen[current] = true;
          (incoming[current] || []).forEach(function (parent) { stack.push(parent); });
        }
        return Object.keys(seen);
      }
      function missingInputs(graph, nodeId) {
        var upstream = [];
        ancestorsOf(graph, nodeId).forEach(function (id) {
          declaredList(graph, id, "artifacts").forEach(function (value) { upstream.push(patternKey(value)); });
        });
        return declaredList(graph, nodeId, "requires").map(patternKey).filter(function (value) {
          if (!value || /^(knowledge|shared|files|inputs)\//.test(value)) return false;
          if (upstream.indexOf(value) !== -1) return false;
          return !upstream.some(function (pattern) { return pattern.slice(-1) === "/" && value.indexOf(pattern) === 0; });
        });
      }
      function wouldCycle(graph, from, to) {
        var outgoing = {};
        ((graph && graph.edges) || []).forEach(function (e) { (outgoing[e.from] = outgoing[e.from] || []).push(e.to); });
        var seen = {}, stack = [to];
        while (stack.length) {
          var current = stack.pop();
          if (current === from) return true;
          if (seen[current]) continue;
          seen[current] = true;
          (outgoing[current] || []).forEach(function (next) { stack.push(next); });
        }
        return false;
      }
      function pushUndo() {
        undoRef.current.push(cloneGraph(stateRef.current.graph));
        if (undoRef.current.length > 80) undoRef.current.shift();
        redoRef.current = [];
      }
      function mutate(fn) {
        pushUndo();
        setGraph(function (g) { var next = cloneGraph(g); fn(next); return next; });
        setDirty(true);
      }
      function undo() {
        if (!undoRef.current.length) return;
        var prev = undoRef.current.pop();
        redoRef.current.push(cloneGraph(stateRef.current.graph));
        setGraph(prev); setDirty(true);
      }
      function redo() {
        if (!redoRef.current.length) return;
        var next = redoRef.current.pop();
        undoRef.current.push(cloneGraph(stateRef.current.graph));
        setGraph(next); setDirty(true);
      }
      function uniqueId(base) {
        var ids = {};
        (stateRef.current.graph.nodes || []).forEach(function (n) { ids[n.id] = 1; });
        if (!ids[base]) return base;
        var i = 2;
        while (ids[base + "-" + i]) i++;
        return base + "-" + i;
      }
      function createLibraryModule(addToCanvas) {
        if (!moduleForm || busy) return;
        setBusy(true); setModuleError("");
        function lines(value) { return String(value || '').split("\n").map(function (s) { return s.trim(); }).filter(Boolean); }
        var payload = { name: moduleForm.name, instructions: moduleForm.instructions,
          requires: lines(moduleForm.inputs), artifacts: lines(moduleForm.outputs),
          timeout_seconds: moduleForm.timeout || null,
          checklist: lines(moduleForm.checklist) };
        if (moduleForm.editingId) payload.id = moduleForm.editingId;
        postJson(moduleForm.editingId ? "/modules/update" : "/modules/create", payload)
          .then(function (r) {
            setModules(r.modules); setSearch(""); setModuleForm(null);
            if (addToCanvas) addFromPalette(r.module);
            setHelpModule(null);
            setMsg(moduleForm.editingId ? "模块定义已更新，下一次执行使用新定义" : addToCanvas ? "新模块已创建并加入画布，保存后可运行" : "新模块已存入模块库，点击即可加入画布");
          }).catch(function (e) { setModuleError(e.message); }).finally(function () { setBusy(false); });
      }

      function editLibraryModule(module) {
        setModuleError("");
        setModuleForm({ name: module.name || '', instructions: module.agent_prompt || module.description || '',
          inputs: (module.requires || []).join('\n'), outputs: (module.artifacts || []).join('\n'),
          checklist: (module.checklist || []).join('\n'), timeout: String(module.timeout_seconds || module.timeout_sec || ''), editingId: module.id });
      }

      async function deleteLibraryModule(module) {
        if (busy || !(await askConfirm("从模块库删除「" + module.name + "」？已放入画布的步骤会保留，并可继续运行。"))) return;
        setBusy(true);
        postJson("/modules/delete", { id: module.id }).then(function (r) {
          setModules(r.modules); setHelpModule(null); setMsg("模块已从模块库删除，画布现有步骤已保留");
        }).catch(function (e) { setMsg("删除失败：" + e.message); }).finally(function () { setBusy(false); });
      }

      function addModuleAt(mod, pos) {
        var id = uniqueId(mod.id);
        var g = stateRef.current.graph;
        var last = g.nodes.length ? g.nodes[g.nodes.length - 1].id : null;
        var from = selectedNode && selectedNode !== id ? selectedNode : last;
        var edges = (g.edges || []).slice();
        if (from && from !== id) edges.push({ from: from, to: id });
        pushUndo();
        setGraph({ nodes: g.nodes.concat([{ id: id, module: mod.id, x: Math.round(pos.x), y: Math.round(pos.y) }]), edges: edges });
        setSelectedNode(id);
        setSelectedEdge(null);
        setHelpModule(null);
        setDirty(true);
        setMsg("已添加 " + mod.name + (from ? "，接在 " + from + " 之后" : ""));
      }
      function addFromPalette(mod) {
        var el = viewportRef.current;
        var v = stateRef.current.view;
        var w = el ? el.clientWidth : 800, hgt = el ? el.clientHeight : 500;
        addModuleAt(mod, { x: (w / 2 - v.x) / v.k + (Math.random() * 50 - 25), y: (hgt / 2 - v.y) / v.k + (Math.random() * 50 - 25) });
      }
      function addEdge(from, to) {
        if (!from || !to || from === to) { setMsg("不能连接到自己"); setConnectFrom(null); return; }
        var graph = stateRef.current.graph;
        var exists = (graph.edges || []).some(function (e) { return e.from === from && e.to === to; });
        if (exists) { setMsg("这条连线已存在"); setConnectFrom(null); return; }
        if (wouldCycle(graph, from, to)) { setMsg("不能连：这条线会让流程成环"); setConnectFrom(null); return; }
        var tentative = cloneGraph(graph);
        tentative.edges = (tentative.edges || []).concat([{ from: from, to: to }]);
        mutate(function (next) { next.edges.push({ from: from, to: to }); });
        setConnectFrom(null);
        var target = (graph.nodes || []).filter(function (n) { return n.id === to; })[0] || {};
        var missing = missingInputs(tentative, to);
        if (missing.length) {
          setMsg("已连接；但「" + (target.name || to) + "」还缺输入：" + missing.join("、") + "（上游没有节点声明产出）");
        } else {
          setMsg("已连接 " + from + " → " + to);
        }
      }
      async function deleteSelected() {
        if (selectedEdge) {
          var key = selectedEdge;
          var graph = stateRef.current.graph;
          var removed = (graph.edges || []).filter(function (e) { return edgeKey(e) === key; })[0];
          var tentative = cloneGraph(graph);
          tentative.edges = (tentative.edges || []).filter(function (e) { return edgeKey(e) !== key; });
          mutate(function (next) { next.edges = next.edges.filter(function (e) { return edgeKey(e) !== key; }); });
          setSelectedEdge(null);
          var missing = removed ? missingInputs(tentative, removed.to) : [];
          if (missing.length) {
            var target = nodeById(removed.to) || {};
            setMsg("已删除连线；「" + (target.name || removed.to) + "」现在缺输入：" + missing.join("、"));
          } else {
            setMsg("已删除连线");
          }
          return;
        }
        if (!selectedNode) return;
        var id = selectedNode;
        if (!(await askConfirm("删除节点「" + id + "」及其连线？"))) return;
        mutate(function (next) {
          next.nodes = next.nodes.filter(function (n) { return n.id !== id; });
          next.edges = next.edges.filter(function (e) { return e.from !== id && e.to !== id; });
        });
        setSelectedNode(null); setMsg("已删除节点");
      }
      function save() {
        if (selectedRunRef.current) { setMsg("正在查看运行快照；请先返回编辑再保存模板。"); return Promise.reject(new Error("运行快照只读")); }
        setBusy(true);
        var current = projectRef.current, epoch = projectEpochRef.current;
        return (current ? postJson('/projects/update', { request_id: requestId(), project_id: current.project_id,
            expected_revision: current.revision, graph: stateRef.current.graph })
          : postJson("/canvas", { graph: stateRef.current.graph, revision: canvasRevision }))
          .then(function (r) {
            if (epoch !== projectEpochRef.current) return;
            if (r.project) { projectRef.current = r.project; setProject(r.project); }
            setGraph((r.project && r.project.graph) || r.graph || stateRef.current.graph);
            setCanvasRevision(r.revision || "");
            setDirty(false);
            setMsg("已保存，并按连线生成执行顺序");
          })
          .catch(function (e) {
            setMsg(e.status === 409 ? "画布已被另一个窗口修改，请刷新后再保存" : "保存失败：" + e.message + "（检查是否有环）");
            throw e;
          })
          .finally(function () { setBusy(false); });
      }
      async function runWorkflow() {
        var epoch = projectEpochRef.current;
        if (selectedRunRef.current) { setMsg("请先返回编辑，再启动新任务。"); return; }
        var ep = await askText(projectRef.current ? "本次运行名称（资料保存在当前项目）" : "给这次任务起个名字（自动使用同名知识分区）", projectRef.current ? projectRef.current.name : (stateRef.current.kbTask || (stateRef.current.run && friendlyRunName(stateRef.current.run)) || "新任务"));
        if (!ep || epoch !== projectEpochRef.current) return;
        setBusy(true);
        save().then(function () {
          if (epoch !== projectEpochRef.current) throw new Error("查看项目已变化，未提交运行");
          var current = projectRef.current;
          var body = { episode: ep };
          if (current) Object.assign(body, { project_id: current.project_id, expected_revision: current.revision, request_id: requestId() });
          if (stopAtReview && (stateRef.current.graph.nodes || []).some(function (n) { return n.id === "review"; })) body.until = "review";
          return postRunCommand("/wf/run", body);
        }).then(function (accepted) {
          if (epoch !== projectEpochRef.current) return;
          if (accepted && accepted.work_revision && projectRef.current) {
            projectRef.current = Object.assign({}, projectRef.current, { work_revision: accepted.work_revision, work_run_id: accepted.run_id });
            setProject(projectRef.current);
          }
          if (accepted && accepted.run_id) { selectedRunRef.current = accepted.run_id; setViewingRun(accepted.run_id); }
          startingRef.current = true;
          startTriesRef.current = 0;
          seenRunIdRef.current = dismissedRunRef.current || (stateRef.current.run && stateRef.current.run.run_id) || "";
          runStartRef.current = Date.now();
          setRun(function (prev) {
            return { episode: ep, status: "running", nodes: (stateRef.current.graph.nodes || []).map(function (n) { return { id: n.id, module: n.module, name: n.name || n.module, status: "pending" }; }) };
          });
          setMsg("▶ 已提交运行：" + ep + "，正在执行…");
        }).catch(function (e) { setMsg("运行失败：" + e.message); }).finally(function () { setBusy(false); });
      }
      function cancelRun() {
        var rid = stateRef.current.run && stateRef.current.run.run_id;
        if (!rid) return;
        setBusy(true);
        postRunCommand("/wf/cancel", { run_id: rid })
          .then(function (r) {
            if (r && r.stopping) setMsg("已请求停止：执行进程会在当前步骤结束后停下");
            else if (r && r.already) setMsg("这次运行已经结束：" + r.already);
            else setMsg("任务已停止：" + rid);
            pollRuntime();
          })
          .catch(function (e) { setMsg("停止失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }
      function pauseRun() {
        var rid = stateRef.current.run && stateRef.current.run.run_id;
        if (!rid) return;
        setBusy(true);
        postRunCommand("/wf/pause", { run_id: rid })
          .then(function () { setMsg("已请求暂停：当前步骤结束后停下，进度保留，可随时继续"); pollRuntime(); })
          .catch(function (e) { setMsg("暂停失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }
      function resumeRun() {
        var rid = stateRef.current.run && stateRef.current.run.run_id;
        if (!rid) return;
        setBusy(true);
        postRunCommand("/wf/resume", { run_id: rid })
          .then(function () { setMsg("已继续执行：" + rid); pollRuntime(); })
          .catch(function (e) { setMsg("继续失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }
      function stopTaskRun() {
        var rid = taskRun;
        if (!rid) return;
        postRunCommand("/wf/cancel", { run_id: rid })
          .then(function (r) {
            setMsg(r && r.stopping ? "已请求停止：执行进程会在当前步骤结束后停下" : "任务已停止");
            pollRuntime();
            loadWorkspace(taskRun, taskNode);
          })
          .catch(function (e) { setMsg("停止失败：" + e.message); });
      }

      async function replyToAgent() {
        var current = stateRef.current.run;
        var node = current && (current.nodes || []).filter(function (n) { return n.status === "awaiting_input"; })[0];
        if (node) openWorkspace(current.run_id, node.id);
      }

      async function rerunNode(rid, nodeId) {
        if (!(await askConfirm("从「" + nodeId + "」重跑（该节点及下游）？"))) return;
        setBusy(true);
        return postRunCommand("/wf/rerun", { run_id: rid, from: nodeId })
          .then(function () { setMsg("已从 " + nodeId + " 继续，正在执行…"); pollRuntime(); })
          .catch(function (e) { setMsg("重跑失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }
      function rerunFromNode() {
        var rid = stateRef.current.run && stateRef.current.run.run_id;
        if (!rid || !selectedNode) { setMsg("没有可重跑的运行"); return; }
        return rerunNode(rid, selectedNode);
      }

      function exportCanvas() {
        fetchJson("/canvas").then(function (r) {
          var blob = new Blob([JSON.stringify(r.graph || {}, null, 2)], { type: "application/json" });
          var a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = "canvas-workflow.json";
          a.click();
          setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
          setMsg("已导出画布 JSON");
        }).catch(function (e) { setMsg("导出失败：" + e.message); });
      }

      function importCanvas(e) {
        var file = e.target.files && e.target.files[0];
        if (!file) return;
        file.text().then(function (text) {
          var data = JSON.parse(text);
          return postJson("/canvas/import", { graph: data.graph || data });
        }).then(function () { setMsg("已导入画布"); load(); })
          .catch(function (err) { setMsg("导入失败：" + err.message); });
        e.target.value = "";
      }

      function openAi() {
        setGuideOpen(false);
        setAiOpen(function (v) { return !v; });
      }

      function openGuide() {
        setAiOpen(false);
        setGuideOpen(function (v) { return !v; });
      }

      function closeGuide() {
        setGuideOpen(false);
        try { window.localStorage.setItem("dsh-canvas-guide-seen", "1"); } catch (e) {}
      }

      function waitingNode() {
        return ((run && run.nodes) || []).filter(function (n) {
          return n.status === "awaiting_agent" || n.status === "awaiting_input";
        })[0] || null;
      }

      function sameTaskContext(ctx) {
        var now = taskCtxRef.current;
        return !!ctx && !!now && now.runId === ctx.runId && now.nodeId === ctx.nodeId;
      }

      function openHostSession(childId, parentId) {
        if (!childId || !parentId) { setMsg("这个节点还没有宿主父会话，先跑一次"); return; }
        try {
          var ui = props.ctx && typeof props.ctx.get === "function" ? props.ctx.get("uiWorkspace") : null;
          if (!ui || typeof ui.openSession !== "function") { setMsg("当前 DSH 版本不支持从画布打开子会话"); return; }
          ui.openSession({ parentSessionId: parentId, childSessionId: childId, mode: "continuable" });
          setMsg("已在 DSH 打开子会话");
        } catch (err) {
          setMsg("打开失败：" + ((err && err.message) || err));
        }
      }

      function closeTask() {
        rememberProjectView(null);
        taskCtxRef.current = null;
        artifactSeqRef.current += 1;
        logSeqRef.current += 1;
        wsSeqRef.current += 1;
        setTaskOpen(false);
        setTaskRun("");
        setTaskInfo(null);
        setWs(null);
        setWsErr("");
        setLineageOpen(false);
        setHistOpen(false);
        setResultsOpen(false);
        setWsText("");
        setWsLoading(false);
        setArtifactRows([]);
        setArtifactState("idle");
        setArtifactError("");
        setArtifactModal(null);
      }

      function loadArtifacts(runId, nodeId) {
        var rid = runId || (taskCtxRef.current && taskCtxRef.current.runId) || "";
        var nid = nodeId || (taskCtxRef.current && taskCtxRef.current.nodeId) || "";
        if (!rid || !nid) { setArtifactRows([]); setArtifactState("idle"); setArtifactError(""); return; }
        var seq = ++artifactSeqRef.current;
        setArtifactState("loading");
        setArtifactError("");
        fetchJson("/wf/artifacts?run_id=" + encodeURIComponent(rid) + "&node=" + encodeURIComponent(nid))
          .then(function (r) {
            if (seq !== artifactSeqRef.current) return;
            if (!sameTaskContext({ runId: rid, nodeId: nid })) return;
            setArtifactRows(r.artifacts || []);
            setArtifactState("ready");
          })
          .catch(function (e) {
            if (seq !== artifactSeqRef.current) return;
            if (!sameTaskContext({ runId: rid, nodeId: nid })) return;
            setArtifactRows([]);
            setArtifactError(e.message || "读取产物失败");
            setArtifactState("error");
          });
      }

      function openArtifact(row) {
        var ctx = row && row._runId ? { runId: row._runId, nodeId: row.node_id || "", results: true } : taskCtxRef.current;
        if (!ctx || !row || !row.path) return;
        var query = "/wf/artifact?run_id=" + encodeURIComponent(ctx.runId) + "&path=" + encodeURIComponent(row.path);
        var raw = row.media_url || API + "/wf/raw?run_id=" + encodeURIComponent(ctx.runId) + "&path=" + encodeURIComponent(row.path);
        var source = { runId: ctx.runId, nodeId: row.node_id || ctx.nodeId, generation: row.generation || (ws && ws.generation), sha256: row.sha256, logical: row.logical_path || row.path };
        if (row.kind === "image" || row.kind === "text") {
          setBusy(true);
          fetchJson(query)
            .then(function (r) {
              if (ctx.results ? !resultCtxRef.current || resultCtxRef.current.runId !== ctx.runId : !sameTaskContext(ctx)) return;
              setArtifactModal(Object.assign({}, r, { kind: r.kind === "image" && !r.data ? "file" : r.kind,
                url: raw, download: raw + "&download=1", status: row.status, zoom: 1, _source: source }));
            })
            .catch(function (e) { if (ctx.results || sameTaskContext(ctx)) setMsg("打开产物失败：" + e.message); })
            .finally(function () { setBusy(false); });
          return;
        }
        setArtifactModal({ kind: row.kind, path: row.path, name: row.name, bytes: row.bytes,
          status: row.status, url: raw, download: raw + "&download=1", zoom: 1, _source: source });
      }

      function approveVisual() {
        var ctx = taskCtxRef.current;
        if (!ctx) return;
        setBusy(true);
        postRunCommand("/wf/visual-review", { run_id: ctx.runId, node: ctx.nodeId })
          .then(function (r) {
            if (!sameTaskContext(ctx)) return;
            setMsg("已记录视觉复核：" + (((r || {}).review || {}).status || "PASS"));
            return load();
          })
          .catch(function (e) { if (sameTaskContext(ctx)) setMsg("视觉复核失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      function compareArtifact() {
        var current = artifactModal, source = current && current._source;
        if (!source || source.generation < 2) return;
        fetchJson(projectPath('/wf/diff?run_id=' + encodeURIComponent(source.runId) + '&node=' + encodeURIComponent(source.nodeId) + '&from_generation=' + (source.generation - 1) + '&to_generation=' + source.generation + '&path=' + encodeURIComponent(source.logical)))
          .then(function (r) { setArtifactModal(Object.assign({}, r, { status: 'verified' })); }).catch(function (e) { setMsg(e.message); });
      }

      function loadTask(nodeId) {
        var current = stateRef.current.run;
        var rid = (current && current.run_id) || "";
        if (!rid || !nodeId) { setMsg("先运行或选择一次执行记录，再查看产物"); return; }
        var nodeInfo = ((current.nodes || []).filter(function (n) { return n.id === nodeId; })[0]) || { id: nodeId };
        var ctx = { runId: rid, nodeId: nodeId };
        taskCtxRef.current = ctx;
        setTaskRun(rid);
        setTaskNode(nodeId);
        setTaskInfo(Object.assign({}, nodeInfo, { _automatic: !!current.agent_mode }));
        setTaskOpen(true);
        setArtifactModal(null);
        setTaskContent("（任务卡读取中…）");
        loadArtifacts(rid, nodeId);
        fetchJson("/wf/task?run_id=" + encodeURIComponent(rid) + "&node=" + encodeURIComponent(nodeId))
          .then(function (r) { if (sameTaskContext(ctx)) setTaskContent(r.content || "（这次运行没有留下任务卡）"); })
          .catch(function () { if (sameTaskContext(ctx)) setTaskContent("（读取任务卡失败）"); });
      }

      function loadWorkspace(runId, nodeId, generation) {
        var seq = ++wsSeqRef.current;
        var suffix = generation ? "&generation=" + encodeURIComponent(generation) : "";
        return fetchJson("/wf/workspace?run_id=" + encodeURIComponent(runId) + "&node=" + encodeURIComponent(nodeId) + suffix)
          .then(function (r) {
            if (seq !== wsSeqRef.current) return;
            if (!sameTaskContext({ runId: runId, nodeId: nodeId })) return;
            setWs(r);
            rememberProjectView({ type: 'workspace', runId: runId, nodeId: nodeId, generation: r.generation });
            setWsLoading(false);
            setWsErr("");
            setTaskInfo(Object.assign({}, r.node || {}, {
              _automatic: !!(r.run && r.run.agent_mode === "automatic"),
            }));
          })
          .catch(function (e) {
            if (seq === wsSeqRef.current && sameTaskContext({ runId: runId, nodeId: nodeId })) {
              setWsLoading(false);
              setWsErr(e.message || "读取模块工作区失败");
            }
          });
      }

      function openWorkspace(runId, nodeId) {
        rememberProjectView({ type: 'workspace', runId: runId, nodeId: nodeId });
        resultsSeqRef.current += 1;
        resultCtxRef.current = null;
        var ctx = { runId: runId, nodeId: nodeId };
        taskCtxRef.current = ctx;
        setTaskRun(runId);
        setTaskNode(nodeId);
        setTaskOpen(true);
        setTaskContent("");
        setTaskInfo(null);
        setWs(null);
        setWsLoading(true);
        setWsErr("");
        setHistOpen(false);
        setResultsOpen(false);
        setKbOpen(false);
        setWsText("");
        setArtifactModal(null);
        loadWorkspace(runId, nodeId);
        loadArtifacts(runId, nodeId);
      }

      function loadOlderMessages() {
        var current = ws, ctx = taskCtxRef.current;
        if (!current || !ctx || !current.message_page || !current.message_page.has_older) return;
        var generation = current.generation, before = current.message_page.first_seq;
        fetchJson(projectPath('/wf/messages?run_id=' + encodeURIComponent(ctx.runId) + '&node=' + encodeURIComponent(ctx.nodeId) + '&generation=' + generation + '&before_seq=' + before))
          .then(function (page) {
            if (!sameTaskContext(ctx)) return;
            setWs(function (latest) {
              if (!latest || latest.generation !== generation) return latest;
              var seen = {}; var messages = (page.messages || []).concat(latest.messages || []).filter(function (m) { if (seen[m.id]) return false; seen[m.id] = true; return true; });
              return Object.assign({}, latest, { messages: messages, message_page: Object.assign({}, latest.message_page, { first_seq: page.first_seq, has_older: page.has_older }) });
            });
          }).catch(function (e) { setMsg(e.message); });
      }

      function adoptWorkspaceChanges() {
        var ctx = taskCtxRef.current; if (!ctx || !ws || ws.readonly) return;
        setBusy(true);
        postRunCommand('/wf/adopt-changes', { run_id: ctx.runId, node: ctx.nodeId, expected_generation: ws.generation,
          hashes: ws.workspace_hashes, request_id: requestId() })
          .then(function () { if (sameTaskContext(ctx)) { setMsg('修改已登记为新版本，请阅读成果并完成内容审阅。'); loadWorkspace(ctx.runId, ctx.nodeId); } })
          .catch(function (e) { if (sameTaskContext(ctx)) setMsg(e.message); })
          .finally(function () { setBusy(false); });
      }
      function approveWorkspaceReview() {
        var ctx = taskCtxRef.current; if (!ctx || !ws || ws.readonly) return;
        setBusy(true);
        postRunCommand('/wf/review', { run_id: ctx.runId, node: ctx.nodeId, expected_generation: ws.generation,
          hashes: ws.artifact_hashes, request_id: requestId() })
          .then(function () { if (sameTaskContext(ctx)) { setMsg('本版本内容审阅已通过'); loadWorkspace(ctx.runId, ctx.nodeId); } })
          .catch(function (e) { if (sameTaskContext(ctx)) setMsg(e.message); })
          .finally(function () { setBusy(false); });
      }
      function syncWorkspace() {
        var ctx = taskCtxRef.current; if (!ctx || !ws || ws.readonly) return;
        setBusy(true);
        postRunCommand('/wf/sync-session', { run_id: ctx.runId, node: ctx.nodeId, expected_generation: ws.generation })
          .then(function (r) { if (sameTaskContext(ctx)) { setMsg(r.hint); loadWorkspace(ctx.runId, ctx.nodeId); loadArtifacts(ctx.runId, ctx.nodeId); } })
          .catch(function (e) { if (sameTaskContext(ctx)) setMsg('同步失败：' + e.message); })
          .finally(function () { setBusy(false); });
      }
      function sendWorkspaceReply() {
        var ctx = taskCtxRef.current;
        var text = (wsText || "").trim();
        if (!ctx || !text) return;
        if (busy || workspaceMutationRef.current || !ws || ws.node.status !== "awaiting_input") return;
        var requestId = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now());
        workspaceMutationRef.current = requestId;
        setBusy(true);
        return postRunCommand("/wf/reply", { run_id: ctx.runId, node: ctx.nodeId, answer: text, expected_generation: ws.node.generation,
          request_id: requestId })
          .then(function () {
            if (!sameTaskContext(ctx)) return;
            setWsText("");
            setMsg("已回复，继续同一工作会话…");
            loadWorkspace(ctx.runId, ctx.nodeId);
          })
          .catch(function (e) { setMsg("回复失败：" + e.message); })
          .finally(function () { if (workspaceMutationRef.current === requestId) workspaceMutationRef.current = null; setBusy(false); });
      }

      function discussWorkspace() {
        var ctx = taskCtxRef.current, text = (wsText || '').trim();
        if (!ctx || !text || busy || workspaceMutationRef.current || !ws) return;
        var commandId = requestId(); workspaceMutationRef.current = commandId; setBusy(true);
        postRunCommand('/wf/discuss', { run_id: ctx.runId, node: ctx.nodeId, answer: text,
          expected_generation: ws.generation, request_id: commandId })
          .then(function () { if (sameTaskContext(ctx)) { setWsText(''); setMsg('已提交讨论，完成后刷新或同步会话查看回复。'); loadWorkspace(ctx.runId, ctx.nodeId); } })
          .catch(function (e) { if (sameTaskContext(ctx)) setMsg(e.message); })
          .finally(function () { if (workspaceMutationRef.current === commandId) workspaceMutationRef.current = null; setBusy(false); });
      }

      function reviseWorkspace() {
        var ctx = taskCtxRef.current;
        var text = (wsText || "").trim();
        if (!ctx || !text || busy || workspaceMutationRef.current) return;
        var requestId = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now());
        workspaceMutationRef.current = requestId;
        setBusy(true);
        return postRunCommand("/wf/revise", { run_id: ctx.runId, node: ctx.nodeId, answer: text, expected_generation: ws && ws.node ? ws.node.generation : undefined, request_id: requestId })
          .then(function (r) {
            if (!sameTaskContext(ctx)) return;
            setWsText("");
            setMsg("已创建修订，原有成果保留；受影响的下游将重新验收。");
            openWorkspace(r.run_id || ctx.runId, r.node || ctx.nodeId);
          }).catch(function (e) { if (sameTaskContext(ctx)) setMsg("创建修订失败：" + e.message); })
          .finally(function () { if (workspaceMutationRef.current === requestId) workspaceMutationRef.current = null; setBusy(false); });
      }

      function openTaskForCurrent() {
        var n = waitingNode();
        if (n && run && run.run_id) openWorkspace(run.run_id, n.id);
        else setMsg("当前没有等待中的任务");
      }

      function toggleCheck(index) {
        var n = waitingNode();
        if (!n) return;
        var rid = run.run_id;
        postRunCommand("/wf/check", { run_id: rid, node: n.id, index: index }).then(function () {
          fetchJson(projectPath("/wf/status")).then(function (r) { if (r.run) setRun(r.run); }).catch(function () {});
        }).catch(function (e) { setMsg("勾选失败：" + e.message); });
      }

      function handoffTask() {
        var n = waitingNode();
        if (!n || !run) { setMsg("当前没有等待中的任务"); return; }
        if (n.status !== "awaiting_agent") { setMsg("这一步在等你回复，先用「回复并继续」"); return; }
        try {
          var ctx = props.ctx;
          var ws = ctx && typeof ctx.get === "function" ? ctx.get("uiWorkspace") : null;
          var rid = run.run_id;
          var taskPath = ".runs/" + rid + "/tasks/" + n.id + ".md";
          var prompt = [
            "请执行画布任务。",
            "run_id: " + rid,
            "node: " + n.id + "（" + (n.name || "") + "）",
            "任务卡: " + taskPath.replace("/", "\\"),
            "",
            "先读任务卡和知识库上下文，完成任务并写出声明的产物。",
            "逐项确认验收清单后运行：",
            "python wf.py agent-check --run " + rid + " --node " + n.id + " --all",
            "python wf.py agent-done --run " + rid + " --node " + n.id,
          ].join("\n");
          if (ws && typeof ws.startSession === "function") {
            ws.startSession(undefined, { agentPreset: "cordis", prompt: prompt, whenDraftExists: "append" });
            setMsg("已交给贾维斯执行：" + (n.name || n.id));
          } else {
            setMsg("无法自动发起对话，请把任务卡内容贴给贾维斯");
          }
        } catch (e) { setMsg("交给贾维斯失败：" + e.message); }
      }

      function completeTask() {
        var n = waitingNode();
        if (!n || !run) return;
        var checks = n.checks || [];
        if (checks.length && !checks.every(Boolean)) { setMsg("验收清单还没全部确认"); return; }
        setBusy(true);
        postRunCommand("/wf/agent-done", { run_id: run.run_id, node: n.id })
          .then(function () { closeTask(); setMsg("已确认通过，继续下一步…"); setTimeout(load, 1500); setTimeout(load, 4000); })
          .catch(function (e) { setMsg("确认失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      async function skipTask() {
        var n = waitingNode();
        if (!n || !run) return;
        if (!(await askConfirm("跳过这个任务？"))) return;
        setBusy(true);
        postRunCommand("/wf/agent-skip", { run_id: run.run_id, node: n.id })
          .then(function () { closeTask(); setMsg("已跳过，继续下一步…"); setTimeout(load, 1500); })
          .catch(function (e) { setMsg("跳过失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      function regenTask() {
        var n = waitingNode();
        if (!n || !run) return;
        setBusy(true);
        postRunCommand("/wf/rerun", { run_id: run.run_id, from: n.id })
          .then(function () { setMsg("已重新生成任务卡"); setTimeout(load, 1500); })
          .catch(function (e) { setMsg("重试失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      function openHistory() {
        var shouldOpen = !histOpen;
        closeTask();
        setKbOpen(false);
        setHistOpen(shouldOpen);
        fetchJson(projectPath("/wf/history")).then(function (r) { setHistory(r.runs || []); }).catch(function () {});
      }

      function loadRunDetail(rid) {
        return fetchJson("/wf/run-state?run_id=" + encodeURIComponent(rid))
          .then(function (r) { setHistDetail(r.run || null); })
          .catch(function () { setHistDetail(null); });
      }

      function viewRun(rid) {
        if (!rid) return;
        if (stateRef.current.dirty) { setMsg("请先保存当前画布，再查看运行快照。"); return; }
        dismissedRunRef.current = "";
        selectedRunRef.current = rid;
        setViewingRun(rid);
        setHistOpen(false);
        return fetchJson("/wf/run-state?run_id=" + encodeURIComponent(rid)).then(function (r) {
          if (selectedRunRef.current !== rid) return;
          setRun(r.run || null);
          var saved = r.graph;
          if (!saved || !saved.nodes) saved = { nodes: ((r.run || {}).nodes || []).map(function (n, i) {
            return { id: n.id, module: n.module, name: n.name, x: 30 + (i % 4) * 270, y: 35 + Math.floor(i / 4) * 160 };
          }), edges: ((r.run || {}).edges || []) };
          setGraph(saved);
          setDirty(false);
          setTimeout(fitView, 100);
        }).catch(function (e) { setMsg("读取运行快照失败：" + e.message); });
      }

      function exitRunView() {
        selectedRunRef.current = "";
        setViewingRun("");
        closeTask();
        setResultsOpen(false);
        load();
        pollRuntime();
      }

      function openResults(rid) {
        rid = rid || (stateRef.current.run || {}).run_id;
        if (!rid) { setMsg("还没有运行记录，请先运行一个任务。"); return; }
        dismissedRunRef.current = "";
        closeTask();
        setKbOpen(false);
        var seq = ++resultsSeqRef.current;
        resultCtxRef.current = { runId: rid };
        setResultRun(rid);
        rememberProjectView({ type: 'results', runId: rid });
        setResultsOpen(true);
        setHistOpen(false);
        setResultsData(null);
        setDeliveryDraft(null);
        setPrimaryText(null);
        setResultsError("");
        return Promise.all([fetchJson(projectPath("/wf/results?run_id=" + encodeURIComponent(rid))), fetchJson("/wf/run-state?run_id=" + encodeURIComponent(rid))])
          .then(function (rows) {
            if (seq !== resultsSeqRef.current) return;
            setResultsData(rows[0]);
            setResultNodes((rows[1].run || {}).nodes || []);
            var main = primaryArtifact(rows[0]);
            if (!isInlineTextArtifact(main)) return;
            setPrimaryText({ status: "loading", text: "" });
            fetchJson("/wf/artifact?run_id=" + encodeURIComponent(rid) + "&path=" + encodeURIComponent(main.path))
              .then(function (r) { if (seq === resultsSeqRef.current) setPrimaryText({ status: "ready", text: r.text || "" }); })
              .catch(function (e) { if (seq === resultsSeqRef.current) setPrimaryText({ status: "error", text: e.message || "读取文本预览失败" }); });
          }).catch(function (e) { if (seq === resultsSeqRef.current) setResultsError(e.message || "读取任务成果失败"); });
      }

      function closeResults() { rememberProjectView(null); resultsSeqRef.current += 1; resultCtxRef.current = null; setResultsOpen(false); setArtifactModal(null); setDeliveryDraft(null); }

      function editDeliveryScope() {
        var seen = {};
        setDeliveryDraft((resultsData.artifacts || []).map(function (a) {
          var path = a.logical_path || a.path, policy = (resultsData.delivery_declarations || []).find(function (d) { return d.node_id === a.node_id && d.path === path; }) || {};
          return { node_id: a.node_id, path: path, role: policy.role || (a.is_primary ? 'primary' : 'attachment'), required: policy.required !== false, review_required: !!policy.review_required || !!a.is_primary };
        }).filter(function (row) { var key = row.node_id + ':' + row.path; if (seen[key]) return false; seen[key] = true; return true; }));
      }

      function saveDeliveryScope() {
        var rid = resultRun;
        postRunCommand('/wf/deliveries', { run_id: rid, expected_state_revision: resultsData.run.state_revision, declarations: deliveryDraft })
          .then(function () { setMsg('交付范围已确认；所需内容审阅完成后可导出'); openResults(rid); load(); })
          .catch(function (e) { setMsg('交付范围未保存：' + e.message); });
      }


      function saveKbEdit() {
        var task = knowledgeTask();
        if (!kbPath || !task) return;
        postJson("/kb/write", { task_id: task, path: kbPath, content: kbEditText, expected_sha256: kbMeta && kbMeta.file_sha256 })
          .then(function () { setMsg("当前任务资料已保存"); setKbEdit(false); return loadKbEntries(task, ""); })
          .catch(function (e) { setMsg("知识库保存失败：" + e.message); });
      }

      function knowledgeTask() {
        if (stateRef.current.kbTask === "__shared__") return "";
        return stateRef.current.kbTask || (projectRef.current && projectRef.current.project_id) || (stateRef.current.run && stateRef.current.run.task_id) || "";
      }

      async function decideKnowledge(status) {
        if (!kbMeta) return;
        var item = kbMeta, task = knowledgeTask();
        if (!(await askConfirm('将当前第 ' + item.knowledge_revision + ' 版资料设为' + ({ confirmed: '已确认知识', deprecated: '停用（以后不采用）', draft: '草稿' }[status]) + '？'))) return;
        postJson('/kb/decision', { task_id: task, path: item.path, expected_revision: item.knowledge_revision, expected_sha256: item.file_sha256, status: status, request_id: commandId() })
          .then(function () { setMsg('知识状态已记录'); readKb(item.path); }).catch(function (e) { setMsg(e.message); });
      }

      async function promoteKnowledge() {
        var artifact = artifactModal, meta = artifact && artifact._source;
        if (!meta || artifact.status !== 'verified') return;
        var projectId = (projectRef.current || {}).project_id || (stateRef.current.run || {}).task_id;
        var name = (artifact.path || '').split('/').pop();
        var path = await askText('知识保存路径：shared/ 为共享；tasks/项目ID/ 为本项目。确认后保存来源、代次和哈希。', 'tasks/' + projectId + '/confirmed/' + name);
        if (!path) return;
        var expected = null;
        var targetTask = path.indexOf('shared/') === 0 ? '' : projectId;
        try { var old = await fetchJson('/kb/read?task_id=' + encodeURIComponent(targetTask) + '&path=' + encodeURIComponent(path)); expected = old.file_sha256;
          if (!(await askConfirm('同名知识已有第 ' + old.knowledge_revision + ' 版。用当前成果更新并保留旧版？取消后可另存其他路径。'))) return;
        } catch (e) { if (!/不存在|404/.test(e.message || '')) { setMsg(e.message); return; } }
        postRunCommand('/kb/promote', { run_id: meta.runId, node: meta.nodeId, generation: meta.generation, artifact_path: artifact.path, source_sha256: meta.sha256, path: path, expected_sha256: expected, request_id: commandId() })
          .then(function (r) { setMsg('已确认沉淀为知识第 ' + r.knowledge_revision + ' 版，来源已记录'); }).catch(function (e) { setMsg('沉淀失败：' + e.message); });
      }

      function loadKbEntries(task, query) {
        if (task === "__shared__") task = "";
        var requestId = ++kbRequestRef.current;
        ++kbReadRef.current;
        setKbEntries([]);
        setKbPath(""); setKbContent(""); setKbEdit(false);
        setKbMeta(null);
        // An empty task shows shared knowledge only; it never requests other tasks.
        var url = query && query.trim() ? "/kb/search?q=" + encodeURIComponent(query.trim()) : "/kb/list";
        url += (url.indexOf("?") === -1 ? "?" : "&") + "task_id=" + encodeURIComponent(task);
        if (!task) url += "&scope=shared";
        return fetchJson(url).then(function (r) { if (requestId === kbRequestRef.current) setKbEntries(r.entries || (r.index && r.index.entries) || []); })
          .catch(function (e) { if (requestId === kbRequestRef.current) { setKbEntries([]); setMsg("资料读取失败：" + e.message); } });
      }

      function searchKb(q) {
        setKbSearch(q);
        loadKbEntries(knowledgeTask(), q);
      }

      function askText(title, def) {
        return new Promise(function (resolve) {
          setDialog({ type: "prompt", title: title, value: def || "", resolve: resolve });
        });
      }

      function askConfirm(title) {
        return new Promise(function (resolve) {
          setDialog({ type: "confirm", title: title, resolve: resolve });
        });
      }

      function closeDialog(ok) {
        setDialog(function (d) {
          if (d && d.resolve) {
            if (d.type === "prompt") d.resolve(ok ? d.value : null);
            else d.resolve(!!ok);
          }
          return null;
        });
      }

      function reloadWorkflowIndex() {
        return fetchJson("/workflows").then(function (r) {
          setWorkflows(r.workflows || []);
          var activeMeta = (r.workflows || []).filter(function (w) { return w.id === r.active; })[0];
          setActiveWorkflow({ id: r.active || "", name: (activeMeta && activeMeta.name) || r.active || "工作流" });
        }).catch(function () {});
      }

      async function selectWorkflow(id) {
        if (projectRef.current) {
          var epoch = projectEpochRef.current;
          fetchJson('/workflows/export?id=' + encodeURIComponent(id)).then(function (r) {
            if (epoch !== projectEpochRef.current) return;
            setGraph(r.workflow.graph); setDirty(true); setWfOpen(false);
            setMsg('已将流程复制到当前项目；保存后生效');
          }).catch(function (e) { setMsg(e.message); });
          return;
        }
        if (!id || id === activeWorkflow.id) { setWfOpen(false); return; }
        var current = stateRef.current.run;
        if (current && runActive(current.status)) {
          if (current.status === "running" || current.status === "pausing" || current.status === "stopping") {
            setMsg("任务正在执行；请先点「暂停」或「停止」，再切换工作流");
            return;
          }
          var ok = await askConfirm("当前任务「" + (current.episode || current.run_id) + "」还没结束（" + statusLabel(current.status) + "）。切换工作流不会删除它，进度保留在「历史」里，可随时回来继续。确定切换？");
          if (!ok) return;
          dismissedRunRef.current = current.run_id || "";
          setRun(null);
        }
        selectedRunRef.current = "";
        setViewingRun("");
        setBusy(true);
        postJson("/workflows/select", { id: id }).then(function () {
          setMsg("已切换工作流");
          setWfOpen(false);
          return load();
        }).catch(function (e) { setMsg("切换工作流失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      async function createWorkflow() {
        var name = await askText("新工作流名称（例如：写作流程、调研流程）", "新工作流");
        if (!name) return;
        setBusy(true);
        postJson("/workflows/create", { name: name }).then(function () {
          setMsg("已创建并切换到：" + name);
          setWfOpen(false);
          return load();
        }).catch(function (e) { setMsg("创建工作流失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      async function renameWorkflow() {
        if (!activeWorkflow.id) return;
        var name = await askText("工作流名称", activeWorkflow.name);
        if (!name) return;
        postJson("/workflows/rename", { id: activeWorkflow.id, name: name }).then(function () {
          setMsg("工作流已重命名");
          return reloadWorkflowIndex();
        }).catch(function (e) { setMsg("重命名失败：" + e.message); });
      }

      async function duplicateWorkflow() {
        if (!activeWorkflow.id) return;
        var name = await askText("复制为", activeWorkflow.name + " 副本");
        if (!name) return;
        postJson("/workflows/create", { name: name, from: activeWorkflow.id }).then(function () {
          setMsg("已复制并切换");
          setWfOpen(false);
          return load();
        }).catch(function (e) { setMsg("复制失败：" + e.message); });
      }

      async function deleteWorkflow() {
        if (!activeWorkflow.id) return;
        if (!(await askConfirm("删除当前工作流「" + activeWorkflow.name + "」？至少保留一个工作流。"))) return;
        postJson("/workflows/delete", { id: activeWorkflow.id }).then(function () {
          setMsg("工作流已删除");
          return load();
        }).catch(function (e) { setMsg("删除失败：" + e.message); });
      }

      async function exportWorkflow() {
        if (!activeWorkflow.id) return;
        try {
          var r = await fetchJson("/workflows/export?id=" + encodeURIComponent(activeWorkflow.id));
          var blob = new Blob([JSON.stringify(r.workflow, null, 2)], { type: "application/json" });
          var a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = (activeWorkflow.name || "workflow").replace(/[\\/:*?"<>|]/g, "-") + ".dsh-workflow.json";
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
          setMsg("工作流已导出");
        } catch (e) { setMsg("导出失败：" + e.message); }
      }

      async function importWorkflow(event) {
        var file = event.target.files && event.target.files[0];
        if (!file) return;
        try {
          var data = JSON.parse(await file.text());
          await postJson("/workflows/import", { workflow: data });
          setMsg("工作流已导入");
          setWfOpen(false);
          await load();
        } catch (e) { setMsg("导入失败：" + e.message); }
        finally { event.target.value = ""; }
      }

      function openWorkflows() {
        setWfOpen(function (v) { return !v; });
        reloadWorkflowIndex();
      }

      function openKb() {
        closeTask();
        setResultsOpen(false);
        setAiOpen(false); setGuideOpen(false);
        setKbOpen(function (v) { return !v; });
        fetchJson("/kb/tasks").then(function (r) { setKbTasks(r.tasks || []); }).catch(function () {});
        loadKbEntries(knowledgeTask(), "");
      }

      function readKb(path) {
        var requestId = ++kbReadRef.current;
        setKbPath(path);
        setKbEdit(false);
        setKbImage(null);
        setKbOriginal("");
        setKbContent("正在读取资料…");
        setKbMeta(null);
        var task = knowledgeTask();
        fetchJson("/kb/read?task_id=" + encodeURIComponent(task) + "&path=" + encodeURIComponent(path))
          .then(function (r) { if (requestId === kbReadRef.current) {
            setKbMeta(r);
            setKbImage(r.kind === "image" && r.data ? r : null);
            setKbOriginal(r.url ? (r.url.indexOf("/api/") === 0 ? API + r.url.slice(4) : r.url) : "");
            var content = r.content || r.text || "";
            if (!content && r.parse_status) content = knowledgeParseLabel(r) + (r.error ? "\n" + r.error : "");
            setKbContent(content || "文件已保存，当前没有可提取的文本。");
            setKbEditText(content);
          } })
          .catch(function (e) { if (requestId === kbReadRef.current) setKbContent("读取失败：" + e.message); });
      }

      function uploadKbAttachment(event) {
        var input = event.target;
        var file = input.files && input.files[0];
        if (!file) return;
        var task = kbTask === '__shared__' ? '__shared__' : knowledgeTask();
        if (!task) { setMsg("先在知识库选择当前任务分区"); input.value = ""; return; }
        if (file.size > 25 * 1024 * 1024) { setMsg("附件不能超过 25 MB"); input.value = ""; return; }
        var reader = new FileReader();
        reader.onload = function () {
          var data = String(reader.result || "").split(",")[1] || "";
          postJson("/kb/attach", { task_id: task === "__shared__" ? "" : task, scope: task === "__shared__" ? "shared" : "task", name: file.name, data: data })
            .then(function (r) { setMsg("素材已上传：" + (r.path || file.name)); loadKbEntries(task, ""); })
            .catch(function (e) { setMsg("素材上传失败：" + e.message); })
            .finally(function () { input.value = ""; });
        };
        reader.readAsDataURL(file);
      }

      async function createKbTask() {
        var name = await askText("给这次任务建一个知识库分区（例如 day2 或选题名）", (run && run.episode) || "day1");
        if (!name) return;
        postJson("/kb/task", { name: name }).then(function (r) {
          setKbTask(r.task);
          setMsg("已创建/确认知识库分区：" + name);
          fetchJson("/kb/tasks").then(function (data) { setKbTasks(data.tasks || []); }).catch(function () {});
          return loadKbEntries(r.task, "");
        }).catch(function (e) { setMsg("创建分区失败：" + e.message); });
      }

      function sendAi() {
        var prompt = aiText.trim();
        if (!prompt) return;
        try {
          var ctx = props.ctx;
          var ws = ctx && typeof ctx.get === "function" ? ctx.get("uiWorkspace") : null;
          if (ws && typeof ws.startSession === "function") {
            ws.startSession(undefined, { agentPreset: "cordis", prompt: prompt, whenDraftExists: "append" });
            setAiLog(function (prev) { return prev.concat([prompt]).slice(-10); });
            setAiText("");
            setMsg("指令已发送给贾维斯（在对话中处理）");
          } else {
            setMsg("当前环境无法发送指令，请在对话里直接说");
          }
        } catch (err) { setMsg("发送失败：" + err.message); }
      }

      async function loadTemplate(id) {
        if (!id) return;
        if (!(await askConfirm("载入模板会替换当前画布，继续？"))) return;
        if (projectRef.current) {
          var epoch = projectEpochRef.current;
          fetchJson('/canvas/templates').then(function (r) {
            if (epoch !== projectEpochRef.current) return;
            var template = (r.templates || []).filter(function (t) { return t.id === id; })[0];
            if (!template || !template.graph) throw new Error('模板不存在或缺少流程');
            setGraph(cloneGraph(template.graph)); setDirty(true); setMsg('模板已复制到当前项目；保存后生效');
          }).catch(function (e) { setMsg(e.message); });
          return;
        }
        setBusy(true);
        postJson("/canvas/template", { id: id, revision: canvasRevision })
          .then(function (r) { setGraph(r.graph || { nodes: [], edges: [] }); setCanvasRevision((r && r.revision) || ""); setDirty(false); setMsg("已载入模板"); load(); })
          .catch(function (e) { setMsg("载入模板失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      async function restoreRevision(id) {
        if (!id) return;
        if (projectRef.current) { setMsg('项目流程保存在项目中；请从该项目的运行历史查看旧流程'); return; }
        if (!(await askConfirm("恢复这个历史版本？当前画布会先被存档。"))) return;
        setBusy(true);
        postJson("/canvas/restore", { id: id, revision: canvasRevision })
          .then(function (r) { setCanvasRevision((r && r.revision) || ""); setMsg("已恢复历史版本"); load(); })
          .catch(function (e) { setMsg("恢复失败：" + e.message); })
          .finally(function () { setBusy(false); });
      }

      function setNodeList(id, field, text) {
        var arr = String(text || "").split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
        setGraph(function (g) {
          var next = cloneGraph(g);
          next.nodes.forEach(function (n) { if (n.id === id) n[field] = arr; });
          return next;
        });
        setDirty(true);
      }

      function setNodeText(id, field, value) {
        setGraph(function (g) {
          var next = cloneGraph(g);
          next.nodes.forEach(function (n) { if (n.id === id) n[field] = value; });
          return next;
        });
        setDirty(true);
      }

      function setNodeChecklist(id, text) {
        var arr = String(text || "").split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
        setGraph(function (g) {
          var next = cloneGraph(g);
          next.nodes.forEach(function (n) { if (n.id === id) n.checklist = arr; });
          return next;
        });
        setDirty(true);
      }

      function setNodeError(id, cont) {
        setGraph(function (g) {
          var next = cloneGraph(g);
          next.nodes.forEach(function (n) { if (n.id === id) n.on_error = cont ? "continue" : "stop"; });
          return next;
        });
        setDirty(true);
      }

      function resetNodeMapping(id) {
        setGraph(function (g) {
          var next = cloneGraph(g);
          next.nodes.forEach(function (n) { if (n.id === id) { delete n.requires; delete n.artifacts; } });
          return next;
        });
        setDirty(true);
        setMsg("已恢复模块默认映射");
      }

      function autoLayout() {
        var g = stateRef.current.graph;
        if (!g.nodes.length) return;
        var indeg = {}, adj = {};
        g.nodes.forEach(function (n) { indeg[n.id] = 0; adj[n.id] = []; });
        (g.edges || []).forEach(function (e) { if (adj[e.from] && indeg[e.to] !== undefined) { adj[e.from].push(e.to); indeg[e.to]++; } });
        var queue = g.nodes.filter(function (n) { return !indeg[n.id]; }).map(function (n) { return n.id; });
        var depth = {};
        queue.forEach(function (id) { depth[id] = 0; });
        while (queue.length) {
          var cur = queue.shift();
          adj[cur].forEach(function (nxt) { depth[nxt] = Math.max(depth[nxt] || 0, depth[cur] + 1); queue.push(nxt); });
        }
        var row = {};
        mutate(function (next) {
          next.nodes.forEach(function (n) {
            var d = depth[n.id] || 0;
            row[d] = row[d] || 0;
            var idx = row[d] || 0;
            n.x = 80 + (d % 4) * 270;
            n.y = 90 + Math.floor(d / 4) * 175 + idx * 0; row[d] = idx + 1;
          });
        });
        setTimeout(fitView, 60);
        setMsg("已自动布局");
      }
      function fitView() {
        var el = viewportRef.current;
        var g = stateRef.current.graph;
        if (!el || !g.nodes.length) return;
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        g.nodes.forEach(function (n) { minX = Math.min(minX, n.x); minY = Math.min(minY, n.y); maxX = Math.max(maxX, n.x + NODE_W); maxY = Math.max(maxY, n.y + NODE_H); });
        var pad = 70;
        var k = clamp(Math.min((el.clientWidth - pad * 2) / (maxX - minX || 1), (el.clientHeight - pad * 2) / (maxY - minY || 1)), 0.5, 1.25);
        var gw = (maxX - minX) * k, gh = (maxY - minY) * k;
        setView({
          k: k,
          x: Math.max(24, (el.clientWidth - gw) / 2 - minX * k),
          y: Math.max(24, (el.clientHeight - gh) / 2 - minY * k),
        });
      }
      function zoomBy(f) {
        var el = viewportRef.current;
        var v = stateRef.current.view;
        var w = el ? el.clientWidth / 2 : 400, hgt = el ? el.clientHeight / 2 : 300;
        var wx = (w - v.x) / v.k, wy = (hgt - v.y) / v.k;
        var nk = clamp(v.k * f, 0.35, 2.2);
        setView({ k: nk, x: w - wx * nk, y: hgt - wy * nk });
      }

      function onCanvasPointerDown(e) {
        if (e.target !== e.currentTarget) return;
        setSelectedNode(null); setSelectedEdge(null); setConnectFrom(null);
        panRef.current = { sx: e.clientX, sy: e.clientY, vx: stateRef.current.view.x, vy: stateRef.current.view.y };
        try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) {}
      }
      function onCanvasPointerMove(e) {
        var p = panRef.current;
        if (p) { setView({ x: p.vx + e.clientX - p.sx, y: p.vy + e.clientY - p.sy, k: stateRef.current.view.k }); return; }
        if (connectingRef.current) {
          var r = viewportRef.current.getBoundingClientRect();
          var v = stateRef.current.view;
          setMouseWorld({ x: (e.clientX - r.left - v.x) / v.k, y: (e.clientY - r.top - v.y) / v.k });
        }
      }
      function onCanvasPointerUp(e) {
        panRef.current = null;
        try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      function onNodePointerDown(e, n) {
        if (e.button !== 0) return;
        e.stopPropagation();
        dragRef.current = { id: n.id, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y, moved: false };
        try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) {}
      }
      function onNodePointerMove(e) {
        var d = dragRef.current;
        if (!d) return;
        var dx = e.clientX - d.sx, dy = e.clientY - d.sy;
        if (!d.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
        d.moved = true;
        var k = stateRef.current.view.k;
        setGraph(function (g) {
          var next = cloneGraph(g);
          next.nodes.forEach(function (n) { if (n.id === d.id) { n.x = Math.max(0, d.ox + dx / k); n.y = Math.max(0, d.oy + dy / k); } });
          return next;
        });
      }
      function onNodePointerUp(e) {
        if (dragRef.current && dragRef.current.moved) {
          setDirty(true);
          dragMovedRef.current = true;
          setTimeout(function () { dragMovedRef.current = false; }, 0);
        }
        dragRef.current = null;
        try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      function onDrop(e) {
        e.preventDefault();
        var mid = e.dataTransfer.getData("text/module");
        if (!mid) return;
        var mod = moduleById(mid);
        if (!mod) return;
        var r = viewportRef.current.getBoundingClientRect();
        var v = stateRef.current.view;
        addModuleAt(mod, { x: (e.clientX - r.left - v.x) / v.k - NODE_W / 2, y: (e.clientY - r.top - v.y) / v.k - NODE_H / 2 });
      }

      var categories = [];
      (modules || []).forEach(function (m) { var c = m.category || "其他"; if (categories.indexOf(c) === -1) categories.push(c); });
      var filtered = (modules || []).filter(function (m) {
        if (m.archived) return false;
        if (!search.trim()) return true;
        var q = search.trim().toLowerCase();
        return (m.name + " " + m.id + " " + (m.description || "") + " " + (m.category || "")).toLowerCase().indexOf(q) !== -1;
      });
      var runMap = {};
      ((run && run.nodes) || []).forEach(function (n) { runMap[n.id] = n; });
      var monitor = executionModel(run, runtimeSync, runtimeError, Date.now() / 1000);
      var matchingRun = graphMatchesRun(graph, run);
      var activity = [];
      ((run && run.nodes) || []).forEach(function (n) { (n.activity || []).forEach(function (a) { activity.push({ at: a.at, name: n.name || n.id, phase: a.phase, tool_name: a.tool_name }); }); });
      activity.sort(function (a, b) { return b.at - a.at; });
      var selNode = selectedNode ? nodeById(selectedNode) : null;
      var selModule = selNode ? moduleById(selNode.module) : null;
      var selEdge = selectedEdge ? (graph.edges || []).filter(function (e) { return edgeKey(e) === selectedEdge; })[0] : null;

      function ToolButton(label, onClick, primary, disabled) {
        disabled = disabled || (!!viewingRun && ["保存", "运行", "撤销", "重做", "自动布局", "导入"].indexOf(label) !== -1);
        return h("button", {
          key: label, onClick: onClick, disabled: disabled || busy,
          style: {
            padding: "6px 11px", borderRadius: 8, cursor: (disabled || busy) ? "default" : "pointer",
            border: "1px solid " + (primary ? C.accent : C.border),
            background: primary ? C.accent : C.card, color: primary ? "#181207" : C.text,
            fontSize: 12, fontFamily: "'Noto Sans SC','Microsoft YaHei',sans-serif", opacity: disabled ? 0.5 : 1,
          },
        }, label);
      }

      actionsRef.current = { save: save, undo: undo, redo: redo, deleteSelected: deleteSelected };

      return h("div", {
        ref: canvasRootRef,
        style: { height: presentation ? '100vh' : "100%", width: presentation ? '100vw' : undefined, inset: presentation ? 0 : undefined, zIndex: presentation ? 99999 : undefined, display: "flex", flexDirection: "column", background: C.bg, color: C.text, fontFamily: "'Noto Sans SC','Microsoft YaHei',sans-serif", overflow: "hidden", position: presentation ? 'fixed' : "relative" },
      },
        projectOpen ? h('div', { style: { position: 'absolute', inset: 0, zIndex: 90, background: C.bg, padding: 24, overflow: 'auto' } },
          h('div', { style: { display: 'flex', gap: 12, alignItems: 'center' } },
            h('h2', null, '项目总览'), ToolButton('新建项目', createProject, true),
            ToolButton('导入项目包', function () { if (projectImportRef.current) projectImportRef.current.click(); }),
            ToolButton(presentation ? '退出全屏' : '全屏工作台', togglePresentation), ToolButton('关闭', function () { setProjectOpen(false); })),
          h('input', { ref: projectImportRef, type: 'file', accept: '.json,application/json', onChange: importProjectPackage, style: { display: 'none' } }),
          h('p', { style: { color: C.sub } }, '从目标开始，选择模板或空白流程，导入资料后运行。每个项目独立保存会话、版本和成果。'),
          environment ? h('details', { style: { padding: 10, marginBottom: 12, border: '1px solid ' + C.border, borderRadius: 8 } },
            h('summary', { style: { cursor: 'pointer', color: C.teal } }, '环境与视频工具 · 文档内置可用 · 视频' + environment.capabilities.video),
            h('p', { style: { color: C.sub, fontSize: 12 } }, environment.note),
            environment.runtime ? h('div', { style: { color: C.sub, fontSize: 11, overflowWrap: 'anywhere' } },
              h('p', null, '实际加载：' + environment.runtime.version + ' · build ' + environment.runtime.build_id + ' · schema ' + environment.runtime.schema),
              h('p', null, '源目录：' + environment.runtime.source_directory), h('p', null, '数据目录：' + environment.runtime.data_directory)) : null,
            ['node','chrome','ffmpeg','ffprobe','remotion_dependencies'].map(function (key) { return h('label', { key: key, style: { display: 'block', marginBottom: 9 } },
              key + (environment.available[key] ? ' · 已找到' : ' · 待配置'),
              h('input', { 'aria-label': '工具路径 ' + key, value: toolPaths[key] || '',
                onChange: function (e) { var next = Object.assign({}, toolPaths); next[key] = e.target.value; setToolPaths(next); },
                placeholder: '已安装工具的绝对路径', style: { display: 'block', boxSizing: 'border-box', width: '100%', padding: 7, color: C.text, background: C.card, border: '1px solid ' + C.border } })); }),
            ToolButton('保存并检测工具', saveToolConfiguration),
            h('p', { style: { color: C.sub, fontSize: 11 } }, '视频需要Node、Chrome、FFmpeg与Remotion。工具路径保存在本机，不随模板/项目分享。参见随包 docs/视频配置.md。'),
            h('p', { style: { color: C.sub, fontSize: 11 } }, '诊断仅含版本、系统和状态计数，不含机器路径、账号、提示词或聊天正文，不自动上传。'),
            ToolButton('下载脱敏诊断', downloadDiagnostics)) : null,
          h('select', { value: project ? project.project_id : '', onChange: function (e) { selectProject(e.target.value); },
            style: { padding: 10, background: C.card, color: C.text, border: '1px solid ' + C.border, width: '100%' } },
            h('option', { value: '' }, '原有工作流与全局历史'), projects.map(function (p) {
              return h('option', { key: p.project_id, value: p.project_id }, p.name + ' · ' + p.goal.slice(0, 32) + (p.archived ? ' · 已归档' : ''));
            })),
          projectDraft ? h('form', { onSubmit: function (e) { e.preventDefault(); saveProjectDraft(); },
            style: { marginTop: 20, padding: 18, border: '1px solid ' + C.border, borderRadius: 10 } },
            h('h3', null, projectDraft.project_id ? '修改项目信息' : '新建项目'),
            [['name', '项目名称'], ['goal', '项目目标'], ['delivery', '交付要求']].map(function (field) {
              return h('label', { key: field[0], style: { display: 'block', marginBottom: 14 } }, field[1],
                h(field[0] === 'name' ? 'input' : 'textarea', { 'aria-label': field[1], required: field[0] !== 'delivery',
                  maxLength: field[0] === 'name' ? 80 : field[0] === 'goal' ? 20000 : 5000,
                  rows: 3, value: projectDraft[field[0]], onChange: function (e) { var update = {}; update[field[0]] = e.target.value; setProjectDraft(Object.assign({}, projectDraft, update)); },
                  style: { display: 'block', boxSizing: 'border-box', width: '100%', marginTop: 6, padding: 10, color: C.text, background: C.card, border: '1px solid ' + C.border, borderRadius: 6 } }));
            }),
            !projectDraft.project_id ? h('div', { style: { marginBottom: 12 } },
              h('label', null, '流程起点 ', h('select', { 'aria-label': '流程起点', value: projectDraft.start || 'blank',
                onChange: function (e) { setProjectDraft(Object.assign({}, projectDraft, { start: e.target.value })); },
                style: { background: C.card, color: C.text, padding: 8 } },
                h('option', { value: 'blank' }, '空白流程'), h('option', { value: 'template' }, '选择内置模板'),
                h('option', { value: 'copy' }, '仅复制当前流程定义'))),
              projectDraft.start === 'template' ? h('select', { 'aria-label': '项目模板', value: projectDraft.template,
                onChange: function (e) { setProjectDraft(Object.assign({}, projectDraft, { template: e.target.value })); },
                style: { background: C.card, color: C.text, padding: 8, marginLeft: 8 } },
                h('option', { value: '' }, '选择模板…'), (workflows || []).map(function (w) { return h('option', { key: w.id, value: w.id }, w.name || w.id); })) : null,
              h('p', { style: { color: C.sub, fontSize: 11 } }, '复制只包含步骤定义，不带原项目的资料、会话和成果。')) : null,
            h('button', { type: 'submit', disabled: busy, style: { padding: '8px 16px', background: C.accent, color: C.bg, borderRadius: 6 } }, busy ? '保存中…' : '保存项目'),
            ToolButton('取消', function () { setProjectDraft(null); })) : null,
          project ? h('div', null, h('h3', null, project.name), h('p', { style: { whiteSpace: 'pre-wrap' } }, project.goal),
            h('p', { style: { color: C.sub } }, '交付要求：' + (project.delivery || '尚未约定')),
            h('p', null, '项目状态：' + ((overview || {}).project_status_label || '准备中')),
            h('p', { style: { color: C.sub } }, '当前工作：' + ((overview || {}).current_run ? friendlyRunName(overview.current_run) + ' · ' + statusLabel(overview.current_run.status) : '尚未运行')),
            h('p', { style: { color: C.sub } }, '当前交付版：' + ((overview || {}).delivery_run ? friendlyRunName(overview.delivery_run) + (overview.delivery_valid ? ' · 可下载' : ' · 需重新核对') : '尚未指定；成果通过审阅后可在运行卡片中设定')),
            ToolButton('编辑流程', function () { setProjectOpen(false); }), ToolButton('修改项目信息', editProject),
            !project.archived ? ToolButton('AI 建议流程', suggestProjectWorkflow, true) : h('p', null, '项目已归档，历史成果仍可查看。'),
            !project.archived ? ToolButton('归档项目', archiveProject) : null,
            h('details', { style: { marginTop: 12 } }, h('summary', { style: { cursor: 'pointer', color: C.sub } }, '迁移与分享项目'),
              h('p', { style: { color: C.sub, fontSize: 12 } }, '项目包与成果交付ZIP分开。默认只包含AI流程和模块定义；主动选择后才加入项目资料，不带共享资料、会话、账号或机器工具配置。'),
              ToolButton('导出项目定义', function () { exportProjectPackage(false); }),
              ToolButton('含项目资料导出', function () { exportProjectPackage(true); })),
            h('h3', null, '需要处理'),
            ((overview || {}).commands || []).map(function (command) { return h('div', { key: command.request_id, style: { padding: 9, marginBottom: 6, color: command.status === 'uncertain' || command.status === 'rejected' ? C.accent : C.sub, border: '1px solid ' + C.border } },
              ({ queued: '已受理，等待执行器空闲', running: '命令执行中', uncertain: '执行结果待核对', rejected: '命令未执行' }[command.status] || command.status) + (command.error ? '：' + command.error : ''),
              ToolButton('查看对应运行', function () { setProjectOpen(false); viewRun(command.run_id); })); }),
            overview && overview.pending_actions.length ? overview.pending_actions.map(function (a) {
              return h('div', { key: a.run_id + ':' + a.node_id, style: { padding: 10, borderBottom: '1px solid ' + C.border } },
                h('span', null, a.name + ' · ' + statusLabel(a.status) + (a.question ? ' · ' + a.question : '')),
                ToolButton('打开', function () { setProjectOpen(false); openWorkspace(a.run_id, a.node_id); }));
            }) : h('p', { style: { color: C.sub } }, '当前没有待处理事项'),
            overview && (overview.historical_pending_actions || []).length ? h('details', { style: { marginTop: 12 } },
              h('summary', { style: { cursor: 'pointer', color: C.sub } }, '历史待办（' + (overview.unresolved_history_count || 0) + ' 项待归类，保留原记录）'),
              overview.historical_pending_actions.map(function (a) { return h('div', { key: a.run_id + ':' + a.node_id + ':' + a.status, style: { padding: 10, borderBottom: '1px solid ' + C.border } },
                h('span', null, a.name + ' · ' + (a.disposition === 'superseded' ? '已由后续运行替代' : '历史运行待办，未自动放弃')),
                ToolButton('查看历史', function () { setProjectOpen(false); openWorkspace(a.run_id, a.node_id); }),
                a.disposition !== 'superseded' && overview.current_run ? ToolButton('由当前工作运行替代', function () {
                  chooseProjectRun('supersede-run', a.run_id, { replacement_run_id: overview.current_run.run_id, reason: '使用者在项目总览指定由当前工作运行替代' });
                }) : null); })) : null,
            h('h3', null, '运行与成果'), overview ? overview.runs.map(function (r) {
              return h('div', { key: r.run_id, style: { padding: 10, borderBottom: '1px solid ' + C.border } }, friendlyRunName(r) + ' · ' + statusLabel(r.status),
                r.run_kind === 'workflow_proposal' ? ToolButton('审阅流程建议', function () { viewWorkflowProposal(r.run_id); }) : h('div', null,
                  ToolButton('查看成果', function () { setProjectOpen(false); openResults(r.run_id); }),
                  ToolButton('设为当前工作', function () { chooseProjectRun('set-work-run', r.run_id); }, false, (overview.current_run || {}).run_id === r.run_id),
                  r.status === 'completed' ? ToolButton('设为当前交付版', function () { chooseProjectRun('set-delivery-run', r.run_id); }, false, (overview.delivery_run || {}).run_id === r.run_id) : null),
                h('details', null, h('summary', { style: { color: C.faint, fontSize: 10, cursor: 'pointer' } }, '技术标识'), h('code', null, r.run_id)));
            }) : null,
            h('p', { style: { color: C.sub } }, run ? '下一步：查看待处理事项或审阅本次成果。' : '下一步：编辑流程、导入项目资料，然后运行。'))
            : h('div', null, h('h3', null, '开始第一个项目'), h('p', { style: { color: C.sub } }, '先体验一个真实AI任务，再替换为自己的资料。模型使用Harness账号。'),
              (workflows || []).filter(function (w) { return ['wc17-article','wc17-motion-video','wc17-file-task'].indexOf(w.id) >= 0; }).map(function (w) {
                return h('div', { key: w.id, style: { padding: 14, border: '1px solid ' + C.border, borderRadius: 8, marginBottom: 10 } },
                  h('b', null, w.name), h('p', { style: { color: C.sub, fontSize: 12 } }, w.description),
                  ToolButton('用此模板创建项目', function () { setProjectDraft({ name: '我的' + w.name, goal: w.id === 'wc17-motion-video' ? '制作一段30秒原创动态图形，介绍目标、修改、交付三个环节，有字幕和轻柔提示音。' : '根据项目资料整理清楚、可阅读的中文成果并给出来源。没有资料时使用共享合成示例体验，不补造信息。', delivery: w.id === 'wc17-motion-video' ? '成片、字幕、完整工程与制作说明；内容审阅后导出。' : '正文或整理文件及来源，内容审阅后导出。', start: 'template', template: w.id }); }, true));
              }), h('p', { style: { color: C.sub } }, '也可新建空白项目，自建AI模块或让AI建议流程。'))) : null,
        workflowProposal ? h('div', { role: 'dialog', 'aria-label': '审阅流程建议', style: { position: 'absolute', inset: 16, zIndex: 96, background: C.panel, padding: 20, overflow: 'auto', border: '1px solid ' + C.border, borderRadius: 12 } },
          h('h2', null, '审阅流程建议'), h('p', { style: { whiteSpace: 'pre-wrap' } }, workflowProposal.explanation),
          (workflowProposal.graph.nodes || []).map(function (n) { return h('div', { key: n.id, style: { padding: 10, marginBottom: 8, background: C.card } }, h('b', null, n.name || n.id), h('p', null, '模块：' + n.module), h('p', null, '输入：' + (n.requires || []).join('、')), h('p', null, '产物：' + (n.artifacts || []).join('、'))); }),
          (workflowProposal.errors || []).concat(workflowProposal.capability_gaps || []).map(function (text, i) { return h('p', { key: i, style: { color: C.accent } }, text); }),
          (workflowProposal.normalizations || []).map(function (text, i) { return h('p', { key: 'normalized-' + i, style: { color: C.sub } }, text); }),
          h('p', null, workflowProposal.notice),
          workflowProposal.can_apply ? ToolButton('写入项目画布（不执行）', applyWorkflowProposal, true) : null,
          ToolButton('修订建议', function () { var rid = workflowProposal.run_id; setWorkflowProposal(null); setProjectOpen(false); openWorkspace(rid, 'workflow_proposal'); }),
          ToolButton('关闭', function () { setWorkflowProposal(null); })) : null,
        h("style", null, "@keyframes wcPulse{0%,100%{box-shadow:0 0 0 0 rgba(255,176,32,0)}50%{box-shadow:0 0 0 6px rgba(255,176,32,.14)}}@keyframes wcSpin{to{transform:rotate(360deg)}}@keyframes wcFlow{to{stroke-dashoffset:-28}}@media(prefers-reduced-motion:reduce){[data-flow-active], [data-live-node]{animation:none!important}}"),
        h("div", { style: { display: "flex", alignItems: "center", gap: 7, padding: "9px 13px", borderBottom: "1px solid " + C.borderSoft, background: C.panel, flexWrap: "wrap" } },
          h("div", { style: { fontSize: 14.5, fontWeight: 800, marginRight: 4 } }, "创作画布 v0.17.0 RC2"),
          h("span", { style: { width: 7, height: 7, borderRadius: 99, background: dirty ? C.accent : C.teal, marginRight: 8 } }),
          ToolButton("项目", openProjects, !!project),
          ToolButton(presentation ? '退出全屏' : '全屏工作台', togglePresentation),
          project ? h('span', { style: { color: C.teal, fontSize: 12 } }, project.name) : null,
          ToolButton("工作流", openWorkflows),
          h("span", { style: { fontSize: 10.5, color: C.faint, maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, project ? '项目独立流程' : (activeWorkflow.name || "")),
          ToolButton("保存", save),
          run && run.status === "paused" ? ToolButton("继续", resumeRun, true)
            : run && run.status === 'queued' ? ToolButton('等待执行…', function () {}, true, true)
            : run && run.status === "running" ? ToolButton("运行中…", function () {}, true, true)
            : run && run.status === "pausing" ? ToolButton("正在暂停…", function () {}, true, true)
            : run && run.status === "stopping" ? ToolButton("正在停止…", function () {}, true, true)
            : run && run.status === "awaiting_input" ? ToolButton("等你回复", function () {}, true, true)
            : run && run.status === "awaiting_agent" ? ToolButton("等待贾维斯…", function () {}, true, true)
            : run && run.status === "awaiting_approval" ? ToolButton("等待审核…", function () {}, true, true)
            : ToolButton("运行", runWorkflow, true),
          run && run.status === "running" ? ToolButton("暂停", pauseRun) : null,
          run && runActive(run.status) ? ToolButton("停止", cancelRun) : null,
          ToolButton("AI 指令", openAi),
          ToolButton("新手引导", openGuide),
          ToolButton("知识库", openKb),
          ToolButton("历史", openHistory),
          ToolButton("会话树", function () { setTreeOpen(function (v) { return !v; }); }),
          ToolButton("任务与成果", function () { openResults((run || {}).run_id); }, true, !!project && !run),
          ToolButton("执行监控", function () { setMonitorOpen(function (v) { return !v; }); }, monitorOpen),
          ToolButton("导出", exportCanvas),
          ToolButton("导入", function () { if (importRef.current) importRef.current.click(); }),
          h("select", {
            onChange: function (e) { var v = e.target.value; e.target.value = ""; if (v) loadTemplate(v); },
            style: { padding: "6px 8px", borderRadius: 8, border: "1px solid " + C.border, background: C.card, color: C.text, fontSize: 11, marginRight: 6, maxWidth: 110 },
          }, h("option", { value: "" }, "模板…"), templates.map(function (t) { return h("option", { key: t.id, value: t.id }, t.name); })),
          h("select", {
            onChange: function (e) { var v = e.target.value; e.target.value = ""; if (v) restoreRevision(v); },
            style: { padding: "6px 8px", borderRadius: 8, border: "1px solid " + C.border, background: C.card, color: C.text, fontSize: 11, marginRight: 6, maxWidth: 130 },
          }, h("option", { value: "" }, "历史版本…"), revisions.slice(0, 20).map(function (r) { return h("option", { key: r.id, value: r.id }, (r.saved_at || "").slice(5) + " · " + r.nodes + "节点"); })),
          h("span", { style: { width: 1, height: 20, background: C.border, margin: "0 2px" } }),
          ToolButton("撤销", undo, false, !undoRef.current.length),
          ToolButton("重做", redo, false, !redoRef.current.length),
          ToolButton("自动布局", autoLayout),
          h("span", { style: { width: 1, height: 20, background: C.border, margin: "0 2px" } }),
          ToolButton("−", function () { zoomBy(0.85); }),
          h("span", { style: { fontFamily: "Consolas,monospace", fontSize: 10.5, color: C.sub, width: 46, textAlign: "center" } }, Math.round(view.k * 100) + "%"),
          ToolButton("+", function () { zoomBy(1.18); }),
          ToolButton("适应", fitView),
          h("label", { style: { fontSize: 10.5, color: C.sub, marginLeft: 4, cursor: "pointer", display: "flex", alignItems: "center", gap: 4 } },
            h("input", { type: "checkbox", checked: stopAtReview, onChange: function (e) { setStopAtReview(e.target.checked); } }), "审核后暂停（可选）"),
          h("div", { style: { flex: 1 } }),
          run ? h("span", {
            style: { fontFamily: "Consolas,monospace", fontSize: 10.5, color: statusColor(run.status), border: "1px solid " + statusColor(run.status), borderRadius: 99, padding: "3px 9px" },
          }, friendlyRunName(run) + " · " + monitor.title) : null
        ),
        viewingRun ? h("div", { style: { display: "flex", gap: 12, alignItems: "center", padding: "9px 14px", background: "#132b30", color: C.teal, fontSize: 12 } },
          h("strong", null, "运行查看模式 · 只读快照"), h("span", { style: { flex: 1, fontFamily: "Consolas,monospace" } }, viewingRun),
          ToolButton("返回编辑", exitRunView), ToolButton("查看成果", function () { openResults(viewingRun); })) : null,
        msg ? h("div", { style: { padding: "5px 14px", fontSize: 11, color: C.accent, background: C.panel, borderBottom: "1px solid " + C.borderSoft } }, msg) : null,
        h("div", { "data-execution-overview": monitor.mode, role: "status", style: { padding: "11px 15px", background: monitor.mode === "running" ? "#171c27" : C.panel, borderBottom: "1px solid " + C.border, color: C.text } },
          h("div", { style: { display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" } },
            h("strong", { style: { color: monitor.mode === "completed" ? C.teal : monitor.connected ? C.accent : C.danger, fontSize: 14 } }, monitor.title),
            h("span", { style: { fontSize: 12 } }, run ? friendlyRunName(run) + " · " + monitor.complete + "/" + monitor.total + " 步已结束" + (monitor.skipped ? "（含 " + monitor.skipped + " 步跳过）" : "") : (graph.nodes || []).length + " 个步骤待执行，点「运行」开始"),
            h("span", { style: { fontSize: 11, color: C.sub, marginLeft: "auto" } }, monitor.connected ? "状态已同步 · " + Math.max(0, Math.floor(Date.now()/1000-runtimeSync)) + "秒前" : "未收到最新状态")),
          run ? h("div", { role: "progressbar", "aria-valuemin": 0, "aria-valuemax": monitor.total, "aria-valuenow": monitor.complete, "aria-label": "已完成或跳过的步骤", style: { height: 5, background: C.border, borderRadius: 9, marginTop: 9, overflow: "hidden" } }, h("div", { style: { width: monitor.percent + "%", height: "100%", background: C.teal, transition: "width 250ms ease-out" } })) : null,
          monitor.active.length ? h("div", { style: { fontSize: 11, marginTop: 8, color: C.sub } }, monitor.active.map(function (n) {
            var live = n.live || {}, confirmed = monitor.freshIds.indexOf(n.id) !== -1;
            return (n.name || n.id) + " · " + (confirmed ? phaseLabel(live.phase, live.tool_name) : "执行状态待确认") + (live.event_count ? " · 已收到 " + live.event_count + " 个执行事件" : "");
          }).join("　｜　")) : null,
          run && !matchingRun ? h("div", { style: { color: C.accent, fontSize: 11, marginTop: 7 } }, "当前画布与本次运行快照不同；请在执行监控中查看实际运行步骤。") : null,
          h("div", { style: { marginTop: 7, color: C.faint, fontSize: 10 } }, "绿色 已完成　橙色 正在执行　黄色 等你回复　红色 遇到问题　灰色 待执行；进度按已结束步骤计算")),
        run && run.status === "awaiting_input" ? h("div", {
          "data-agent-question": "1",
          style: { display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", background: "#1b1407", color: C.accent, borderBottom: "1px solid #4a3510" },
        },
          h("strong", null, "DeepSeek 需要你补充信息"),
          h("span", { style: { flex: 1, fontSize: 12 } }, (run.nodes || []).filter(function (n) { return n.status === "awaiting_input"; }).map(function (n) { return n.question || n.detail; }).join("；")),
          h("button", { onClick: replyToAgent, disabled: busy, style: { padding: "5px 10px", border: "1px solid " + C.accent, background: C.accent, color: "#181207", borderRadius: 6, cursor: "pointer" } }, "回复并继续")
        ) : null,
        run && run.status === "awaiting_agent" ? h("div", {
          style: { display: "flex", alignItems: "center", gap: 10, padding: "9px 14px", background: "#1b1407", borderBottom: "1px solid #4a3510", color: C.accent, fontSize: 12 },
        },
          h("span", { style: { fontSize: 14 } }, "🤖"),
          h("span", { style: { fontWeight: 700 } }, "等待贾维斯执行"),
          h("span", { style: { color: "#e8d9b0" } },
            ((run.nodes || []).filter(function (n) { return n.status === "awaiting_agent"; }).map(function (n) { return n.name; }).join("、") || "当前任务")
            + " · 完成后会自动继续"),
          h("div", { style: { marginLeft: "auto", display: "flex", gap: 6 } },
            h("button", { onClick: openTaskForCurrent, style: { padding: "3px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, cursor: "pointer", fontSize: 10 } }, "查看任务卡"),
            h("button", { onClick: handoffTask, style: { padding: "3px 8px", borderRadius: 6, border: "1px solid " + C.accent, background: C.accent, color: "#181207", cursor: "pointer", fontSize: 10, fontWeight: 700 } }, "交给贾维斯"))
        ) : null,
        run && (run.status === "paused" || run.status === "pausing") ? h("div", {
          "data-run-paused": "1",
          style: { display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", background: "#0f1d22", color: C.teal, borderBottom: "1px solid #1d3a3a", fontSize: 12 },
        },
          h("strong", null, run.status === "paused" ? "已暂停" : "正在暂停"),
          h("span", { style: { flex: 1, color: C.sub } }, run.status === "paused" ? "进度已保留；可以继续执行，也可以停止后改天再做。" : "当前步骤结束后停下，进度会保留。"),
          run.status === "paused" ? h("button", { onClick: resumeRun, disabled: busy, style: { padding: "5px 10px", border: "1px solid " + C.teal, background: C.teal, color: "#06211d", borderRadius: 6, cursor: "pointer", fontWeight: 700 } }, "继续") : null
        ) : null,

        h("div", { style: { flex: 1, display: "flex", minHeight: 0 } },
          h("div", { style: { width: 200, flexShrink: 0, borderRight: "1px solid " + C.borderSoft, background: C.panel, display: "flex", flexDirection: "column", minHeight: 0 } },
            h("div", { style: { padding: "10px 10px 8px" } },
              h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between" } },
                h("div", { style: { fontSize: 11.5, fontWeight: 700 } }, "模块库"),
                h("div", { style: { fontSize: 9.5, color: C.faint, fontFamily: "Consolas,monospace" } }, modules.filter(function (m) { return !m.archived; }).length + " 个")),
              h("button", { "data-new-module": "1", disabled: busy, onClick: function () { setModuleError(""); setModuleForm({ name: "", instructions: "", checklist: "", inputs: "", outputs: "", timeout: "", editingId: "" }); },
                style: { width: "100%", marginTop: 9, padding: "9px 8px", border: "1px solid " + C.accent, borderRadius: 7, background: C.accent, color: C.bg, fontWeight: 700, cursor: "pointer" } }, "+ 新建模块"),
              h("input", {
                value: search, onChange: function (e) { setSearch(e.target.value); }, placeholder: "搜索模块…",
                style: { width: "100%", boxSizing: "border-box", marginTop: 8, padding: "6px 8px", borderRadius: 7, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 11, outline: "none" },
              })),
            h("div", { style: { flex: 1, overflow: "auto", padding: "0 10px 14px" } },
              filtered.length === 0 ? h("div", { style: { fontSize: 11, color: C.faint, padding: "8px 2px" } }, "没有匹配的模块") : null,
              categories.map(function (cat) {
                var list = filtered.filter(function (m) { return (m.category || "其他") === cat; });
                if (!list.length) return null;
                return h("div", { key: cat },
                  h("div", { style: { display: "flex", alignItems: "center", gap: 6, margin: "10px 0 5px" } },
                    h("span", { style: { width: 7, height: 7, borderRadius: 2, background: catColor(cat) } }),
                    h("span", { style: { fontSize: 9.5, color: C.faint, letterSpacing: 1 } }, cat.toUpperCase())),
                  list.map(function (m) {
                    return h("div", {
                      key: m.id, "data-module": m.id, draggable: true,
                      onDragStart: function (e) { e.dataTransfer.setData("text/module", m.id); e.dataTransfer.effectAllowed = "copy"; },
                      onClick: function () { addFromPalette(m); }, title: (m.plain && m.plain["一句话"]) || m.description,
                      style: { padding: "7px 8px", marginBottom: 5, borderRadius: 8, cursor: "grab", background: C.card, border: "1px solid " + C.borderSoft, borderLeft: "2px solid " + catColor(cat) },
                      onMouseEnter: function (e) { e.currentTarget.style.background = C.cardHover; },
                      onMouseLeave: function (e) { e.currentTarget.style.background = C.card; },
                    },
                      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 } },
                        h("div", { style: { fontSize: 11.5, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, m.name),
                        h("button", {
                          "data-module-help": m.id,
                          onClick: function (e) { e.stopPropagation(); setHelpModule(m); setSelectedNode(null); setSelectedEdge(null); },
                          title: "查看模块说明",
                          style: { flexShrink: 0, width: 18, height: 18, borderRadius: 99, border: "1px solid " + C.border, background: C.panel, color: C.sub, fontSize: 10, cursor: "pointer", lineHeight: "16px", padding: 0 },
                        }, "?"),
                        h("button", { "data-delete-module": m.id, title: "从模块库删除", disabled: busy,
                          onClick: function (e) { e.stopPropagation(); deleteLibraryModule(m); },
                          style: { border: "none", background: "transparent", color: C.danger, padding: "2px", cursor: "pointer", fontSize: 10, flexShrink: 0 } }, "删除")),
                      h("div", { style: { fontFamily: "Consolas,monospace", fontSize: 9, color: C.faint, marginTop: 2 } }, m.id),
                      (m.skills || []).length ? h("div", { style: { color: C.teal, fontSize: 9, marginTop: 4 } }, "已绑定 " + m.skills.length + " 个 skill") : null);
                  })
                );
              })),
            h("div", { style: { borderTop: "1px solid " + C.borderSoft, padding: "9px 11px", fontSize: 10, color: C.faint, lineHeight: 1.7 } },
              "点击/拖拽加入画布；拖右侧圆点到另一节点左侧圆点连线。",
              h("div", { style: { marginTop: 4 } }, "点模块右侧 ? 查看它做什么/吃什么/产出什么。"),
              h("div", { style: { marginTop: 6, color: C.sub } }, "AI：对话里说「加一个发布模块」即可"))),

          h("div", {
            ref: viewportRef,
            "data-viewport": "1",
            onPointerDown: onCanvasPointerDown, onPointerMove: onCanvasPointerMove, onPointerUp: onCanvasPointerUp,
            onDragOver: function (e) { e.preventDefault(); }, onDrop: onDrop,
            style: {
              flex: 1, position: "relative", overflow: "hidden",
              backgroundImage: "radial-gradient(" + C.grid + " 1px, transparent 1px)", backgroundSize: "26px 26px", touchAction: "none",
            },
          },
            h("div", {
              "data-world": "1",
              onPointerDown: onCanvasPointerDown, onPointerMove: onCanvasPointerMove, onPointerUp: onCanvasPointerUp,
              style: {
                position: "absolute", left: 0, top: 0,
                transform: "translate(" + view.x + "px," + view.y + "px) scale(" + view.k + ")",
                transformOrigin: "0 0", width: 2600, height: 1400,
              },
            },
              h("svg", {
                width: 2600, height: 1400,
                onPointerDown: onCanvasPointerDown, onPointerMove: onCanvasPointerMove, onPointerUp: onCanvasPointerUp,
                style: { position: "absolute", left: 0, top: 0, overflow: "visible" },
              },
                h("defs", null,
                  h("marker", { id: "wc-arrow", markerWidth: 10, markerHeight: 10, refX: 8, refY: 5, orient: "auto" }, h("path", { d: "M0,0 L10,5 L0,10 z", fill: "#4a5f78" })),
                  h("marker", { id: "wc-arrow-done", markerWidth: 10, markerHeight: 10, refX: 8, refY: 5, orient: "auto" }, h("path", { d: "M0,0 L10,5 L0,10 z", fill: C.teal })),
                  h("marker", { id: "wc-arrow-sel", markerWidth: 10, markerHeight: 10, refX: 8, refY: 5, orient: "auto" }, h("path", { d: "M0,0 L10,5 L0,10 z", fill: C.accent }))),
                (graph.edges || []).map(function (e) {
                  var a = nodeById(e.from), b = nodeById(e.to);
                  if (!a || !b) return null;
                  var sel = selectedEdge === edgeKey(e);
                  var sourceStatus = matchingRun && runMap[e.from] && runMap[e.from].status;
                  var targetStatus = matchingRun && runMap[e.to] && runMap[e.to].status;
                  var flowing = (sourceStatus === "passed" || sourceStatus === "skipped") && targetStatus === "running" && monitor.freshIds.indexOf(e.to) !== -1;
                  var delivered = (sourceStatus === "passed" || sourceStatus === "skipped") && (targetStatus === "passed" || targetStatus === "skipped");
                  return h("g", { key: edgeKey(e) },
                    h("path", {
                      "data-edge": edgeKey(e),
                      d: bezierPath(a, b), stroke: "transparent", strokeWidth: 18, fill: "none",
                      style: { pointerEvents: "stroke", cursor: "pointer" },
                      onClick: function (ev) { ev.stopPropagation(); setSelectedEdge(edgeKey(e)); setSelectedNode(null); },
                    }),
                    h("path", { "data-flow-active": flowing ? "1" : undefined, "data-edge-status": flowing ? "active" : delivered ? "completed" : "pending", d: bezierPath(a, b), stroke: sel || flowing ? C.accent : delivered ? C.teal : "#34465a", strokeWidth: flowing ? 3 : sel ? 2.6 : 2, strokeDasharray: flowing ? "9 5" : undefined, style: { animation: flowing ? "wcFlow 900ms linear infinite" : "none" }, fill: "none", markerEnd: "url(#" + (sel || flowing ? "wc-arrow-sel" : delivered ? "wc-arrow-done" : "wc-arrow") + ")" }));
                }),
                connectFrom ? (function () {
                  var a = nodeById(connectFrom);
                  if (!a) return null;
                  return h("path", {
                    d: bezierPath(a, { x: mouseWorld.x, y: mouseWorld.y - NODE_H / 2 }),
                    stroke: C.accent, strokeWidth: 2, strokeDasharray: "6 5", fill: "none", opacity: 0.9,
                  });
                })() : null),

              (graph.nodes || []).map(function (n) {
                var mod = moduleById(n.module);
                var cat = mod ? (mod.category || "其他") : "其他";
                var sel = selectedNode === n.id;
                var from = connectFrom === n.id;
                var st = matchingRun && runMap[n.id] ? runMap[n.id].status : "";
                var liveNode = matchingRun && monitor.freshIds.indexOf(n.id) !== -1;
                return h("div", {
                  key: n.id,
                  "data-node": n.id,
                  "data-node-status": st || "idle", "data-live-node": liveNode ? "1" : undefined,
                  onDoubleClick: function (e) { e.stopPropagation(); var selected = stateRef.current.run; if (selected && selected.run_id) openWorkspace(selected.run_id, n.id); },
                  onPointerDown: function (e) { if (!selectedRunRef.current) onNodePointerDown(e, n); },
                  onPointerMove: onNodePointerMove,
                  onPointerUp: onNodePointerUp,
                  onClick: function (e) {
                    e.stopPropagation();
                    if (connectingRef.current) { addEdge(connectingRef.current, n.id); return; }
                    setSelectedNode(n.id); setSelectedEdge(null); setHelpModule(null);
                    if (st && !dragMovedRef.current) {
                      var selected = stateRef.current.run;
                      if (selected && selected.run_id) openWorkspace(selected.run_id, n.id);
                    }
                  },
                  style: {
                    position: "absolute", left: n.x, top: n.y, width: NODE_W, height: NODE_H, boxSizing: "border-box",
                    borderRadius: 12, padding: "10px 12px",
                    background: "linear-gradient(180deg, #141e2c 0%, #111a26 100%)",
                    border: "1.5px solid " + (from ? C.accent : sel ? C.teal : st ? statusColor(st) : C.border),
                    boxShadow: sel ? "0 0 0 3px rgba(61,214,196,.10), 0 10px 24px rgba(0,0,0,.35)" : "0 6px 18px rgba(0,0,0,.25)",
                    cursor: "grab", userSelect: "none", zIndex: sel || from ? 4 : 2,
                    animation: liveNode ? "wcPulse 1.6s ease-in-out infinite" : "none",
                  },
                },
                  h("div", { style: { display: "flex", alignItems: "center", gap: 7 } },
                    h("span", { style: { width: 8, height: 8, borderRadius: 2, background: catColor(cat), flexShrink: 0 } }),
                    h("span", { style: { fontSize: 12.5, fontWeight: 700, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, n.name || (mod ? mod.name : n.module)),
                    h("div", { style: { flex: 1 } }),
                    h("span", { style: { fontSize: 9, color: st === "running" && !liveNode ? C.faint : statusColor(st), border: "1px solid " + statusColor(st), borderRadius: 99, padding: "1px 5px", flexShrink: 0 } }, st === "running" && !liveNode ? "待确认" : statusLabel(st))),
                  h("div", { style: { fontFamily: "Consolas,monospace", fontSize: 9, color: C.faint, marginTop: 7 } }, n.id),
                  h("div", { style: { fontSize: 9.5, color: C.sub, marginTop: 4, height: 26, overflow: "hidden", lineHeight: 1.4 } },
                    liveNode ? phaseLabel(runMap[n.id].live && runMap[n.id].live.phase, runMap[n.id].live && runMap[n.id].live.tool_name) : (st === "awaiting_input" ? "需要你的回复后继续" : (n.agent_prompt || (mod ? mod.description : "") || "").slice(0, 44))),
                  st ? h("span", {
                    onClick: function (e) { e.stopPropagation(); var selected = stateRef.current.run; if (selected && selected.run_id) openWorkspace(selected.run_id, n.id); },
                    style: { position: "absolute", right: 10, bottom: 7, fontSize: 9, color: C.teal, cursor: "pointer", fontWeight: 700 },
                  }, "会话 ›") : null,
                  h("div", {
                    "data-port": "in", "data-node-id": n.id,
                    title: "输入", onClick: function (e) { e.stopPropagation(); if (connectingRef.current) addEdge(connectingRef.current, n.id); },
                    style: { position: "absolute", left: -6, top: NODE_H / 2 - 6, width: 12, height: 12, borderRadius: 99, background: C.card, border: "2px solid " + (connectFrom ? C.accent : "#4a5f78"), cursor: "crosshair" },
                  }),
                  h("div", {
                    "data-port": "out", "data-node-id": n.id,
                    title: "输出：拖到另一个节点的输入点",
                    onPointerDown: function (e) {
                      e.stopPropagation();
                      setConnectFrom(n.id);
                      var r = viewportRef.current.getBoundingClientRect();
                      var v = stateRef.current.view;
                      setMouseWorld({ x: (e.clientX - r.left - v.x) / v.k, y: (e.clientY - r.top - v.y) / v.k });
                    },
                    onClick: function (e) {
                      e.stopPropagation();
                      if (!connectingRef.current) {
                        setConnectFrom(n.id);
                        var r = viewportRef.current.getBoundingClientRect();
                        var v = stateRef.current.view;
                        setMouseWorld({ x: (e.clientX - r.left - v.x) / v.k, y: (e.clientY - r.top - v.y) / v.k });
                      }
                    },
                    style: { position: "absolute", right: -6, top: NODE_H / 2 - 6, width: 12, height: 12, borderRadius: 99, background: connectFrom ? C.accent : "#4a5f78", border: "2px solid " + C.card, cursor: "crosshair" },
                  })
                );
              })
            ),

            (graph.nodes || []).length === 0 ? h("div", {
              style: { position: "absolute", left: "50%", top: "50%", transform: "translate(-50%,-50%)", textAlign: "center", color: C.faint, fontSize: 12, lineHeight: 1.9 },
            },
              h("div", { style: { fontSize: 15, color: C.sub, fontWeight: 700 } }, "不知道从哪开始？"),
              h("div", null, "点左侧模块加入画布，拖右侧圆点到另一个节点左侧圆点连线"),
              h("div", null, "或者先载入现成流程："),
              h("div", { style: { marginTop: 10, display: "flex", gap: 8, justifyContent: "center" } },
                h("button", { onClick: function () { loadTemplate("standard-creation"); }, style: { padding: "7px 12px", borderRadius: 8, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontWeight: 700, cursor: "pointer", fontSize: 11.5 } }, "载入标准创作流"),
                h("button", { onClick: openGuide, style: { padding: "7px 12px", borderRadius: 8, border: "1px solid " + C.border, background: C.card, color: C.sub, cursor: "pointer", fontSize: 11.5 } }, "看新手引导"))) : null,

            (graph.nodes || []).length ? h("div", {
              style: { position: "absolute", right: 14, bottom: 14, width: 190, height: 116, background: "#0a1019", border: "1px solid #1b2838", borderRadius: 9, overflow: "hidden", boxShadow: "0 8px 20px rgba(0,0,0,.35)" },
            },
              h("div", { style: { position: "absolute", left: 8, top: 5, fontSize: 8, letterSpacing: 1, color: C.faint, fontFamily: "Consolas,monospace" } }, "MINIMAP"),
              h("svg", { width: 190, height: 116 },
                h("rect", { x: 0, y: 0, width: 190, height: 116, fill: "#0a1019" }),
                (function () {
                  var g = graph.nodes;
                  var minX = Math.min.apply(null, g.map(function (n) { return n.x; }));
                  var minY = Math.min.apply(null, g.map(function (n) { return n.y; }));
                  var maxX = Math.max.apply(null, g.map(function (n) { return n.x + NODE_W; }));
                  var maxY = Math.max.apply(null, g.map(function (n) { return n.y + NODE_H; }));
                  var s = Math.min(160 / Math.max(1, maxX - minX), 90 / Math.max(1, maxY - minY)) * 0.85;
                  var ox = 15 - minX * s, oy = 13 - minY * s;
                  var rects = g.map(function (n) {
                    return h("rect", { key: n.id, x: ox + n.x * s, y: oy + n.y * s, width: Math.max(3, NODE_W * s), height: Math.max(2, NODE_H * s), rx: 1.5, fill: "#2b3d52" });
                  });
                  var vp = viewportRef.current;
                  var vw = vp ? vp.clientWidth : 800, vh = vp ? vp.clientHeight : 500;
                  var rw = Math.max(6, vw / view.k * s), rh = Math.max(6, vh / view.k * s);
                  var rx = -view.x / view.k * s + ox, ry = -view.y / view.k * s + oy;
                  if (rw < 176 && rh < 102) {
                    rects.push(h("rect", {
                      key: "vp", x: rx, y: ry, width: rw, height: rh,
                      fill: "rgba(255,176,32,.05)", stroke: "rgba(255,176,32,.25)", strokeWidth: 1, rx: 2,
                    }));
                  }
                  return rects;
                })()
              )) : null
          ),

          h("input", { ref: importRef, type: "file", accept: "application/json,.json", onChange: importCanvas, style: { display: "none" } }),
          monitorOpen ? h("aside", { "data-execution-monitor": "1", style: { width: 330, flexShrink: 0, borderLeft: "1px solid " + C.border, background: C.panel, overflow: "auto", padding: 15, boxSizing: "border-box" } },
            h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } }, h("strong", null, "执行监控"), h("button", { onClick: function () { setMonitorOpen(false); }, style: { background: "transparent", border: "none", color: C.sub, cursor: "pointer" } }, "返回设置")),
            h("p", { style: { fontSize: 12, color: monitor.connected ? C.teal : C.danger } }, monitor.title),
            h("div", { style: { fontSize: 10, color: C.faint, overflowWrap: "anywhere", marginBottom: 12 } }, run ? (run.episode || "") + " · " + (run.run_id || "等待确认启动") : "当前没有执行任务"),
            monitor.active.map(function (n) { var live = n.live || {}, confirmed = monitor.freshIds.indexOf(n.id) !== -1;
              return h("div", { key: n.id, style: { padding: 12, marginBottom: 10, borderRadius: 9, background: C.card, border: "1px solid " + (confirmed ? C.accent : C.border) } },
                h("strong", { style: { fontSize: 12 } }, n.name || n.id),
                h("div", { style: { fontSize: 11, color: C.accent, marginTop: 7 } }, confirmed ? phaseLabel(live.phase, live.tool_name) : "当前执行状态未确认"),
                h("div", { style: { fontSize: 10, color: C.sub, marginTop: 7, lineHeight: 1.8 } }, "执行事件 " + (live.event_count || 0) + " · 工具调用 " + (live.tool_calls || 0), h("br"),
                  "本步骤已用时：" + (n.started_at_epoch ? Math.max(0, Math.floor(Date.now()/1000-n.started_at_epoch)) + "秒" : "记录未同步"), h("br"),
                  "执行器心跳：" + (live.heartbeat_at ? Math.max(0, Math.floor(Date.now()/1000-live.heartbeat_at)) + "秒前" : "尚未收到"), h("br"),
                  "最近执行事件：" + (live.last_event_at ? Math.max(0, Math.floor(Date.now()/1000-live.last_event_at)) + "秒前" : "尚未收到")),
                h("button", { onClick: function () { var node = nodeById(n.id); if (!node || !matchingRun) { setMsg("该步骤属于运行快照，请以监控中的状态为准"); return; } setSelectedNode(n.id); var rect = viewportRef.current.getBoundingClientRect(); setView(function (v) { return { k: v.k, x: rect.width/2-(node.x+NODE_W/2)*v.k, y: rect.height/2-(node.y+NODE_H/2)*v.k }; }); }, style: { marginTop: 9, padding: "6px 9px", background: C.bg, color: C.text, border: "1px solid " + C.border, borderRadius: 6, cursor: "pointer" } }, "定位此步骤"));
            }),
            h("div", { style: { fontSize: 11, fontWeight: 700, margin: "16px 0 9px" } }, "流程进度 · " + monitor.complete + "/" + monitor.total),
            ((run && run.nodes) || []).map(function (n, index) { return h("div", { key: n.id, "data-monitor-node": n.id, style: { display: "flex", alignItems: "center", gap: 7, padding: "8px 0", borderBottom: "1px solid " + C.borderSoft, fontSize: 11 } },
              h("span", { style: { width: 18, color: statusColor(n.status) } }, n.status === "passed" ? "✓" : n.status === "running" ? "●" : n.status === "awaiting_input" ? "?" : index + 1),
              h("span", { style: { flex: 1 } }, n.name || n.id), h("span", { style: { color: statusColor(n.status), fontSize: 10 } }, n.status === "running" && monitor.freshIds.indexOf(n.id) === -1 ? "待确认" : statusLabel(n.status))); }),
            h("div", { style: { fontSize: 11, fontWeight: 700, margin: "18px 0 9px" } }, "最近执行活动"),
            activity.length ? activity.slice(0, 10).map(function (a, i) { return h("div", { key: i, style: { padding: "7px 0", fontSize: 10, color: C.sub, borderBottom: "1px solid " + C.borderSoft } }, h("span", { style: { color: C.faint } }, new Date(a.at*1000).toLocaleTimeString()), " · " + a.name, h("div", { style: { marginTop: 4 } }, phaseLabel(a.phase, a.tool_name))); }) : h("div", { style: { fontSize: 11, color: C.faint } }, "开始执行后显示真实阶段变化"),
            h("p", { style: { fontSize: 10, color: C.faint, lineHeight: 1.7 } }, "心跳表示执行器仍在响应；执行事件来自 Harness。这里不展示模型私有思考或工具参数。")
          ) : h("div", { style: { width: 240, flexShrink: 0, borderLeft: "1px solid " + C.borderSoft, background: C.panel, overflow: "auto", padding: "12px 12px 20px" } },
            h("div", { style: { fontSize: 11.5, fontWeight: 700, marginBottom: 8 } }, helpModule ? "模块说明" : "节点设置"),
            (!helpModule && run && run.run_id && selectedNode) ? h("div", { style: { display: "flex", gap: 6, marginBottom: 10 } },
              h("button", { onClick: function () { openWorkspace(run.run_id, selectedNode); }, style: { flex: 1, padding: "6px 8px", borderRadius: 6, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontSize: 10.5, fontWeight: 700, cursor: "pointer" } }, "打开模块工作区"),
              h("button", { onClick: function () { openWorkspace(run.run_id, selectedNode); setLineageOpen(true); }, style: { padding: "6px 8px", borderRadius: 6, border: "1px solid " + C.teal, background: C.card, color: C.teal, fontSize: 10.5, cursor: "pointer" } }, "血缘")
            ) : null,
            (!helpModule && run && run.run_id && selectedNode) ? h("div", { style: { fontSize: 9.5, color: C.faint, marginBottom: 8 } }, "也可以双击节点，或点节点右下角的「会话 ›」") : null,
            helpModule ? h("div", null,
              h("div", { style: { display: "flex", alignItems: "center", gap: 8 } },
                h("span", { style: { width: 8, height: 8, borderRadius: 2, background: catColor(helpModule.category || "其他") } }),
                h("div", { style: { fontSize: 13, fontWeight: 700 } }, helpModule.name)),
              h("div", { style: { fontFamily: "Consolas,monospace", fontSize: 10, color: C.faint, marginTop: 4 } }, helpModule.id + " · " + (helpModule.category || "其他")),
              (function () {
                var pl = helpModule.plain || {};
                function row(label, value) {
                  return h("div", { style: { marginTop: 8 } },
                    h("div", { style: { fontSize: 10, color: C.faint, marginBottom: 3 } }, label),
                    h("div", { style: { fontSize: 11, color: C.sub, lineHeight: 1.6 } }, value));
                }
                return h("div", null,
                  row("一句话", pl["一句话"] || helpModule.description || ""),
                  row("什么时候用", pl["什么时候用"] || "-"),
                  row("需要准备", pl["需要准备"] || "-"),
                  row("产出什么", pl["产出什么"] || "-"),
                  row("新手提示", pl["新手提示"] || "-"),
                  h("div", { style: { marginTop: 12, padding: "9px 10px", borderRadius: 8, background: C.card, border: "1px solid " + C.borderSoft } },
                    h("div", { style: { fontSize: 10, color: C.faint, marginBottom: 4 } }, "技术信息"),
                    h("div", { style: { fontSize: 10, fontFamily: "Consolas,monospace", color: C.faint, lineHeight: 1.7 } },
                      "输入: " + ((helpModule.requires || []).join(", ") || "无"),
                      h("br"),
                      "输出: " + ((helpModule.artifacts || []).join(", ") || "无"),
                      h("br"),
                      "执行器: " + (helpModule.executor || "-"))));
              })(),
              h("div", { style: { marginTop: 12, display: "flex", gap: 6 } },
                h("button", { onClick: function () { addFromPalette(helpModule); setHelpModule(null); }, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid " + C.accent, background: C.accent, color: "#181207", cursor: "pointer", fontSize: 11, fontWeight: 700 } }, "添加到画布"),
                helpModule.executor === 'agent' ? ToolButton('编辑模块定义', function () { editLibraryModule(helpModule); }) : null,
                h("button", { onClick: function () { setHelpModule(null); }, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid " + C.border, background: C.card, color: C.sub, cursor: "pointer", fontSize: 11 } }, "关闭")))
              : selNode ? h("div", null,
              h("div", { style: { display: "flex", alignItems: "center", gap: 8 } },
                h("span", { style: { width: 8, height: 8, borderRadius: 2, background: catColor(selModule ? (selModule.category || "其他") : "其他") } }),
                h("div", { style: { fontSize: 13, fontWeight: 700 } }, selModule ? selModule.name : selNode.module)),
              h("div", { style: { fontFamily: "Consolas,monospace", fontSize: 10, color: C.faint, marginTop: 4 } }, selNode.id + " · " + selNode.module),
              h("div", { style: { marginTop: 10, fontSize: 10, color: C.faint } }, "任务名称"),
              h("input", {
                value: selNode.name || (selModule ? selModule.name : ""),
                onChange: function (e) { setNodeText(selNode.id, "name", e.target.value); },
                style: { width: "100%", boxSizing: "border-box", marginTop: 4, padding: "6px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 11, outline: "none" },
              }),
              h("div", { "data-skill-bindings": selectedNode, style: { marginTop: 12, padding: 9, background: C.card, border: "1px solid " + C.border, borderRadius: 7 } },
                h("b", { style: { fontSize: 11 } }, "执行技能（GitHub Skills）"),
                h("div", { style: { fontSize: 9, color: C.sub, marginTop: 5 } }, "运行时加载技能文件与参考资料，按本步骤输入输出执行。"),
                skillCatalog.filter(function (skill) { return (Array.isArray(selNode.skills) ? selNode.skills : ((selModule && selModule.skills) || [])).indexOf(skill.id) !== -1; }).map(function (skill) {
                  return h("div", { key: skill.id, style: { marginTop: 7, fontSize: 10 } }, h("b", null, skill.name), " · " + (skill.installed ? "已安装" : "缺文件"),
                    h("a", { href: /^https:\/\/github\.com\//.test(skill.source_url || "") ? skill.source_url : undefined, target: "_blank", rel: "noreferrer", style: { display: "block", color: C.teal, fontSize: 9, marginTop: 3 } }, "GitHub 来源 · " + String(skill.commit || "").slice(0, 10)));
                }),
                h("details", { style: { marginTop: 9 } }, h("summary", { style: { fontSize: 10, cursor: "pointer", color: C.sub } }, "调整技能绑定"), skillCatalog.map(function (skill) {
                  var bindings = Array.isArray(selNode.skills) ? selNode.skills : ((selModule && selModule.skills) || []);
                  return h("div", { key: skill.id, style: { marginTop: 6, fontSize: 10 } },
                    h("label", { style: { display: "flex", alignItems: "center", gap: 4 } },
                      h("input", { type: "checkbox", checked: bindings.indexOf(skill.id) !== -1, onChange: function (e) {
                        var next = bindings.filter(function (id) { return id !== skill.id; }); if (e.target.checked) next.push(skill.id);
                        setNodeList(selectedNode, "skills", next.join("\n"));
                      } }), skill.name || skill.id, skill.installed ? " · 已安装" : " · 缺文件"),
                    bindings.indexOf(skill.id) !== -1 ? h("a", { href: /^https:\/\/github\.com\//.test(skill.source_url || "") ? skill.source_url : undefined, target: "_blank", rel: "noreferrer", style: { color: C.teal, fontSize: 9, marginLeft: 19 } }, "GitHub 来源 · " + String(skill.commit || "").slice(0, 10)) : null);
                }))),
              h("div", { style: { marginTop: 8, fontSize: 10, color: C.faint } }, "这一步要贾维斯做什么（任务说明）"),
              h("textarea", {
                value: selNode.agent_prompt || (selModule ? selModule.agent_prompt : ""),
                onChange: function (e) { setNodeText(selNode.id, "agent_prompt", e.target.value); },
                rows: 6,
                placeholder: "写清楚这一步的目标、做法、交付物和注意事项…",
                style: { width: "100%", boxSizing: "border-box", marginTop: 4, padding: 6, borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 10.5, lineHeight: 1.6, resize: "vertical" },
              }),
              h("div", { style: { marginTop: 6, fontSize: 10, color: C.faint } }, "验收清单（每行一项）"),
              h("textarea", {
                value: (selNode.checklist || (selModule ? selModule.checklist : []) || []).join("\n"),
                onChange: function (e) { setNodeChecklist(selNode.id, e.target.value); },
                rows: 4,
                style: { width: "100%", boxSizing: "border-box", marginTop: 4, padding: 6, borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 10.5, lineHeight: 1.6, resize: "vertical" },
              }),
              h("div", { style: { marginTop: 4, fontSize: 9.5, color: C.accent } }, "修改保存后，下次运行生效。当前流程遇到问题时，点「回复并继续」。"),
              h("div", { style: { marginTop: 12, padding: "9px 10px", borderRadius: 8, background: C.card, border: "1px solid " + C.borderSoft } },
                h("div", { style: { fontSize: 10, color: C.faint, marginBottom: 5 } }, "连线"),
                h("div", { style: { fontSize: 10.5, color: C.sub, lineHeight: 1.8 } },
                  "上游：" + ((graph.edges || []).filter(function (e) { return e.to === selNode.id; }).map(function (e) { return e.from; }).join("、") || "无"),
                  h("br"),
                  "下游：" + ((graph.edges || []).filter(function (e) { return e.from === selNode.id; }).map(function (e) { return e.to; }).join("、") || "无"))),
              h("div", { style: { marginTop: 14, fontSize: 11, fontWeight: 700 } }, "输入/输出文件（数据映射）"),
              h("div", { style: { marginTop: 4, fontSize: 9.5, color: C.faint, lineHeight: 1.6 } }, "任务之间靠文件交接：这里写这一步读哪些文件、写哪些文件；空=用模块默认。"),
              h("div", { style: { marginTop: 5, fontSize: 10, color: C.faint } }, "输入产物（每行一个路径；空=模块默认）"),
              h("textarea", {
                value: (selNode.requires || (selModule && selModule.requires) || []).join("\n"),
                onChange: function (e) { setNodeList(selNode.id, "requires", e.target.value); },
                rows: 2,
                style: { width: "100%", boxSizing: "border-box", marginTop: 4, padding: 6, borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 10, fontFamily: "Consolas,monospace", resize: "vertical" },
              }),
              h("div", { style: { marginTop: 6, fontSize: 10, color: C.faint } }, "输出产物（每行一个路径）"),
              (function () {
                var ancestors = {}, stack = [selNode.id];
                while (stack.length) { var current = stack.pop(); (graph.edges || []).forEach(function (edge) { if (edge.to === current && !ancestors[edge.from]) { ancestors[edge.from] = true; stack.push(edge.from); } }); }
                return (selNode.requires || (selModule && selModule.requires) || []).map(function (path) {
                  var producers = (graph.nodes || []).filter(function (n) { var definition = modules.filter(function (m) { return m.id === n.module; })[0] || {}; return ancestors[n.id] && (n.artifacts || definition.artifacts || []).indexOf(path) !== -1; });
                  return h('label', { key: path, style: { display: 'block', margin: '6px 0', fontSize: 10, color: C.sub } }, path,
                    h('select', { 'aria-label': '输入来源 ' + path, value: (selNode.input_bindings || {})[path] || '', style: { width: '100%', padding: 5, color: C.text, background: C.bg }, onChange: function (e) { var bindings = Object.assign({}, selNode.input_bindings || {}); if (e.target.value) bindings[path] = e.target.value; else delete bindings[path]; setNodeText(selNode.id, 'input_bindings', bindings); } },
                      h('option', { value: '' }, producers.length === 1 ? '自动采用：' + (producers[0].name || producers[0].id) : producers.length > 1 ? '多个来源，请明确选择' : '按上游声明解析'),
                      producers.map(function (n) { return h('option', { key: n.id, value: n.id }, n.name || n.id); })));
                });
              })(),
              h("textarea", {
                value: (selNode.artifacts || (selModule && selModule.artifacts) || []).join("\n"),
                onChange: function (e) { setNodeList(selNode.id, "artifacts", e.target.value); },
                rows: 2,
                style: { width: "100%", boxSizing: "border-box", marginTop: 4, padding: 6, borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 10, fontFamily: "Consolas,monospace", resize: "vertical" },
              }),
              h("label", { style: { marginTop: 8, display: "flex", alignItems: "center", gap: 6, fontSize: 10.5, color: C.sub, cursor: "pointer" } },
                h("input", { type: "checkbox", checked: selNode.on_error === "continue", onChange: function (e) { setNodeError(selNode.id, e.target.checked); } }),
                "失败后继续其他分支"),
              h('div', { style: { marginTop: 10, fontSize: 11, fontWeight: 700 } }, '交付与审阅'),
              (selNode.artifacts || (selModule && selModule.artifacts) || []).map(function (path) {
                var policies = selNode.deliveries || (selModule && selModule.deliveries) || {}, policy = policies[path] || {};
                function update(key, value) { var next = Object.assign({}, policies); next[path] = Object.assign({ role: 'attachment', required: true }, policy); next[path][key] = value; setNodeText(selNode.id, 'deliveries', next); }
                return h('div', { key: path, style: { padding: 6, marginTop: 5, border: '1px solid ' + C.border, fontSize: 10, wordBreak: 'break-all' } }, path,
                  h('select', { 'aria-label': '交付角色 ' + path, value: policy.role || '', style: { width: '100%', background: C.bg, color: C.text, marginTop: 5 }, onChange: function (e) { update('role', e.target.value); } },
                    h('option', { value: '' }, '沿用旧版自动判断'), h('option', { value: 'primary' }, '主成果'), h('option', { value: 'attachment' }, '附件'), h('option', { value: 'internal' }, '中间产物（不导出）')),
                  h('label', { style: { display: 'block', marginTop: 5 } }, h('input', { type: 'checkbox', checked: policy.required !== false, onChange: function (e) { update('required', e.target.checked); } }), '交付必需'),
                  h('label', { style: { display: 'block', marginTop: 5 } }, h('input', { type: 'checkbox', checked: !!policy.review_required, onChange: function (e) { update('review_required', e.target.checked); } }), '须确认内容审阅'));
              }),
              h('div', { style: { marginTop: 8, fontSize: 10, color: C.faint } }, '必读资料路径（每行一项；缺失时停止执行）'),
              h('textarea', { value: (selNode.required_knowledge || []).join('\n'), rows: 2, style: { width: '100%', boxSizing: 'border-box', background: C.bg, color: C.text }, onChange: function (e) { setNodeList(selNode.id, 'required_knowledge', e.target.value); } }),
              h("div", { style: { marginTop: 6 } },
                h("button", { onClick: function () { resetNodeMapping(selNode.id); }, style: { padding: "4px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" } }, "恢复默认文件")),
              h("div", { style: { marginTop: 14, display: "flex", gap: 6, flexWrap: "wrap" } },
                h("button", { onClick: function () {
                  var current = stateRef.current.run;
                  if (current && (current.nodes || []).some(function (n) { return n.id === selNode.id; })) {
                    openWorkspace(current.run_id, selNode.id);
                    return;
                  }
                  var mod = modules.filter(function (m) { return m.id === selNode.module; })[0] || {};
                  var gn = (graph.nodes || []).filter(function (n) { return n.id === selNode.id; })[0] || {};
                  taskCtxRef.current = null;
                  setTaskRun("");
                  setTaskNode(selNode.id);
                  setTaskOpen(true);
                  setWs(null);
                  setWsLoading(false);
                  setWsErr("");
                  setWsText("");
                  setArtifactRows([]);
                  setArtifactState("idle");
                  setArtifactError("");
                  setTaskInfo({ id: selNode.id, module: selNode.module, name: gn.name || mod.name || selNode.id,
                    status: "not_run", _automatic: false, checklist: gn.checklist || mod.checklist || [] });
                  setTaskContent("模块：" + (mod.name || selNode.module || "") +
                    "\n职责：" + (mod.agent_prompt || mod.description || "未描述") +
                    "\n输出：" + ((gn.artifacts || mod.artifacts || []).join("、") || "未声明") +
                    "\n\n尚未执行：当前运行里还没有这个节点。");
                  setWs({ node: { id: selNode.id, module: selNode.module, name: gn.name || mod.name || selNode.id, status: "not_run", generation: 1 },
                    executor: mod.executor || "agent", readonly: true, messages: [], events: [], artifacts: [], checks: [],
                    checklist: gn.checklist || mod.checklist || [], task_card: "模块职责：" + (mod.agent_prompt || mod.description || "未描述") + "\n声明输出：" + ((gn.artifacts || mod.artifacts || []).join("、") || "未声明") + "\n\n尚未执行：请返回编辑，保存画布后启动一个任务。", session_id: "" });
                }, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid " + C.teal, background: C.card, color: C.teal, cursor: "pointer", fontSize: 11 } }, "模块工作区"),
                h("button", { onClick: rerunFromNode, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid " + C.border, background: C.card, color: C.text, cursor: "pointer", fontSize: 11 } }, "从此节点重跑"),
                h("button", { onClick: deleteSelected, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid #5a2a2a", background: "#2a1414", color: "#ff8a8a", cursor: "pointer", fontSize: 11 } }, "删除节点")),
              h("div", { style: { marginTop: 16, fontSize: 11, fontWeight: 700 } }, "节点日志"),
              h("div", { style: { marginTop: 6, maxHeight: 150, overflow: "auto", fontFamily: "Consolas,monospace", fontSize: 9.5, color: C.sub, lineHeight: 1.7 } },
                logs.length ? logs.map(function (e, i) {
                  return h("div", { key: i, style: { borderLeft: "2px solid " + statusColor(e.status || ""), paddingLeft: 6, marginBottom: 3 } },
                    (e.ts || "").slice(11) + "  " + (e.kind || "") + "  " + (e.status || "") + (e.detail ? "  " + String(e.detail).slice(0, 60) : ""));
                }) : h("div", { style: { color: C.faint } }, "选中节点后显示最近日志")))
              : selEdge ? h("div", null,
                h("div", { style: { fontSize: 13, fontWeight: 700 } }, "连线"),
                h("div", { style: { fontFamily: "Consolas,monospace", fontSize: 10.5, color: C.sub, marginTop: 6 } }, selEdge.from + " → " + selEdge.to),
                h("div", { style: { marginTop: 14 } },
                  h("button", { onClick: deleteSelected, style: { padding: "6px 12px", borderRadius: 7, border: "1px solid #5a2a2a", background: "#2a1414", color: "#ff8a8a", cursor: "pointer", fontSize: 11.5 } }, "删除连线")))
              : h("div", { style: { fontSize: 11, color: C.faint, lineHeight: 1.9 } }, "点选节点或连线查看。", h("br"), "从输出圆点拖到输入圆点即可连线。"),

            h("div", { style: { marginTop: 22, fontSize: 11.5, fontWeight: 700 } }, "运行状态"),
            h("div", { style: { marginTop: 7, fontFamily: "Consolas,monospace", fontSize: 10, color: C.faint, lineHeight: 1.9 } },
              run ? "最近：" + friendlyRunName(run) + " · " + statusLabel(run.status) : "暂无运行", h("br"), "节点颜色跟随最近一次运行状态"),
            h("div", { style: { marginTop: 20, fontSize: 11.5, fontWeight: 700 } }, "快捷键"),
            h("div", { style: { marginTop: 7, fontSize: 10.5, color: C.faint, lineHeight: 1.9 } },
              "Ctrl+S 保存", h("br"), "Ctrl+Z / Shift+Ctrl+Z 撤销/重做", h("br"), "Delete 删除选中", h("br"), "滚轮缩放 · 空白拖动平移"),
            h("div", { style: { marginTop: 20, fontSize: 11.5, fontWeight: 700 } }, "运行历史"),
            h("div", { style: { marginTop: 7, fontSize: 10, fontFamily: "Consolas,monospace", color: C.faint, lineHeight: 1.9 } },
              history.slice(-5).reverse().map(function (r) {
                return h("div", { key: r.run_id, style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } },
                  (r.status || "?") + "  " + (r.run_id || ""));
              })),
            h("div", { style: { marginTop: 20, fontSize: 11.5, fontWeight: 700 } }, "AI 指令"),
            h("div", { style: { marginTop: 7, fontSize: 10.5, color: C.faint, lineHeight: 1.9 } },
              "在对话里说：", h("br"), "「加一个发布检查模块」", h("br"), "「把 QA 连到审核」", h("br"), "「删掉数据回填」", h("br"), "我会改画布并自动刷新。"))
        ),
        aiOpen ? h("div", {
          "data-ai-drawer": "1",
          style: {
            position: "absolute", right: 0, top: 0, bottom: 0, width: 330, zIndex: 30,
            background: C.panel, borderLeft: "1px solid " + C.border,
            boxShadow: "-12px 0 30px rgba(0,0,0,.35)", display: "flex", flexDirection: "column",
            padding: 12, boxSizing: "border-box",
          },
        },
          h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            h("div", { style: { fontSize: 13, fontWeight: 800 } }, "AI 指令"),
            h("button", { onClick: openAi, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 7px" } }, "关闭")),
          h("div", { style: { fontSize: 10, color: C.faint, marginTop: 6, lineHeight: 1.7 } }, "指令会发给贾维斯，在对话中执行；画布随后自动刷新。"),
          h("div", { style: { marginTop: 8, display: "flex", flexWrap: "wrap", gap: 4 } },
            ["加一个发布检查模块", "把 QA 连到审核", "删除数据回填", "从渲染节点重跑"].map(function (x) {
              return h("button", { key: x, onClick: function () { setAiText(x); }, style: { padding: "3px 7px", borderRadius: 99, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 9.5, cursor: "pointer" } }, x);
            })),
          h("textarea", {
            value: aiText, onChange: function (e) { setAiText(e.target.value); },
            placeholder: "例如：在 QA 后面加一个发布检查模块，再接到审核前",
            style: { flex: 1, marginTop: 8, padding: 8, borderRadius: 8, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 11.5, resize: "none", outline: "none", lineHeight: 1.6 },
          }),
          h("button", {
            onClick: sendAi,
            style: { marginTop: 8, padding: "8px 0", borderRadius: 8, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontWeight: 700, cursor: "pointer", fontSize: 12 },
          }, "发送给贾维斯"),
          aiLog.length ? h("div", { style: { marginTop: 10 } },
            h("div", { style: { fontSize: 9.5, color: C.faint, marginBottom: 4 } }, "已发送"),
            aiLog.map(function (x, i) { return h("div", { key: i, style: { fontSize: 9.5, color: C.sub, padding: "3px 6px", marginBottom: 3, borderRadius: 5, background: C.card, border: "1px solid " + C.borderSoft } }, x); })
          ) : null
        ) : null,
        moduleForm ? h("div", { role: "dialog", "aria-modal": true, "aria-label": moduleForm.editingId ? "编辑模块" : "新建模块", onKeyDown: function (e) { e.stopPropagation(); if (e.key === "Escape" && !busy) setModuleForm(null); },
          style: { position: "absolute", inset: 0, zIndex: 60, background: "rgba(0,0,0,.65)", display: "flex", alignItems: "center", justifyContent: "center", padding: 20 } },
          h("form", { onSubmit: function (e) { e.preventDefault(); createLibraryModule(!moduleForm.editingId); }, style: { width: 480, maxHeight: "90%", boxSizing: "border-box", overflow: "auto", background: C.panel, color: C.text, border: "1px solid " + C.border, borderRadius: 12, padding: 24, boxShadow: "0 16px 70px #000" } },
            h("h2", { style: { margin: "0 0 8px", fontSize: 19 } }, moduleForm.editingId ? "编辑模块" : "新建模块"),
            h("p", { style: { color: C.sub, fontSize: 12 } }, moduleForm.editingId ? "修改后，已放入画布的步骤会在下一次运行时使用新定义。" : "把你想让 DeepSeek 做的一步写下来，结果会自动保存为任务文档。"),
            [
              { key: "name", label: "模块名称", placeholder: "例如：整理采访重点" },
              { key: "instructions", label: "这一步要做什么", placeholder: "例如：读取本任务的采访资料，整理3个重点，并标明来源。", multiline: true },
              { key: "inputs", label: "输入文件（选填，每行一个）", placeholder: "例如：episodes/{stem}-research.md", multiline: true },
              { key: "outputs", label: "产物路径（选填，每行一个；不填自动生成）", placeholder: "例如：episodes/{stem}-notes.md", multiline: true },
              { key: "checklist", label: "怎样算完成（选填，每行一条）", placeholder: "例如：每个重点都有资料来源", multiline: true },
              { key: "timeout", label: "超时秒数（选填，30–7200）", placeholder: "例如：1800" }
            ].map(function (field) { return h("label", { key: field.key, style: { display: "block", fontSize: 12, marginTop: 14 } }, field.label,
              h(field.multiline ? "textarea" : "input", { "data-module-field": field.key, autoFocus: field.key === "name", value: moduleForm[field.key], required: field.key === "name" || field.key === "instructions", maxLength: field.key === "name" ? 80 : 20000, rows: field.key === "instructions" ? 5 : 3,
                onChange: function (e) { var value = e.target.value; setModuleForm(function (previous) { return Object.assign({}, previous, { [field.key]: value }); }); },
                placeholder: field.placeholder, style: { display: "block", width: "100%", boxSizing: "border-box", marginTop: 7, padding: 10, borderRadius: 7, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 12, resize: "vertical" } })); }),
            moduleError ? h("p", { role: "alert", style: { color: C.danger, fontSize: 12 } }, moduleError) : null,
            h("div", { style: { display: "flex", gap: 8, marginTop: 20, flexWrap: "wrap" } },
              h("button", { type: "submit", disabled: busy, style: { padding: "9px 12px", borderRadius: 7, border: "none", background: C.accent, color: C.bg, fontWeight: 700, cursor: "pointer" } }, busy ? "正在保存…" : moduleForm.editingId ? "保存模块定义" : "创建并加入画布"),
              !moduleForm.editingId ? h("button", { type: "button", disabled: busy || !moduleForm.name.trim() || !moduleForm.instructions.trim(), onClick: function () { createLibraryModule(false); }, style: { padding: "9px 12px", borderRadius: 7, border: "1px solid " + C.border, background: C.card, color: C.text, cursor: "pointer" } }, "仅存入模块库") : null,
              h("button", { type: "button", disabled: busy, onClick: function () { setModuleForm(null); }, style: { padding: "9px 12px", border: "none", background: "transparent", color: C.sub, cursor: "pointer" } }, "取消")))
        ) : null,
        guideOpen ? h("div", {
          "data-guide-drawer": "1",
          style: {
            position: "absolute", right: 0, top: 0, bottom: 0, width: 390, zIndex: 31,
            background: C.panel, borderLeft: "1px solid " + C.border,
            boxShadow: "-12px 0 30px rgba(0,0,0,.4)", display: "flex", flexDirection: "column",
            padding: 14, boxSizing: "border-box",
          },
        },
          h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            h("div", { style: { fontSize: 15, fontWeight: 800 } }, "新手引导"),
            h("button", { onClick: closeGuide, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "知道了")),
          h("div", { style: { flex: 1, overflow: "auto", marginTop: 10, paddingRight: 4 } },
            h("div", { style: { fontSize: 12, color: C.sub, lineHeight: 1.7 } }, "不需要懂技术。把每个模块当成一个步骤：读文件 → 干活 → 写文件。"),
            h("div", { style: { marginTop: 12, fontSize: 11.5, fontWeight: 700 } }, "三步上手"),
            h("div", { style: { marginTop: 5, fontSize: 11, color: C.sub, lineHeight: 1.9 } },
              "1. 在知识库新建任务分区，填入本次任务资料", h("br"),
              "2. 把模块按箭头连接，写清每一步要做的事", h("br"),
              "3. 点「运行」并填写同一个任务名，DeepSeek 自动往下做", h("br"),
              "遇到问题会停下来，点「回复并继续」即可接着做。"),
            h("div", { style: { marginTop: 12, fontSize: 11.5, fontWeight: 700 } }, "推荐路线"),
            h("div", { style: { marginTop: 5, fontSize: 10.5, fontFamily: "Consolas,monospace", color: C.sub, lineHeight: 1.8 } },
              "明确任务 → 调研 → 写脚本 → 制作 → 质检 → 审核 → 发布(可选) → 归档"),
            h("div", { style: { marginTop: 12, fontSize: 11.5, fontWeight: 700 } }, "常用按钮"),
            h("div", { style: { marginTop: 5, fontSize: 10.5, color: C.sub, lineHeight: 1.9 } },
              "保存：把画布变成可执行流程", h("br"),
              "运行：DeepSeek 自动按箭头执行，遇到问题才停下询问", h("br"),
              "模板：一键换成现成流程", h("br"),
              "历史版本：回到之前的画布", h("br"),
              "AI 指令：让贾维斯帮你加/改模块", h("br"),
              "?：查看模块的大白话说明"),
            h("div", { style: { marginTop: 12, fontSize: 11.5, fontWeight: 700 } }, "模块速查"),
            h("div", { style: { marginTop: 5 } },
              modules.map(function (m) {
                return h("div", { key: m.id, style: { fontSize: 10, color: C.sub, padding: "4px 7px", marginBottom: 3, borderRadius: 5, background: C.card, border: "1px solid " + C.borderSoft } },
                  h("b", { style: { color: C.text, fontWeight: 600 } }, m.name),
                  " · ",
                  (m.plain && m.plain["一句话"]) || m.description);
              })),
            h("div", { style: { marginTop: 14, fontSize: 11.5, fontWeight: 700 } }, "常见困惑"),
            h("div", { style: { marginTop: 5, fontSize: 10.5, color: C.sub, lineHeight: 1.9 } },
              "为什么还没视频？→ 看 produce 节点有没有跑", h("br"),
              "为什么停下来？→ 补充问题所需的信息，再点「回复并继续」", h("br"),
              "为什么失败？→ 点节点看「节点日志」里的原因", h("br"),
              "不想发布？→ 不要加 publish/stats 模块"),
            h("div", { style: { marginTop: 16, display: "flex", gap: 8, flexWrap: "wrap" } },
              h("button", { onClick: function () { closeGuide(); selectWorkflow("knowledge-digest"); }, style: { padding: "8px 12px", borderRadius: 8, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontWeight: 700, cursor: "pointer", fontSize: 11.5 } }, "先试资料卡工作流"),
              h("button", { onClick: closeGuide, style: { padding: "8px 12px", borderRadius: 8, border: "1px solid " + C.border, background: C.card, color: C.sub, cursor: "pointer", fontSize: 11.5 } }, "先自己逛逛"))
          )
        ) : null,
        kbOpen ? h("div", {
          "data-kb-drawer": "1",
          style: {
            position: "absolute", right: 0, top: 0, bottom: 0, width: 430, zIndex: 32,
            background: C.panel, borderLeft: "1px solid " + C.border,
            boxShadow: "-12px 0 30px rgba(0,0,0,.4)", display: "flex", flexDirection: "column",
            padding: 14, boxSizing: "border-box",
          },
        },
          h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            h("div", { style: { fontSize: 15, fontWeight: 800 } }, "知识库"),
            h("div", null,
              h("button", { onClick: createKbTask, style: { marginRight: 6, border: "1px solid " + C.accent, background: C.card, color: C.accent, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "新建任务分区"),
              h("button", { onClick: function () { if (kbAttachRef.current) kbAttachRef.current.click(); }, style: { marginRight: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "上传素材"),
              h("input", { ref: kbAttachRef, type: "file", onChange: uploadKbAttachment, style: { display: "none" } }),
              h("button", { onClick: openKb, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭"))),
          h("div", { style: { fontSize: 10, color: C.faint, marginTop: 6, lineHeight: 1.7 } },
              "知识库分两层：共享知识（所有工作流可用）+ 任务分区（当前任务专属）。其他任务分区不会混入。"),
          h("select", {
            value: kbTask || (run && run.task_id) || "",
            onChange: function (e) { var task = e.target.value; setKbTask(task); setKbSearch(""); loadKbEntries(task, ""); },
            style: { marginTop: 8, padding: 6, color: C.text, background: C.bg, border: "1px solid " + C.border, borderRadius: 6 },
          }, h("option", { value: "" }, "选择任务分区"), h("option", { value: "__shared__" }, "共享资料（所有任务）"), kbTasks.map(function (item) {
            return h("option", { key: item.task_id, value: item.task_id }, item.name || item.task_id);
          })),
          h("input", {
            value: kbSearch, onChange: function (e) { searchKb(e.target.value); }, placeholder: "搜索知识（标题/路径/正文）…",
            style: { marginTop: 8, padding: "6px 8px", borderRadius: 7, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 11, outline: "none" },
          }),
          h("div", { style: { flex: 1, display: "flex", flexDirection: "column", minHeight: 0, marginTop: 8 } },
            h("div", { style: { flex: 1, overflow: "auto", minHeight: 120 } },
              (function () {
                var q = kbSearch.trim().toLowerCase();
                var list = kbEntries;
                var shared = list.filter(function (e) { return e.scope === "shared"; });
                var task = list.filter(function (e) { return e.scope === "task"; });
                function group(title, items) {
                  if (!items.length) return null;
                  return h("div", { key: title },
                    h("div", { style: { fontSize: 10, color: C.faint, margin: "8px 0 4px" } }, title),
                    items.map(function (e) {
                      return h("div", {
                        key: e.path, onClick: function () { readKb(e.path); },
                        style: { padding: "5px 7px", marginBottom: 3, borderRadius: 6, cursor: "pointer", background: C.card, border: "1px solid " + C.borderSoft },
                      },
                        h("div", { style: { fontSize: 11, color: C.text } }, e.title || e.path),
                        h("div", { style: { fontSize: 11, color: C.faint, fontFamily: "Consolas,monospace" } }, e.path),
                        h("div", { style: { fontSize: 11, color: e.usable ? C.teal : C.accent, marginTop: 4 } }, knowledgeParseLabel(e)));
                    }));
                }
                return h("div", null,
                  group("共享知识", shared),
                  group("任务分区", task),
                  (!shared.length && !task.length) ? h("div", { style: { fontSize: 10.5, color: C.faint } }, "还没有可显示的知识；点上方「新建任务分区」") : null);
              })()),
            h("div", { style: { marginTop: 8, borderTop: "1px solid " + C.borderSoft, paddingTop: 8, maxHeight: "45%", overflow: "auto" } },
              kbPath ? h("div", { style: { display: "flex", gap: 6, marginBottom: 5 } },
                h("div", { style: { flex: 1, fontSize: 9.5, color: C.faint, fontFamily: "Consolas,monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, kbPath),
                !kbImage && /\.(md|txt)$/i.test(kbPath) ? (!kbEdit ? h("button", { onClick: function () { setKbEdit(true); setKbEditText(kbContent); }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 5, cursor: "pointer", fontSize: 11, padding: "4px 8px" } }, "编辑")
                  : h("button", { onClick: saveKbEdit, style: { border: "1px solid " + C.accent, background: C.accent, color: "#181207", borderRadius: 5, cursor: "pointer", fontSize: 11, padding: "4px 8px", fontWeight: 700 } }, "保存")) : null)
                : null,
              kbOriginal ? h("a", { href: kbOriginal, target: "_blank", rel: "noreferrer", style: { display: "inline-block", marginBottom: 9, fontSize: 12, color: C.teal } }, "打开原文件") : null,
              kbMeta ? h('div', { style: { fontSize: 11, color: C.sub, marginBottom: 9 } },
                '知识第 ' + kbMeta.knowledge_revision + ' 版 · ' + ({ draft: '草稿', confirmed: '已确认', deprecated: '已停用' }[kbMeta.knowledge_status] || '未确认'),
                h('div', { style: { display: 'flex', gap: 5, marginTop: 6, flexWrap: 'wrap' } }, ToolButton('确认采用', function () { decideKnowledge('confirmed'); }), ToolButton('退回草稿', function () { decideKnowledge('draft'); }), ToolButton('停用', function () { decideKnowledge('deprecated'); })),
                kbMeta.source_revision ? h('div', { style: { marginTop: 5, wordBreak: 'break-all' } }, '来源：' + kbMeta.source_revision.run_id + ' / ' + kbMeta.source_revision.node_id + ' / 第 ' + kbMeta.source_revision.generation + ' 代') : null,
                h('details', null, h('summary', null, '资料版本记录 · ' + (kbMeta.revisions || []).length), (kbMeta.revisions || []).map(function (version) { return h('div', { key: version.revision }, '第 ' + version.revision + ' 版 · ' + version.sha256.slice(0, 12)); }))) : null,
              kbImage ? h("img", { src: "data:" + kbImage.mime + ";base64," + kbImage.data, style: { maxWidth: "100%", maxHeight: 280, objectFit: "contain", borderRadius: 6, border: "1px solid " + C.border } })
                : kbEdit ? h("textarea", {
                value: kbEditText, onChange: function (e) { setKbEditText(e.target.value); },
                style: { width: "100%", boxSizing: "border-box", minHeight: 160, padding: 7, borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 10, fontFamily: "Consolas,monospace", lineHeight: 1.6, resize: "vertical" },
              }) : kbContent ? h("pre", { style: { margin: 0, whiteSpace: "pre-wrap", fontSize: 10, lineHeight: 1.6, color: C.sub, fontFamily: "Consolas,monospace" } }, kbContent) : h("div", { style: { fontSize: 10.5, color: C.faint } }, "点左侧文件查看/编辑内容"))
          )
        ) : null,
        wfOpen ? h("div", {
          "data-workflow-drawer": "1",
          style: { position: "absolute", right: 0, top: 0, bottom: 0, width: 380, zIndex: 37, background: C.panel,
                   borderLeft: "1px solid " + C.border, boxShadow: "-12px 0 30px rgba(0,0,0,.4)",
                   display: "flex", flexDirection: "column", padding: 14, boxSizing: "border-box" },
        },
          h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            h("div", { style: { fontSize: 15, fontWeight: 800 } }, "工作流库"),
            h("button", { onClick: openWorkflows, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭")),
          h("div", { style: { fontSize: 10, color: C.faint, marginTop: 6, lineHeight: 1.7 } },
            "每个工作流是独立的一张画布：可以同时保存视频、写作、调研等不同流程。"),
          h("div", { style: { marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" } },
            h("button", { onClick: createWorkflow, style: { padding: "5px 8px", borderRadius: 6, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontSize: 10, fontWeight: 700, cursor: "pointer" } }, "新建"),
            h("button", { onClick: renameWorkflow, style: { padding: "5px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" } }, "重命名"),
            h("button", { onClick: duplicateWorkflow, style: { padding: "5px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" } }, "复制"),
            h("button", { onClick: deleteWorkflow, style: { padding: "5px 8px", borderRadius: 6, border: "1px solid #5a2a2a", background: "#2a1414", color: "#ff8a8a", fontSize: 10, cursor: "pointer" } }, "删除"),
            h("button", { onClick: exportWorkflow, style: { padding: "5px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" } }, "导出"),
            h("button", { onClick: function () { if (wfImportRef.current) wfImportRef.current.click(); }, style: { padding: "5px 8px", borderRadius: 6, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" } }, "导入"),
            h("input", { ref: wfImportRef, type: "file", accept: ".json,.dsh-workflow.json", onChange: importWorkflow, style: { display: "none" } })),
          h("div", { style: { flex: 1, overflow: "auto", marginTop: 10 } },
            workflows.map(function (w) {
              var active = w.id === activeWorkflow.id;
              return h("div", {
                key: w.id, onClick: function () { selectWorkflow(w.id); },
                style: { padding: "8px 9px", marginBottom: 5, borderRadius: 7, cursor: "pointer",
                         background: active ? C.accentSoft || C.card : C.card,
                         border: "1px solid " + (active ? C.accent : C.borderSoft) },
              },
                h("div", { style: { fontSize: 12, fontWeight: 700, color: active ? C.accent : C.text, display: "flex", alignItems: "center", gap: 6 } },
                  w.name || w.id,
                  h("span", { style: { fontSize: 8.5, padding: "1px 5px", borderRadius: 99,
                    border: "1px solid " + (w.tier === "stable" ? C.teal : C.accent),
                    color: w.tier === "stable" ? C.teal : C.accent } }, w.tier === "stable" ? "稳定" : "实验")),
                h("div", { style: { fontSize: 9.5, color: C.faint, marginTop: 2 } }, w.description || "未描述"),
                h("div", { style: { fontSize: 9, color: C.faint, marginTop: 2, fontFamily: "Consolas,monospace" } }, w.id));
            }),
            workflows.length ? null : h("div", { style: { fontSize: 10.5, color: C.faint } }, "还没有工作流"))
        ) : null,
        treeOpen ? h("div", {
          "data-session-tree": "1",
          style: { position: "absolute", right: 0, top: 0, bottom: 0, width: 380, zIndex: 39, background: C.panel, borderLeft: "1px solid " + C.border, boxShadow: "-12px 0 30px rgba(0,0,0,.45)", display: "flex", flexDirection: "column", padding: 14, boxSizing: "border-box" },
        },
          h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            h("div", { style: { fontSize: 15, fontWeight: 800 } }, "会话树"),
            h("button", { onClick: function () { setTreeOpen(false); }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭")),
          h("div", { style: { fontSize: 10, color: C.faint, marginTop: 6, lineHeight: 1.7 } }, "运行会话 → 模块子会话。子会话不占左侧会话列表。"),
          h("div", { style: { marginTop: 10, fontSize: 10.5, fontFamily: "Consolas,monospace", color: C.sub } }, "运行会话 · " + ((run && (run.episode || run.run_id)) || "—")),
          (function () {
            var parent = ((run && run.nodes) || []).map(function (n) { return n.host_parent_session; }).filter(Boolean)[0];
            return parent ? h("div", { style: { marginTop: 6, fontSize: 9.5, fontFamily: "Consolas,monospace", color: C.teal, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, "父会话 " + parent) : null;
          })(),
          h("div", { style: { marginTop: 8, flex: 1, overflow: "auto" } },
            ((run && run.nodes) || []).map(function (n) {
              var sid = n.agent_session_id || "";
              return h("div", { key: n.id, onClick: function () { if (!run) return; setSelectedNode(n.id); openWorkspace(run.run_id, n.id); setTreeOpen(false); }, style: { padding: "7px 8px", borderBottom: "1px solid " + C.borderSoft, cursor: "pointer", display: "flex", alignItems: "center", gap: 7 } },
                h("span", { style: { width: 6, height: 6, borderRadius: 99, background: statusColor(n.status), flex: "0 0 6px" } }),
                h("span", { style: { fontSize: 11.5, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, n.name || n.id),
                h("span", { style: { fontSize: 9, color: C.faint, fontFamily: "Consolas,monospace" } }, sid ? sid.slice(-8) : "—"),
                (sid && n.host_parent_session) ? h("span", { onClick: function (e) { e.stopPropagation(); openHostSession(sid, n.host_parent_session); }, style: { fontSize: 9, color: C.teal, border: "1px solid " + C.teal, borderRadius: 5, padding: "1px 5px", cursor: "pointer" } }, "打开") : null,
                h("span", { style: { fontSize: 9, color: C.faint } }, statusLabel(n.status)));
            }),
            !run ? h("div", { style: { fontSize: 11, color: C.faint, padding: 8 } }, "还没有运行记录。") : null),
          h("div", { style: { marginTop: 8, fontSize: 10, color: C.faint, lineHeight: 1.7, borderTop: "1px dashed " + C.border, paddingTop: 8 } }, "点任意一行打开该模块工作区。"))
        : null,
        lineageOpen ? (function () {
          var lin = ws || {};
          var lnode = lin.node || {};
          var lk = lin.knowledge || {};
          var sources = lk.sources || [];
          var upstream = lin.upstream || [];
          var declared = lin.declared_inputs || [];
          function lrow(key, label, value, note) {
            return h("div", { key: key, style: { display: "flex", gap: 8, padding: "5px 0", borderBottom: "1px dashed " + C.borderSoft, fontSize: 11 } },
              h("div", { style: { flex: "0 0 82px", color: C.faint, fontSize: 9.5, paddingTop: 1 } }, label),
              h("div", { style: { flex: 1, color: C.sub, wordBreak: "break-all" } }, value,
                note ? h("div", { style: { fontSize: 9.5, color: C.faint, fontFamily: "Consolas,monospace", marginTop: 2 } }, note) : null));
          }
          var upRows = [];
          if (upstream.length) {
            upstream.forEach(function (item, i) {
              upRows.push(lrow("u" + i, "来自上游", item.path || item.artifact_path || "",
                "节点 " + (item.node_id || "") + (item.direct === false ? " · 隔层声明" : "")));
            });
          } else if (declared.length) {
            declared.forEach(function (p, i) { upRows.push(lrow("d" + i, "声明需要", p, "本次没有交接记录")); });
          } else {
            upRows.push(h("div", { key: "none", style: { fontSize: 10.5, color: C.faint, padding: "4px 0 8px" } }, "这是起点，没有上游产物。"));
          }
          var kbRows = sources.length ? sources.map(function (s, i) {
            return lrow("k" + i, (s.scope || "资料") + (s.truncated ? " · 截断" : ""), (s.title || s.path || ""),
              (s.path || "") + (s.selected_chars ? " · 采用 " + s.selected_chars + "/" + (s.source_chars || "?") + " 字" : ""));
          }) : [h("div", { key: "nokb", style: { fontSize: 10.5, color: C.faint, padding: "4px 0 8px" } }, "这一步没有采用知识库资料。")];
          var children = [h("div", { key: "h1", style: { fontSize: 10.5, color: C.teal, fontWeight: 700, marginBottom: 4 } }, "① 上游输入")]
            .concat(upRows)
            .concat([h("div", { key: "h2", style: { fontSize: 10.5, color: C.teal, fontWeight: 700, margin: "10px 0 4px" } }, "② 采用的资料")])
            .concat(kbRows)
            .concat([h("div", { key: "h3", style: { fontSize: 10.5, color: C.teal, fontWeight: 700, margin: "10px 0 4px" } }, "③ 本次会话")])
            .concat([
              lrow("s1", "子会话", lin.session_id || lnode.session_id || "尚未建立", "第 " + (lnode.generation || 1) + " 代 · " + statusLabel(lnode.status)),
              lrow("s2", "规模", (lin.messages || []).length + " 条消息 · " + (lin.events || []).length + " 条事件", lin.conversation_source ? "来源 " + lin.conversation_source : ""),
              lrow("s3", "资料指纹", lk.sha256 ? String(lk.sha256).slice(0, 16) : "未记录", lk.scope ? "分区 " + lk.scope : "")
            ]);
          return h("div", {
            "data-lineage": "1",
            style: { position: "absolute", right: 0, top: 0, bottom: 0, width: 520, zIndex: 40, background: C.panel, borderLeft: "1px solid " + C.border, boxShadow: "-12px 0 30px rgba(0,0,0,.45)", display: "flex", flexDirection: "column", padding: 14, boxSizing: "border-box" },
          },
            h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
              h("div", { style: { fontSize: 15, fontWeight: 800 } }, "血缘 · " + (lnode.name || taskNode)),
              h("button", { onClick: function () { setLineageOpen(false); }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭")),
            h("div", { style: { fontSize: 10, color: C.faint, marginTop: 4, lineHeight: 1.7 } }, "按排查顺序：上游给了什么 → 读了哪些资料 → 会话里怎么判断。"),
            h("div", { style: { flex: 1, overflow: "auto", marginTop: 10 } }, children),
            h("div", { style: { marginTop: 8, fontSize: 10, color: C.faint, lineHeight: 1.7, borderTop: "1px dashed " + C.border, paddingTop: 8 } }, "哈希与版本差异会在落地后补进这里；当前先给输入、资料、会话三段。"));
        })() : null,
        taskOpen ? (function () {
          var data = ws || {};
          var wn = data.node || {}, wr = data.run || {};
          var readonly = !!data.readonly || (["awaiting_input", "running", "stopping"].indexOf(wn.status) < 0 && taskRun !== ((run && run.run_id) || ""));
          var waiting = wn.status === "awaiting_input" || wn.status === "awaiting_agent";
          var running = wn.status === "running" || wn.status === "stopping";
          var messages = (data.messages || []).filter(function (message) { return message.kind !== 'task'; });
          var events = (data.events || []).slice(-20).reverse();
          var arts = data.artifacts || [];
          var checks = data.checks || [], checklist = data.checklist || [];
          return h("div", {
            "data-workspace": "1",
            style: { position: "absolute", right: 0, top: 0, bottom: 0, width: 680, zIndex: 36, background: C.panel, borderLeft: "1px solid " + C.border, boxShadow: "-12px 0 30px rgba(0,0,0,.45)", display: "flex", flexDirection: "column", boxSizing: "border-box" },
          },
            h("div", { style: { padding: "12px 14px 6px" } },
              h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
                h("div", { style: { fontSize: 18, fontWeight: 800 } }, "模块工作区 · " + (wn.name || taskNode)),
                h("button", { onClick: function () { setLineageOpen(function (v) { return !v; }); }, style: { border: "1px solid " + (lineageOpen ? C.teal : C.border), background: C.card, color: lineageOpen ? C.teal : C.sub, borderRadius: 6, cursor: "pointer", fontSize: 12, padding: "3px 8px", marginRight: 6 } }, lineageOpen ? "返回会话" : "血缘"),
                taskRun ? ToolButton("返回任务", function () { var rid = taskRun; closeTask(); openResults(rid); }) : null,
                h("button", { onClick: closeTask, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 12, padding: "3px 8px" } }, "关闭")),
              h("div", { "data-session-header": "1", style: { marginTop: 6, padding: "8px 10px", borderRadius: 8, background: C.bg, border: "1px solid " + C.borderSoft } },
                h("div", { style: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" } },
                  h("span", { style: { fontSize: 11.5, fontWeight: 700, marginRight: 2 } }, wn.name || taskNode),
                  h("span", { style: { fontSize: 9, padding: "1px 6px", borderRadius: 99, border: "1px solid " + statusColor(wn.status), color: statusColor(wn.status) } }, statusLabel(wn.status)),
                  workspaceSessionId(data) ? h("span", { style: { fontFamily: "Consolas,monospace", fontSize: 9, color: C.teal, padding: "1px 6px", borderRadius: 99, border: "1px solid " + C.teal } }, String(workspaceSessionId(data)).slice(-8) + " · 子会话") : null,
                  h("span", { style: { flex: 1 } }),
                  !data.readonly && (data.external_changes || []).length ? ToolButton('登记修改为新版本', adoptWorkspaceChanges, true) : null,
                  !data.readonly && wn.review_status === 'pending' ? ToolButton('确认内容审阅通过', approveWorkspaceReview, true) : null,
                  workspaceSessionId(data) && !data.readonly ? ToolButton("同步并校验", syncWorkspace) : null,
                  workspaceSessionId(data) ? h("button", { onClick: function () { try { navigator.clipboard.writeText(workspaceSessionId(data)); setMsg("已复制会话 id"); } catch (e) {} }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 5, cursor: "pointer", fontSize: 9, padding: "2px 6px" } }, "复制") : null,
                  (workspaceSessionId(data) && data.host_parent_session) ? h("button", { onClick: function () { openHostSession(workspaceSessionId(data), data.host_parent_session); }, style: { border: "1px solid " + C.teal, background: C.card, color: C.teal, borderRadius: 5, cursor: "pointer", fontSize: 9, padding: "2px 6px" } }, "打开") : null),
                h("div", { style: { display: "flex", alignItems: "center", gap: 8, marginTop: 5, fontSize: 9, color: C.faint, flexWrap: "wrap", fontFamily: "Consolas,monospace" } },
                  h("span", { style: { color: C.sub } }, "所属运行会话 " + ((wr.episode || "") ? wr.episode + " · " : "") + (data.host_parent_session || (workspaceSessionId(data) ? "尚未记录" : data.executor === 'agent' ? '尚未建立 AI 会话' : "程序模块"))),
                  h("span", null, taskRun),
                  h("span", null, "第 " + (wn.generation || 1) + " 代"),
                  wn.started_at ? h("span", null, String(wn.started_at).slice(11, 19) + (wn.finished_at ? " → " + String(wn.finished_at).slice(11, 19) : "")) : null,
                  readonly ? h("span", { style: { color: C.accent } }, "历史只读") : null)),
              data.continuation_of ? h('div', { style: { padding: 8, fontSize: 11, color: C.accent } }, '会话已续接：' + (data.continuity_reason || '已建立后继会话') + '。', ToolButton('查看前一会话', function () { openHostSession(data.continuation_of, data.host_parent_session); })) : null,
              (data.generations || data.generation_history || []).length > 1 ? h("select", { value: wn.generation || 1, onChange: function (e) { loadWorkspace(taskRun, taskNode, e.target.value); }, style: { marginTop: 8, background: C.card, color: C.text, padding: 6, border: "1px solid " + C.border } }, (data.generations || data.generation_history).map(function (g) { return h("option", { key: g.generation, value: g.generation }, "第 " + g.generation + " 代 · " + statusLabel(g.status)); })) : null,
              readonly ? h("div", { style: { marginTop: 6, fontSize: 11, color: C.accent } }, "这是所选历史运行；查看不会修改当前工作流。") : null),
            h("div", { style: { flex: 1, overflow: "auto", padding: "0 14px 14px" } },
              h("div", { "data-trace-strip": "1", style: { display: "flex", gap: 4, flexWrap: "wrap", margin: "6px 0 5px" } },
                waiting ? h("span", { style: { fontSize: 9, padding: "2px 6px", borderRadius: 99, border: "1px solid " + C.accent, color: C.accent, background: C.card, whiteSpace: "nowrap" } }, "等待你的回复") : null,
                running && wn.live ? h("span", { style: { fontSize: 9, padding: "2px 6px", borderRadius: 99, border: "1px solid " + C.teal, color: C.teal, background: C.card, whiteSpace: "nowrap" } }, "执行中 · " + (wn.live.tool_name || phaseLabel(wn.live.phase))) : null,
                events.length ? events.slice(0, 6).reverse().map(function (e, i) {
                  return h("span", { key: "tr" + i, title: String(e.detail || ""), style: { fontSize: 9, padding: "2px 6px", borderRadius: 99, border: "1px solid " + statusColor(e.status || ""), color: statusColor(e.status || ""), background: C.card, whiteSpace: "nowrap" } },
                    String(e.ts || "").slice(11, 19) + " · " + (e.kind ? e.kind : statusLabel(e.status || "")));
                }) : h("span", { style: { fontSize: 9.5, color: C.faint } }, "尚无执行事件")),
              h("div", { "data-input-strip": "1", style: { display: "flex", gap: 10, fontSize: 9.5, color: C.faint, marginBottom: 8, flexWrap: "wrap" } },
                h("span", null, "上游 " + (data.upstream || []).length),
                h("span", null, "资料 " + ((data.knowledge || {}).sources || []).length),
                h("span", null, "产物 " + arts.length),
                h("span", null, "验收 " + checks.filter(Boolean).length + "/" + checklist.length),
                h("span", null, "事件 " + (data.events || []).length)),
              h("div", { style: { fontSize: 11, fontWeight: 700, marginTop: 6 } }, "对话"),
              wsLoading ? h("div", { role: "status", style: { padding: 12, color: C.sub, fontSize: 13 } }, "正在读取真实会话与执行记录…") : null,
              wsErr ? h("div", { style: { marginTop: 4, fontSize: 11, color: C.danger } }, "读取失败：" + wsErr) : null,
              h("div", { style: { marginTop: 4, maxHeight: 340, overflow: "auto" } },
                (data.message_page || {}).has_older ? ToolButton('加载更早的公开消息', loadOlderMessages) : null,
                messages.length ? messages.map(function (m, i) {
                  var mine = m.role === "user";
                  return h("div", { key: m.id || i, style: { display: "flex", justifyContent: mine ? "flex-end" : "flex-start", marginBottom: 6 } },
                    h("div", { style: { maxWidth: "86%", padding: "6px 8px", borderRadius: 8, fontSize: 12, lineHeight: 1.6, whiteSpace: "pre-wrap",
                      background: mine ? C.card : C.bg, border: "1px solid " + (mine ? C.borderSoft : C.border), color: C.sub } },
                      h("div", { style: { fontSize: 8.5, color: C.faint, marginBottom: 2 } },
                        (data.executor && data.executor !== "agent" ? "执行提示" : workspaceMessageLabel(m)) + (messageTimeText(m) ? " · " + messageTimeText(m) : "")),
                      m.text));
                }) : h("div", { style: { fontSize: 12, color: C.faint, lineHeight: 1.7 } },
                  data.executor && data.executor !== "agent" ? "程序执行不产生 AI 对话；过程请看下方执行记录。" : "还没有公开消息。执行完成后这里会出现助手回复；等待回答时会出现问题。")),
              waiting && !data.readonly && data.executor && data.executor !== "agent" ? h("div", { style: { padding: 12, marginTop: 8, fontSize: 12, color: C.accent, border: "1px solid " + C.border, borderRadius: 7 } },
                "请根据执行记录修正资料或上游结果，再重新生成相关步骤。程序模块按配置执行，文字回复不会改写处理规则。",
                h("button", { onClick: function () { rerunNode(taskRun, taskNode); }, disabled: busy, style: { display: "block", marginTop: 8, padding: "7px 11px", background: C.accent, color: C.bg, border: "none", borderRadius: 6, cursor: "pointer" } }, "输入已修正，重试此步骤"),
                h("button", { onClick: stopTaskRun, disabled: busy, style: { display: "block", marginTop: 6, padding: "7px 11px", background: C.card, color: C.danger, border: "1px solid " + C.danger, borderRadius: 6, cursor: "pointer" } }, "停止本次任务")) : null,
              waiting && !data.readonly && data.executor === "agent" ? h("div", { style: { marginTop: 6 } },
                h("textarea", {
                  value: wsText, rows: 2, placeholder: "回答这个问题，继续同一工作会话…",
                  onChange: function (e) { setWsText(e.target.value); },
                  style: { width: "100%", boxSizing: "border-box", padding: 7, borderRadius: 7, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 12, resize: "vertical", outline: "none" },
                }),
                h("div", { style: { marginTop: 5, display: "flex", gap: 6 } },
                  h("button", { onClick: sendWorkspaceReply, disabled: busy || !wsText.trim(), style: { padding: "6px 11px", borderRadius: 7, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontSize: 12, fontWeight: 700, cursor: wsText.trim() ? "pointer" : "default" } }, "回复并继续"),
                  h("button", { onClick: function () { loadWorkspace(taskRun, taskNode); }, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid " + C.border, background: C.card, color: C.sub, fontSize: 12, cursor: "pointer" } }, "刷新"),
                  h("button", { onClick: stopTaskRun, disabled: busy, style: { padding: "6px 10px", borderRadius: 7, border: "1px solid " + C.danger, background: C.card, color: C.danger, fontSize: 12, cursor: "pointer" } }, "停止")))
                : running && !data.readonly ? h("div", { style: { marginTop: 6, display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: C.accent } },
                  "正在执行；完成或提问后可在这里继续。",
                  h("button", { onClick: stopTaskRun, disabled: busy, style: { padding: "4px 9px", borderRadius: 6, border: "1px solid " + C.danger, background: C.card, color: C.danger, fontSize: 11, cursor: "pointer" } }, "停止本次运行"))
                : null,
              canContinueNode(wn, readonly) ? h("div", { style: { marginTop: 12, padding: 12, border: "1px solid " + C.danger, borderRadius: 8 } },
                h("button", { onClick: function () { rerunNode(taskRun, taskNode); }, disabled: busy, style: { padding: "8px 12px", borderRadius: 7, background: C.accent, border: "none", color: C.bg, fontWeight: 700, cursor: "pointer" } }, busy ? "正在提交…" : "从当前步继续"),
                h("div", { style: { marginTop: 6, fontSize: 11, color: C.sub } }, "保留已通过的上游，从当前节点及其下游继续。")) : null,
              (!wsLoading && !wsErr && !data.readonly && data.executor === "agent" && ["passed", "failed", "cancelled", "skipped"].indexOf(wn.status) !== -1) ? h("div", { style: { marginTop: 12, padding: 12, border: "1px solid " + C.border, borderRadius: 8 } },
                h("strong", { style: { fontSize: 12 } }, "继续讨论或修改"),
                h("div", { style: { margin: "5px 0", fontSize: 11, color: C.sub } }, "讨论用于解释和确认要求；修改成果时创建修订，原版本保留，受影响下游标为过期。"),
                h("textarea", { value: wsText, rows: 3, placeholder: "例如：保留原结构，增加一个更具体的例子…", onChange: function (e) { setWsText(e.target.value); }, style: { width: "100%", boxSizing: "border-box", padding: 9, borderRadius: 7, background: C.bg, border: "1px solid " + C.border, color: C.text, fontSize: 13, lineHeight: 1.7 } }),
                h("button", { onClick: discussWorkspace, disabled: busy || !wsText.trim(), style: { marginTop: 7, marginRight: 8, padding: "8px 12px", borderRadius: 7, background: C.card, border: "1px solid " + C.border, color: C.text, cursor: "pointer" } }, "发送讨论"),
                h("button", { onClick: reviseWorkspace, disabled: busy || !wsText.trim(), style: { marginTop: 7, padding: "8px 12px", borderRadius: 7, background: C.accent, border: "none", color: C.bg, fontWeight: 700, cursor: "pointer" } }, busy ? "正在提交…" : "创建修订并继续")) : null,
              (!data.readonly && data.executor && data.executor !== "agent" && wn.status === "passed") ? h("div", { style: { marginTop: 12, fontSize: 12, color: C.sub } },
                "这是按配置执行的程序模块。修改输入资料后，可从此步骤重新生成。",
                h("button", { disabled: busy, onClick: function () { rerunNode(taskRun, taskNode); }, style: { display: "block", marginTop: 8, padding: "7px 10px", border: "1px solid " + C.border, background: C.card, color: C.text, borderRadius: 6, cursor: "pointer" } }, "重新生成此步骤及下游")) : null,
              arts.length ? h("div", { "data-artifact-strip": "1", style: { display: "flex", gap: 5, overflowX: "auto", marginTop: 10, paddingBottom: 3 } },
                arts.map(function (a) {
                  var badge = ARTIFACT_STATUS[a.status] || ARTIFACT_STATUS.legacy;
                  return h("button", { key: "chip" + a.path, onClick: function () { openArtifact(a); }, title: a.path,
                    style: { flex: "0 0 auto", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", padding: "5px 8px", borderRadius: 99, border: "1px solid " + badge.color, background: C.card, color: C.sub, fontSize: 10, cursor: "pointer" } },
                    (ARTIFACT_KIND[a.kind] || "文件") + " · " + String(a.path).split("/").pop());
                })) : null,
              h("div", { style: { fontSize: 11, fontWeight: 700, marginTop: 12 } }, "输入资料"),
              h("details", { style: { marginTop: 3 } },
                h("summary", { style: { fontSize: 11, color: C.sub, cursor: "pointer" } }, "任务要求与知识来源（" + (data.task_card ? "已保存任务卡" : "无任务卡") + "）"),
                h("pre", { style: { maxHeight: 180, overflow: "auto", margin: "6px 0 0", padding: 8, borderRadius: 6, background: C.bg, border: "1px solid " + C.border, whiteSpace: "pre-wrap", fontSize: 11, lineHeight: 1.6, color: C.sub } }, data.task_card || "（这次执行没有保存任务卡）")),
              h("div", { style: { marginTop: 6, fontSize: 11, color: C.faint, lineHeight: 1.7 } },
                "知识分区：" + ((data.knowledge || {}).scope || (data.executor === "agent" ? "未记录" : "未保存知识采用记录")) +
                ((data.knowledge || {}).sha256 ? " · 上下文哈希 " + String(data.knowledge.sha256).slice(0, 12) : ""),
                h("br"),
                ((data.upstream || []).length ? "本次上游输入：" + (data.upstream || []).map(function (u) { return u.path || u; }).join("、") : "声明输入：" + ((data.declared_inputs || []).join("、") || "无"))),
              h("details", { "data-knowledge-sources": "1", style: { marginTop: 8, fontSize: 12, color: C.sub } },
                h("summary", { style: { cursor: "pointer" } }, "本次采用的知识资料 · " + ((data.knowledge || {}).sources || []).length + " 份"),
                ((data.knowledge || {}).sources || []).map(function (source, i) {
                  return h("div", { key: source.path || i, style: { padding: "7px 9px", marginTop: 5, background: C.bg, borderRadius: 6 } },
                    h("strong", null, source.title || source.path),
                    h("div", { style: { fontSize: 11, overflowWrap: "anywhere" } }, source.path),
                    h("div", { style: { color: C.faint, marginTop: 3 } },
                      source.source_type === "generated_draft" ? "历史生成草稿 · 尚未人工确认" : source.scope === "shared" ? "共享参考资料" : "本任务资料",
                      source.reason === "query_match" ? " · 与本模块任务相关" : "",
                      source.selected_chars ? " · 采用 " + source.selected_chars + " 字" : ""),
                    source.truncated ? h("div", { style: { color: C.accent } }, "已按上下文容量截取；原文件保留完整内容") : null);
                }),
                !((data.knowledge || {}).sources || []).length ? h("div", { style: { padding: 8, color: C.faint } }, "该次运行没有保存可确认的采用记录。") : null),
              h("div", { style: { fontSize: 11, fontWeight: 700, marginTop: 12 } }, "执行记录"),
              h("div", { style: { marginTop: 4, maxHeight: 190, overflow: "auto", fontFamily: "Consolas,monospace", fontSize: 11, color: C.faint, lineHeight: 1.7 } },
                events.length ? events.map(function (e, i) {
                  return h("div", { key: i, style: { borderLeft: "2px solid " + statusColor(e.status || ""), paddingLeft: 6, marginBottom: 3 } },
                    String(e.ts || "").slice(11, 19) + "  " + (e.kind || "") + "  " + (e.status || "") + (e.detail ? "  " + String(e.detail).slice(0, 80) : ""));
                }) : h("div", null, "暂无事件记录")),
              h("div", { style: { fontSize: 11, fontWeight: 700, marginTop: 12 } }, "成果与验收"),
              h("div", { style: { marginTop: 4, maxHeight: 150, overflow: "auto" } },
                arts.length ? arts.map(function (a) {
                  var badge = ARTIFACT_STATUS[a.status] || ARTIFACT_STATUS.legacy;
                  return h("div", { key: a.path, onClick: function () { openArtifact(a); }, title: a.path,
                    style: { display: "flex", justifyContent: "space-between", gap: 6, padding: "3px 5px", marginBottom: 2, borderRadius: 5, cursor: "pointer", background: C.card, border: "1px solid " + C.borderSoft, fontFamily: "Consolas,monospace", fontSize: 11 } },
                    h("span", { style: { flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: C.sub } }, a.path),
                    h("span", { style: { color: badge.color, whiteSpace: "nowrap" } }, badge.label),
                    h("span", { style: { color: C.faint } }, ARTIFACT_KIND[a.kind] || "文件"));
                }) : h("div", { style: { fontSize: 11, color: C.faint } }, "这次执行没有可查看的产物")),
              checklist.length ? h("div", { style: { marginTop: 5 } },
                checklist.map(function (c, i) {
                  return h("div", { key: i, style: { fontSize: 11, color: checks[i] ? C.teal : C.faint, lineHeight: 1.7 } },
                    (checks[i] ? "✓ " : "· ") + c);
                })) : null,
              h("div", { "data-run-card": "1", style: { marginTop: 14, padding: 10, borderRadius: 8, background: C.card, border: "1px solid " + C.border } },
                h("div", { style: { fontSize: 11, fontWeight: 700 } }, "运行会话 · " + (wr.episode || taskRun)),
                h("div", { style: { fontSize: 9, color: C.faint, marginTop: 3, fontFamily: "Consolas,monospace", overflowWrap: "anywhere" } },
                  (data.host_parent_session || "尚未建立父会话") + " · 后代 " + ((run && run.nodes) || []).length
                  + " · 已完成 " + ((run && run.nodes) || []).filter(function (n) { return n.status === "passed" || n.status === "skipped"; }).length
                  + " · 等你回复 " + ((run && run.nodes) || []).filter(function (n) { return n.status === "awaiting_input"; }).length),
                h("button", { onClick: function () { setTreeOpen(true); }, style: { marginTop: 7, padding: "5px 9px", borderRadius: 6, border: "1px solid " + C.border, background: C.bg, color: C.sub, fontSize: 10, cursor: "pointer" } }, "打开后代目录")),
              h("div", { style: { marginTop: 10, fontSize: 9.5, color: C.faint, lineHeight: 1.7 } }, "这是模块自己的子会话：消息、工具调用、输入、资料、产物都在这一页；左侧列表只显示它所属的运行父会话。"))
          );
        })() : null,
        resultsOpen ? h("div", { "data-results": "1", style: { position: "absolute", right: 0, top: 0, bottom: 0, width: 860, maxWidth: "96%", zIndex: 35, background: C.panel, borderLeft: "1px solid " + C.border, boxShadow: "-16px 0 45px rgba(0,0,0,.5)", display: "flex", flexDirection: "column", padding: 20, boxSizing: "border-box" } },
          h("div", { style: { display: "flex", alignItems: "center", gap: 8 } }, h("h2", { style: { margin: 0, flex: 1, fontSize: 20 } }, "任务与成果"), ToolButton("刷新", function () { openResults(resultRun); }), ToolButton("关闭", closeResults)),
          h("select", { "aria-label": "选择任务运行", value: resultRunSelection(history, resultRun), onChange: function (e) { openResults(e.target.value); }, style: { marginTop: 14, padding: 10, background: C.bg, border: "1px solid " + C.border, color: C.text, borderRadius: 8, fontSize: 12 } }, (history.some(function (r) { return r.run_id === resultRun; }) ? [] : [h("option", { key: resultRun, value: resultRun }, ((resultsData || {}).run || {}).episode || "当前任务")]).concat(history.slice().reverse().map(function (r) { return h("option", { key: r.run_id, value: r.run_id }, (r.episode || "任务") + " · " + statusLabel(r.status) + " · " + r.run_id); }))),
          h("div", { style: { overflow: "auto", flex: 1, marginTop: 14 } },
            resultsError ? h("div", { role: "alert", style: { padding: 14, color: C.danger } }, resultsError) : !resultsData ? h("div", { role: "status", style: { color: C.sub, padding: 20 } }, "正在读取真实任务成果…") : h("div", null,
              h("h3", { style: { fontSize: 18, margin: "4px 0" } }, friendlyRunName(resultsData.run)),
              h("div", { style: { color: C.sub, fontSize: 12, marginBottom: 12 } }, resultRun + " · " + statusLabel((resultsData.run || {}).status)),
              h("div", { "data-deliverable": resultsData.deliverable ? "yes" : "no", style: { padding: 12, margin: "10px 0", borderRadius: 8, border: "1px solid " + (resultsData.deliverable ? C.teal : C.accent), color: resultsData.deliverable ? C.teal : C.accent, fontSize: 13 } },
                resultsData.deliverable ? "可交付 · 主成果与验收已通过" : "尚未确认可交付 · 请查看文件和验收范围",
                (resultsData.warnings || []).map(function (warning, i) { return h("div", { key: i, style: { color: C.sub, fontSize: 11, marginTop: 5 } }, warning); })),
              h("div", { style: { display: "flex", gap: 8, marginBottom: 16 } }, ToolButton("在画布查看此运行", function () { closeResults(); viewRun(resultRun); })),
              ToolButton('确认交付范围', editDeliveryScope, false, (resultsData.run || {}).status === 'running'),
              deliveryDraft ? h('div', { style: { border: '1px solid ' + C.border, padding: 12, borderRadius: 8, margin: '12px 0' } },
                h('strong', null, '选择主成果、附件与内部文件'),
                h('p', { style: { color: C.sub, fontSize: 12 } }, '主成果支持多选；必要文件缺失或内容审阅未完成时不能交付。'),
                deliveryDraft.map(function (row, i) {
                  function change(key, value) { setDeliveryDraft(deliveryDraft.map(function (old, j) { return i === j ? Object.assign({}, old, (function () { var update = {}; update[key] = value; return update; })()) : old; })); }
                  return h('div', { key: row.node_id + row.path, style: { marginBottom: 12, borderBottom: '1px solid ' + C.border, paddingBottom: 8 } },
                    h('div', { style: { wordBreak: 'break-all', fontSize: 11, marginBottom: 6 } }, row.path),
                    h('select', { 'aria-label': '用途 ' + row.path, value: row.role, onChange: function (e) { change('role', e.target.value); } },
                      h('option', { value: 'primary' }, '主成果'), h('option', { value: 'attachment' }, '附件'), h('option', { value: 'internal' }, '内部文件（不导出）')),
                    h('label', { style: { marginLeft: 10 } }, h('input', { type: 'checkbox', checked: row.required, onChange: function (e) { change('required', e.target.checked); } }), '必要'),
                    h('label', { style: { marginLeft: 10 } }, h('input', { type: 'checkbox', checked: row.review_required, onChange: function (e) { change('review_required', e.target.checked); } }), '需要内容审阅'));
                }), ToolButton('保存交付范围', saveDeliveryScope, true), ToolButton('取消', function () { setDeliveryDraft(null); })) : null,
              resultsData.deliverable ? h('a', { href: API + projectPath('/wf/export?run_id=' + encodeURIComponent(resultRun)), download: true, style: { display: 'inline-block', color: C.teal, marginBottom: 14 } }, '下载完整交付包（含清单与校验值）') : null,
              (resultsData.stale_nodes || []).length ? h("div", { style: { color: C.accent, padding: 10, border: "1px solid " + C.accent, borderRadius: 7, marginBottom: 12 } }, "上游已修改，需要更新：" + resultsData.stale_nodes.join("、"),
                h("button", { disabled: busy || (resultsData.run || {}).status === "running", onClick: function () {
                  var rid = resultRun; setBusy(true);
                  postRunCommand("/wf/continue", { run_id: rid }).then(function () { setMsg("已开始更新受影响的下游，保留其他成果"); openResults(rid); load(); })
                    .catch(function (e) { setMsg("更新下游失败：" + e.message); }).finally(function () { setBusy(false); });
                }, style: { display: "block", marginTop: 9, padding: "8px 12px", border: "none", borderRadius: 6, background: C.accent, color: C.bg, fontWeight: 700, cursor: "pointer" } }, "更新下游并完成流程")) : null,
              (resultsData.pending_reviews || []).map(function (id) { return h('div', { key: id }, '待内容审阅：' + id, ToolButton('查看并审阅', function () { openWorkspace(resultRun, id); })); }),
              (resultsData.external_changes || []).map(function (change) { return h('div', { key: change.node_id }, '发现工作区新修改：' + change.node_id, ToolButton('查看并登记版本', function () { openWorkspace(resultRun, change.node_id); })); }),
              (function () {
                var main = primaryArtifact(resultsData);
                if (!main || main.status === "missing") return null;
                var mediaUrl = main.media_url || API + "/wf/raw?run_id=" + encodeURIComponent(resultRun) + "&path=" + encodeURIComponent(main.path);
                return h("div", { "data-primary-preview": main.kind, style: { margin: "14px 0", padding: 12, border: "1px solid " + C.teal, borderRadius: 10, background: C.bg } },
                  h("strong", { style: { display: "block", fontSize: 14, marginBottom: 9 } }, "主成果 · " + friendlyArtifactName(main)),
                  main.kind === "video" ? h("video", { controls: true, preload: "metadata", src: mediaUrl, style: { display: "block", width: "100%", maxHeight: 360, background: "#000", borderRadius: 7 } })
                    : main.kind === "image" ? h("img", { src: mediaUrl, alt: main.name || main.path, style: { display: "block", maxWidth: "100%", maxHeight: 360, margin: "auto", objectFit: "contain" } })
                    : main.kind === "audio" ? h("audio", { controls: true, preload: "metadata", src: mediaUrl, style: { width: "100%" } })
                    : isInlineTextArtifact(main) ? h("div", null,
                      !primaryText || primaryText.status === "loading" ? h("div", { role: "status", style: { color: C.sub, fontSize: 12 } }, "正在读取文本预览…")
                        : primaryText.status === "error" ? h("div", { role: "alert", style: { color: C.danger, fontSize: 12 } }, primaryText.text)
                        : h("div", { "data-primary-text": "1", style: { margin: 0, maxHeight: 520, overflow: "auto", color: C.text, fontSize: 14, lineHeight: 1.8 } }, /\.md$/i.test(main.path) ? readableDocument(primaryText.text || "（空文件）") : h("pre", { style: { whiteSpace: "pre-wrap" } }, primaryText.text || "（空文件）")))
                    : h("button", { onClick: function () { openArtifact(Object.assign({}, main, { _runId: resultRun })); }, style: { padding: "10px 14px", background: C.accent, border: "none", borderRadius: 7, color: C.bg, fontWeight: 700, cursor: "pointer" } }, "打开主成果"));
              })(),
              h("h3", { style: { fontSize: 15 } }, "主成果与附件"),
              (resultsData.artifacts || []).length ? (function () {
                var grouped = { primary: [], attachments: [], engineering: [] };
                resultsData.artifacts.forEach(function (a) {
                  var engineering = a.delivery_role === 'internal' || /-project(?:\/|$)/.test(a.logical_path || a.path);
                  grouped[a.is_primary ? 'primary' : engineering ? 'engineering' : 'attachments'].push(a);
                });
                return [['primary','主成果'],['attachments','交付附件'],['engineering','工程与过程文件']].map(function (group) {
                  var rows = grouped[group[0]]; if (!rows.length) return null;
                  return h('details', { key: group[0], open: group[0] !== 'engineering', style: { marginBottom: 12 } },
                    h('summary', { style: { cursor: 'pointer', padding: 8, color: C.sub } }, group[1] + ' · ' + rows.length + ' 项'),
                    rows.map(function (a, i) { return h('button', { key: a.path + ':' + i,
                      onClick: function () { openArtifact(Object.assign({}, a, { _runId: resultRun })); }, disabled: a.status === 'missing',
                      style: { display: 'block', width: '100%', textAlign: 'left', marginBottom: 8, padding: 12, background: a.is_primary ? '#132b30' : C.card, color: C.text, border: '1px solid ' + (a.is_primary ? C.teal : C.border), borderRadius: 8, cursor: 'pointer' } },
                      h('div', { style: { fontSize: 13, fontWeight: 700 } }, friendlyArtifactName(a)),
                      h('div', { style: { color: C.sub, fontSize: 11, marginTop: 4 } }, (ARTIFACT_KIND[a.kind] || '文件') + ' · ' + Math.round((a.bytes || 0)/1024) + ' KB · ' + ((ARTIFACT_STATUS[a.status] || ARTIFACT_STATUS.legacy).label) + (a.node_name ? ' · 来源：' + a.node_name : ''))); }));
                });
              })() : h("div", { style: { padding: 15, color: C.sub, background: C.bg, borderRadius: 7 } }, "这次运行尚无可打开的成果。查看下面的模块，了解停在哪里。"),
              h("h3", { style: { fontSize: 15, marginTop: 22 } }, "模块工作区"),
              resultNodes.map(function (n) {
                return h("div", { key: n.id, "data-result-node": n.id, style: { display: "flex", gap: 6, marginBottom: 6 } },
                  h("button", { onClick: function () { openWorkspace(resultRun, n.id); }, style: { flex: 1, display: "flex", justifyContent: "space-between", gap: 10, padding: "11px 12px", background: C.card, border: "1px solid " + C.border, color: C.text, borderRadius: 7, textAlign: "left", cursor: "pointer", fontSize: 13 } },
                    h("span", null, n.name || n.id),
                    h("span", { style: { color: statusColor(n.status) } }, statusLabel(n.status) + " →")),
                  (n.agent_session_id && n.host_parent_session) ? h("button", { onClick: function () { openHostSession(n.agent_session_id, n.host_parent_session); }, style: { padding: "0 10px", borderRadius: 7, border: "1px solid " + C.teal, background: C.card, color: C.teal, fontSize: 11, cursor: "pointer" } }, "打开会话") : null);
              }),
              h("h3", { style: { fontSize: 15, marginTop: 22 } }, "验收概况"),
              (resultsData.checks || []).map(function (c, i) { return h("div", { key: i, style: { padding: "5px 0", fontSize: 12, color: c.status === "passed" ? C.teal : C.sub } }, (c.name || c.node || "检查项") + " · " + statusLabel(c.status)); }))
          )) : null,
        histOpen ? h("div", {
          "data-history-drawer": "1",
          style: { position: "absolute", right: 0, top: 0, bottom: 0, width: 440, zIndex: 34, background: C.panel, borderLeft: "1px solid " + C.border, boxShadow: "-12px 0 30px rgba(0,0,0,.4)", display: "flex", flexDirection: "column", padding: 14, boxSizing: "border-box" },
        },
          h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            h("div", { style: { fontSize: 15, fontWeight: 800 } }, "运行历史"),
            h("button", { onClick: openHistory, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭")),
          h("div", { style: { flex: 1, overflow: "auto", marginTop: 8, minHeight: 120 } },
            history.slice().reverse().map(function (r) {
              return h("div", {
                key: r.run_id, onClick: function () { loadRunDetail(r.run_id); },
                style: { padding: "6px 8px", marginBottom: 4, borderRadius: 6, cursor: "pointer", background: C.card, border: "1px solid " + (histDetail && histDetail.run_id === r.run_id ? C.accent : C.borderSoft) },
              },
                h("div", { style: { fontSize: 11, color: C.text } }, r.run_id),
                h("div", { style: { fontSize: 9.5, color: statusColor(r.status), fontFamily: "Consolas,monospace" } }, (r.status || "?") + " · " + (r.episode || "")));
            })),
          h("div", { style: { borderTop: "1px solid " + C.borderSoft, paddingTop: 8, marginTop: 6, maxHeight: "45%", overflow: "auto" } },
            histDetail ? h("div", null,
              h("div", { style: { fontSize: 11, fontWeight: 700, marginBottom: 5 } }, histDetail.run_id + " · " + histDetail.status),
              h("div", { style: { display: "flex", gap: 7, margin: "8px 0" } }, ToolButton("查看运行快照", function () { viewRun(histDetail.run_id); }), ToolButton("查看成果", function () { openResults(histDetail.run_id); })),
              h("div", { style: { fontSize: 11, color: C.faint, marginBottom: 4 } }, "点击节点打开工作区；历史默认只读，可显式创建修订。"),
              (histDetail.nodes || []).map(function (n) {
                return h("div", { key: n.id, onClick: function () { openWorkspace(histDetail.run_id, n.id); },
                  style: { display: "flex", justifyContent: "space-between", gap: 6, padding: "3px 0", borderLeft: "2px solid " + statusColor(n.status), paddingLeft: 6, fontSize: 10, cursor: "pointer" } },
                  h("span", { style: { color: C.sub } }, n.name || n.id),
                  h("span", { style: { color: statusColor(n.status), fontFamily: "Consolas,monospace" } }, n.status));
              }))
              : h("div", { style: { fontSize: 10, color: C.faint } }, "点一条运行查看节点状态"))
        ) : null,
        moduleHistory ? h("div", {
          "data-module-history": "1",
          style: { position: "absolute", inset: 0, zIndex: 97, background: "rgba(0,0,0,.65)", display: "flex", alignItems: "center", justifyContent: "center", padding: 20 },
        },
          h("div", { style: { width: 520, maxWidth: "90%", maxHeight: "85%", overflow: "auto", background: C.panel, border: "1px solid " + C.border, borderRadius: 10, padding: 16, boxSizing: "border-box" } },
            h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
              h("div", { style: { fontSize: 14, fontWeight: 800 } }, "版本历史 · " + (moduleHistory.module.name || moduleHistory.module.id)),
              h("button", { onClick: function () { setModuleHistory(null); }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭")),
            h("div", { style: { fontSize: 10, color: C.faint, marginTop: 6 } }, "回滚会生成一个新版本，不删除历史。"),
            h("div", { style: { marginTop: 10 } },
              (moduleHistory.versions || []).map(function (v) {
                return h("div", { key: v.version, style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: "6px 8px", marginBottom: 4, background: C.card, border: "1px solid " + C.borderSoft, borderRadius: 6 } },
                  h("div", null,
                    h("div", { style: { fontSize: 11, color: C.text } }, "v" + v.version + " · " + (v.name || "")),
                    h("div", { style: { fontSize: 9, color: C.faint } }, v.saved_at || "")),
                  h("button", { onClick: function () { rollbackModule(v.version); }, disabled: busy, style: { border: "1px solid " + C.accent, background: C.card, color: C.accent, borderRadius: 5, cursor: "pointer", fontSize: 9.5, padding: "3px 8px" } }, "回滚到此版本"));
              }),
              (moduleHistory.versions || []).length ? null : h("div", { style: { fontSize: 10.5, color: C.faint } }, "还没有历史版本"))
          )) : null,
        artifactModal ? h("div", {
          "data-artifact-modal": "1",
          style: { position: "absolute", inset: 0, zIndex: 98, background: "rgba(0,0,0,.7)", display: "flex", alignItems: "center", justifyContent: "center", padding: 20 },
        },
          h("div", { style: { width: "86%", maxWidth: 1000, maxHeight: "90%", background: C.panel, border: "1px solid " + C.border, borderRadius: 10, padding: 12, display: "flex", flexDirection: "column", boxSizing: "border-box" } },
            h("div", { style: { display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" } },
              h("div", { style: { minWidth: 0 } },
                h("div", { style: { fontSize: 11, color: C.sub, fontFamily: "Consolas,monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, artifactModal.path),
                artifactModal.status ? h("div", { style: { fontSize: 9.5, marginTop: 2, color: (ARTIFACT_STATUS[artifactModal.status] || {}).color || C.faint } }, (ARTIFACT_STATUS[artifactModal.status] || {}).label || artifactModal.status) : null),
              h("button", { onClick: function () { setArtifactModal(null); }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 6, cursor: "pointer", fontSize: 10, padding: "3px 8px" } }, "关闭")),
            h("div", { style: { flex: 1, minHeight: 0, overflow: "auto", display: "flex", justifyContent: "center", alignItems: "flex-start" } },
              artifactModal.kind === "image" ? h("img", {
                alt: artifactModal.path,
                onClick: function () { setArtifactModal(Object.assign({}, artifactModal, { zoom: artifactModal.zoom === 1 ? 2 : 1 })); },
                src: "data:" + artifactModal.mime + ";base64," + artifactModal.data,
                style: { marginTop: 8, maxWidth: artifactModal.zoom === 1 ? "100%" : "220%", maxHeight: artifactModal.zoom === 1 ? "72vh" : "none", objectFit: "contain", cursor: "zoom-in" },
              })
                : artifactModal.kind === "text" ? h("div", { style: { width: "100%", marginTop: 8, fontSize: 13, lineHeight: 1.8, color: C.sub, background: C.bg, padding: 12, borderRadius: 6 } }, /\.md$/i.test(artifactModal.path) ? readableDocument(artifactModal.text || "（空文档）") : h("pre", { style: { whiteSpace: "pre-wrap" } }, artifactModal.text || "（文件较大，请用下方下载查看）"))
                : artifactModal.kind === "video" ? h("video", { controls: true, autoPlay: true, src: artifactModal.url, style: { marginTop: 8, maxWidth: "100%", maxHeight: "72vh", background: "#000", borderRadius: 6 } })
                : artifactModal.kind === "audio" ? h("audio", { controls: true, autoPlay: true, src: artifactModal.url, style: { marginTop: 24, width: "100%" } })
                : artifactModal.kind === "pdf" ? h("iframe", { title: artifactModal.path, src: artifactModal.url, style: { width: "100%", height: "70vh", border: "none", marginTop: 8, background: "#fff", borderRadius: 6 } })
                : h("div", { style: { marginTop: 14, fontSize: 11, color: C.faint, lineHeight: 1.8 } },
                  "该类型不内嵌预览。文件大小 " + Math.round((artifactModal.bytes || 0) / 1024) + " KB",
                  h("br"),
                  h("a", { href: artifactModal.download || artifactModal.url, download: true, style: { color: C.teal } }, "下载这个文件"))),
            h("div", { style: { marginTop: 8, display: "flex", gap: 10, alignItems: "center", fontSize: 10, color: C.faint } },
              artifactModal.download ? h("a", { href: artifactModal.download, download: true, style: { color: C.teal, textDecoration: "none" } }, "下载") : null,
              artifactModal.status === 'verified' && artifactModal._source && artifactModal._source.sha256 ? ToolButton('确认沉淀为知识', promoteKnowledge) : null,
              artifactModal.kind === 'text' && artifactModal._source && artifactModal._source.generation > 1 ? ToolButton('与上一代比较文本', compareArtifact) : null,
              artifactModal._source ? h('span', null, '来源：' + artifactModal._source.nodeId + ' · 第 ' + artifactModal._source.generation + ' 代 · ' + String(artifactModal._source.sha256 || '').slice(0, 12)) : null,
              artifactModal.url && artifactModal.kind !== "text" ? h("a", { href: artifactModal.url, target: "_blank", rel: "noreferrer", style: { color: C.sub, textDecoration: "none" } }, "在新窗口打开") : null,
              artifactModal.zoom && artifactModal.kind === "image" ? h("button", { onClick: function () { setArtifactModal(Object.assign({}, artifactModal, { zoom: artifactModal.zoom === 1 ? 2 : 1 })); }, style: { border: "1px solid " + C.border, background: C.card, color: C.sub, borderRadius: 5, cursor: "pointer", fontSize: 9.5, padding: "2px 8px" } }, artifactModal.zoom === 1 ? "放大" : "适应窗口") : null)
          )
        ) : null,
        dialog ? h("div", {
          "data-dialog": "1",
          role: "dialog", "aria-modal": "true", "aria-label": dialog.title,
          style: { position: "absolute", inset: 0, zIndex: 99, background: "rgba(0,0,0,.55)", display: "flex", alignItems: "center", justifyContent: "center" },
        },
          h("div", { style: { width: 420, maxWidth: "80%", background: C.panel, border: "1px solid " + C.border, borderRadius: 12, boxShadow: "0 20px 60px rgba(0,0,0,.5)", padding: 16, boxSizing: "border-box" } },
            h("div", { style: { fontSize: 13, fontWeight: 700, lineHeight: 1.6 } }, dialog.title),
            dialog.type === "prompt" ? h("input", {
              autoFocus: true,
              value: dialog.value,
              onChange: function (e) { setDialog(Object.assign({}, dialog, { value: e.target.value })); },
              onKeyDown: function (e) { if (e.key === "Enter") closeDialog(true); if (e.key === "Escape") closeDialog(false); },
              style: { width: "100%", boxSizing: "border-box", marginTop: 12, padding: "8px 10px", borderRadius: 7, border: "1px solid " + C.border, background: C.bg, color: C.text, fontSize: 12, outline: "none" },
            }) : null,
            h("div", { style: { marginTop: 14, display: "flex", justifyContent: "flex-end", gap: 8 } },
              h("button", { onClick: function () { closeDialog(false); }, style: { padding: "6px 12px", borderRadius: 7, border: "1px solid " + C.border, background: C.card, color: C.sub, cursor: "pointer", fontSize: 11 } }, "取消"),
              h("button", { onClick: function () { closeDialog(true); }, style: { padding: "6px 12px", borderRadius: 7, border: "1px solid " + C.accent, background: C.accent, color: "#181207", fontWeight: 700, cursor: "pointer", fontSize: 11 } }, "确定"))
          )) : null
      );
    }

    function safeRegister(ctx, injectName, options, body) {
      try {
        ctx.slots.inject(injectName, function () {
          try {
            return ctx.slots.register(options, body);
          } catch (err) {
            console.warn("[workflow-panel] register failed: " + injectName, err);
            return function () {};
          }
        });
      } catch (err) {
        console.warn("[workflow-panel] inject failed: " + injectName, err);
      }
    }

    function apply(ctx) {
      safeRegister(ctx, "main", { name: "main", key: PANEL_ID }, function () { return h(CanvasApp, { ctx: ctx }); });
      safeRegister(ctx, "sidebar.panellist", {
        name: "sidebar.panellist", id: PANEL_ID, order: 10, label: function () { return "画布"; },
      }, Icon);

    }

    exports.name = "workflow-canvas-sidebar";
    exports.inject = ["slots"];
    exports.apply = apply;
    return module.exports;
  },
});


