"use strict";
const $ = (s, r = document) => r.querySelector(s);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
  });
  const type = res.headers.get("Content-Type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw Object.assign(new Error(body.message || res.statusText),
    { status: res.status, body });
  return body;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const toast = (msg, err = false) => {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  t.style.display = "block";
  clearTimeout(toast._h); toast._h = setTimeout(() => t.style.display = "none", 6000);
};
const pill = (s) => `<span class="pill ${esc(s)}">${esc(s)}</span>`;

const TABS = [
  ["import", "导入"],
  ["browse", "版本与条款"],
  ["graph", "某日有效图"],
  ["diff", "日期对比"],
  ["decide", "引用决议"],
  ["snapshots", "研究快照"],
  ["merge", "两人合并"],
  ["export", "导出/锚点/恢复"],
];
let tab = location.hash.slice(1) || "import";
function nav() {
  $("#tabs").innerHTML = TABS.map(([id, label]) =>
    `<button data-t="${id}" class="${id === tab ? "active" : ""}">${label}</button>`
  ).join("");
  $("#tabs").querySelectorAll("button").forEach(b => b.onclick = () => {
    tab = b.dataset.t; location.hash = tab; render();
  });
}
window.addEventListener("hashchange", () => { tab = location.hash.slice(1); render(); });

async function render() {
  nav();
  const fn = VIEWS[tab] || VIEWS.import;
  $("#view").innerHTML = "";
  await fn($("#view"));
}

const VIEWS = {};

VIEWS.import = async (el) => {
  el.innerHTML = `
  <div class="card"><h2>导入法规版本（原文逐字保存）</h2>
    <div class="row">
      <div><label>文档代码</label><input id="code" value="ALPHA"></div>
      <div><label>文档标题</label><input id="title" value="甲法"></div>
      <div><label>版本标签（同代码唯一）</label><input id="label" value="甲法-2025"></div>
    </div>
    <div class="row">
      <div><label>通过日期</label><input id="adopted" placeholder="2024-12-01"></div>
      <div><label>生效日期</label><input id="effective" value="2025-01-01"></div>
      <div><label>失效日期(可选)</label><input id="end" placeholder="YYYY-MM-DD"></div>
      <div><label>状态</label><select id="state">
        <option value="adopted">adopted（已通过）</option>
        <option value="effective">effective（已生效）</option>
      </select></div>
      <div><label>幂等键(可选)</label><input id="idem" placeholder="重复请求安全"></div>
    </div>
    <label>原文（规范化/修订必须另存新版本，不会覆盖此文本）</label>
    <textarea id="raw">第一章 总则
第一条 ……
</textarea>
    <div class="row"><div><label>导入方式</label><select id="stage">
      <option value="final">直接完成</option>
      <option value="staged">先暂存（验证崩溃恢复）</option>
    </select></div></div>
    <button class="act" id="go">导入</button>
    <button class="act ghost" id="finalize">完成最近的暂存版本</button>
    <div id="out" class="small muted" style="margin-top:10px"></div>
  </div>`;
  $("#go", el).onclick = async () => {
    const payload = {
      code: $("#code", el).value.trim(), title: $("#title", el).value.trim(),
      label: $("#label", el).value.trim(),
      raw_text: $("#raw", el).value,
      effective_date: $("#effective", el).value.trim(),
      adopted_date: $("#adopted", el).value.trim() || null,
      end_date: $("#end", el).value.trim() || null,
      state: $("#state", el).value,
      idempotency_key: $("#idem", el).value.trim() || undefined,
      stage: $("#stage", el).value,
    };
    try {
      const r = await api("/api/imports", { method: "POST",
        body: JSON.stringify(payload) });
      $("#out", el).textContent = JSON.stringify(r, null, 2);
      toast(r.replayed ? "幂等：返回首次结果" : "导入完成");
    } catch (e) { toast(e.message + "\n" + JSON.stringify(e.body?.base ?? e.body ?? {},
      null, 2), true); }
  };
  $("#finalize", el).onclick = async () => {
    const rec = await api("/api/recovery");
    const versions = (await api("/api/versions")).versions
      .filter(v => v.import_stage === "staged");
    if (!versions.length) return toast("没有 staged 版本");
    for (const v of versions) {
      try { await api(`/api/versions/${v.id}/finalize`, { method: "POST",
        body: "{}" }); toast(`已完成暂存版本 #${v.id}`); }
      catch (e) { toast(e.message, true); }
    }
  };
};

VIEWS.browse = async (el) => {
  const [{ documents }, { versions }] = await Promise.all(
    [api("/api/documents"), api("/api/versions")]);
  el.innerHTML = `<div class="card"><h2>文档</h2>
    <table><thead><tr><th>代码</th><th>标题</th><th>版本数</th></tr></thead>
    <tbody>${documents.map(d => `<tr><td>${esc(d.code)}</td><td>${esc(d.title)}</td>
      <td>${d.version_count}</td></tr>`).join("")}</tbody></table></div>
    <div class="card"><h2>版本与生命周期（非法跳转返回 409）</h2>
    <table><thead><tr><th>ID</th><th>文档</th><th>版本</th><th>状态</th>
      <th>生效</th><th>失效</th><th>条款</th><th>操作</th></tr></thead><tbody>
    ${versions.map(v => `<tr>
      <td>${v.id}</td><td>${esc(v.code)}</td><td>${esc(v.version_label)}</td>
      <td>${pill(v.state)}<div class="muted small">${esc(v.import_stage)}</div></td>
      <td>${esc(v.effective_date)}</td><td>${esc(v.end_date || "")}</td>
      <td>${v.article_count}</td>
      <td>${["effective", "superseded", "repealed"].filter(s =>
        transitions(v.state).includes(s)).map(s =>
        `<button class="ghost small" data-id="${v.id}" data-s="${s}">→${s}</button>`
      ).join(" ")} <a href="#" data-raw="${v.id}">原文</a></td>
    </tr>`).join("")}
    </tbody></table><pre id="rawbox" hidden></pre></div>`;
  el.querySelectorAll("button[data-s]").forEach(b => b.onclick = async () => {
    try { await api(`/api/versions/${b.dataset.id}/transition`, { method: "POST",
      body: JSON.stringify({ state: b.dataset.s }) });
      toast(`版本 #${b.dataset.id} → ${b.dataset.s}`); render();
    } catch (e) { toast(e.message, true); }
  });
  el.querySelectorAll("a[data-raw]").forEach(a => a.onclick = async (ev) => {
    ev.preventDefault();
    const text = await fetch(`/api/versions/${a.dataset.raw}/raw`).then(r => r.text());
    const box = $("#rawbox", el); box.hidden = false; box.textContent = text;
  });
};
const transitions = (s) => ({
  adopted: ["effective", "repealed"],
  effective: ["superseded", "repealed"],
  superseded: ["repealed"], repealed: [],
}[s] || []);

let graphCache = null;
VIEWS.graph = async (el) => {
  el.innerHTML = `<div class="card"><h2>按日期查看当时有效图</h2>
    <div class="row"><div><label>日期</label><input id="date" type="date"
      value="2024-01-01"></div></div>
    <button class="act" id="go">查看</button>
    <div id="sum" class="small muted" style="margin-top:8px"></div></div>
    <div class="card" id="edges"></div>`;
  $("#go", el).onclick = async () => loadGraph($("#date", el).value, el);
  await loadGraph($("#date", el).value, el);
};
async function loadGraph(date, el) {
  const g = await api(`/api/graph?date=${encodeURIComponent(date)}`);
  graphCache = g;
  $("#sum", el).textContent = `节点 ${g.counts.nodes} · 边 ${g.counts.edges} · 问题 ${g.counts.problems}`;
  $("#edges", el).innerHTML = `<h2>边（点击查看原句、候选与选择依据）</h2>
    <table><thead><tr><th>引用</th><th>所在条款</th><th>目标</th>
      <th>解析</th><th>最短链</th></tr></thead><tbody>
    ${g.edges.map(e => `<tr style="cursor:pointer" data-cid="${e.id}">
      <td class="quote">${esc(e.text)}</td>
      <td>第${esc(e.source_article_num)}条</td>
      <td>${esc(e.target_desc || "—")}</td>
      <td>${pill(e.status)}<div class="muted small">${esc(e.resolution_status || "")}</div></td>
      <td class="chain">${(g.chains[e.id] || []).join(" → ") || ""}</td>
    </tr>`).join("")}</tbody></table>`;
  el.querySelectorAll("tr[data-cid]").forEach(r => r.onclick = () => {
    location.hash = "decide"; setTimeout(() => openCitation(r.dataset.cid), 50);
  });
}

VIEWS.diff = async (el) => {
  el.innerHTML = `<div class="card"><h2>比较两个日期的图</h2>
    <div class="row"><div><label>A</label><input id="a" type="date" value="2021-01-01"></div>
    <div><label>B</label><input id="b" type="date" value="2024-01-01"></div></div>
    <button class="act" id="go">比较</button><div id="out" style="margin-top:10px"></div></div>`;
  $("#go", el).onclick = async () => {
    const d = await api(`/api/diff?a=${$("#a", el).value}&b=${$("#b", el).value}`);
    const edgeRow = e => `<tr><td class="quote">${esc(e.text)}</td>
      <td>第${esc(e.source_article_num)}条</td><td>${esc(e.target_desc || "—")}</td>
      <td>${pill(e.status)}</td></tr>`;
    $("#out", el).innerHTML = `
      <p>新增 ${d.counts.added} · 消失 ${d.counts.removed} · 重定向 ${d.counts.redirected}</p>
      <h2>重定向</h2><table><thead><tr><th>引用</th><th>A 目标</th><th>B 目标</th></tr></thead>
      <tbody>${d.redirected.map(r => `<tr><td class="quote">${esc(r.from.text)}</td>
        <td>${esc(r.from.target_desc)}</td><td>${esc(r.to.target_desc)}</td></tr>`).join("")}
      </tbody></table>
      <h2>新增</h2><table><thead><tr><th>引用</th><th>条款</th><th>目标</th><th>状态</th></tr></thead>
      <tbody>${d.added.map(edgeRow).join("")}</tbody></table>
      <h2>消失</h2><table><thead><tr><th>引用</th><th>条款</th><th>目标</th><th>状态</th></tr></thead>
      <tbody>${d.removed.map(edgeRow).join("")}</tbody></table>`;
  };
};

VIEWS.decide = async (el) => {
  const { versions } = await api("/api/versions");
  const final = versions.filter(v => v.import_stage === "final");
  el.innerHTML = `<div class="card"><h2>引用决议</h2>
    <div class="row"><div><label>引用 ID</label><input id="cid" placeholder="例如 6"></div></div>
    <button class="act" id="open">打开</button>
    <p class="muted small">从“某日有效图”点击任一边可直达此处。</p>
    <div id="detail"></div></div>`;
  $("#open", el).onclick = () => openCitation($("#cid", el).value, true);
};
async function openCitation(cid, scroll) {
  const el = document.querySelector("#view");
  if (!location.hash.endsWith("decide")) location.hash = "decide";
  await render();
  const box = $("#detail", el) || el;
  let data;
  try { data = await api(`/api/citations/${cid}`); }
  catch (e) { return toast(e.message, true); }
  const c = data.citation, res = data.resolution;
  box.innerHTML = `<div class="card">
    <h2>引用 #${c.id}（版本 #${c.version_id} 第${esc(c.source_article_num)}条）</h2>
    <p><span class="quote">${esc(c.text)}</span></p>
    <p class="muted">原句：${esc(c.sentence)}</p>
    <p>自动状态：${res ? pill(res.status) : "未解析"}
       <span class="muted small">${esc(res?.basis?.why || "")}</span></p>
    <h2>候选目标（保留全部候选与依据）</h2>
    <table><thead><tr><th>#</th><th>目标</th><th>状态</th><th>分值</th><th>依据</th></tr></thead>
    <tbody>${data.candidates.map(k => `<tr${res?.selected_candidate_id &&
      k.id === res.selected_candidate_id ? ' style="background:#eff6ff"' : ""}>
      <td>${k.rank}</td><td>${esc(k.target_desc)}</td><td>${pill(k.status)}</td>
      <td>${k.score}</td><td class="small">${esc(k.basis.reason || "")}
      ${k.basis.article ? `（第${k.basis.article}条）` : ""}</td></tr>`).join("")}
    </tbody></table>
    <h2>研究者决定（版本号冲突会返回双方差异）</h2>
    <div class="row">
      <div><label>研究者</label><input id="who" value="alice"></div>
      <div><label>动作</label><select id="action">
        <option value="override">纠正目标</option>
        <option value="version_range">声明版本区间</option>
        <option value="clear">清除本人决定</option></select></div>
      <div><label>目标版本</label><select id="tvid">
        <option value="">(无)</option>${(await api("/api/versions")).versions.map(v =>
          `<option value="${v.id}">#${v.id} ${esc(v.version_label)}</option>`).join("")}
      </select></div>
      <div><label>目标单元 ID(可选)</label><input id="tuid" placeholder="units.id"></div>
    </div>
    <div class="row"><div><label>适用起(可选)</label><input id="vf" placeholder="2023-01-01"></div>
      <div><label>适用止(可选)</label><input id="vt" placeholder="2025-01-01"></div>
      <div><label>来源页</label><input id="page" placeholder="纸本/公报页码"></div></div>
    <label>说明</label><input id="note">
    <p class="small muted">当前 decision_rev = <b>${c.decision_rev}</b></p>
    <button class="act" id="save">提交（base_rev=${c.decision_rev}）</button>
    <h2>已有决定（按研究者分别保留）</h2>
    <table><thead><tr><th>研究者</th><th>动作</th><th>目标版本</th>
      <th>区间</th><th>来源页</th></tr></thead><tbody>
    ${data.decisions.map(d => `<tr><td>${esc(d.researcher)}</td><td>${esc(d.action)}</td>
      <td>#${esc(d.target_version_id || "")}</td>
      <td>${esc(d.valid_from || "")} ~ ${esc(d.valid_to || "")}</td>
      <td>${esc(d.source_page)}</td></tr>`).join("")}</tbody></table>
  </div>`;
  $("#save", box).onclick = async () => {
    const payload = {
      researcher: $("#who", box).value.trim(),
      action: $("#action", box).value,
      target_version_id: Number($("#tvid", box).value) || null,
      target_unit_id: Number($("#tuid", box).value) || null,
      valid_from: $("#vf", box).value || null, valid_to: $("#vt", box).value || null,
      source_page: $("#page", box).value, note: $("#note", box).value,
      base_rev: c.decision_rev,
    };
    try {
      const r = await api(`/api/citations/${c.id}/decisions`,
        { method: "POST", body: JSON.stringify(payload) });
      toast(`已提交，新 rev=${r.decision_rev}；重解析 ${r.reparsed.length} 条`);
      openCitation(c.id);
    } catch (e) {
      toast("冲突/错误（双方差异已返回）：\n" +
        JSON.stringify({ yours: { rev: e.body?.base }, theirs: e.body?.current },
          null, 2), true);
    }
  };
}
window.openCitation = openCitation;

VIEWS.snapshots = async (el) => {
  const { snapshots } = await api("/api/snapshots");
  el.innerHTML = `<div class="card"><h2>研究快照（发布后不可变）</h2>
    <div class="row"><div><label>标签</label><input id="label" value="阶段汇报"></div>
    <div><label>日期</label><input id="date" type="date" value="2024-01-01"></div>
    <div><label>研究者</label><input id="who" value="alice"></div></div>
    <button class="act" id="create">创建开放快照</button>
    <div id="list" style="margin-top:12px"></div></div>`;
  async function list_() {
    const r = await api("/api/snapshots");
    $("#list", el).innerHTML = `<table><thead><tr><th>ID</th><th>标签</th>
      <th>状态</th><th>rev</th><th>指纹</th><th></th></tr></thead><tbody>
      ${r.snapshots.map(s => `<tr><td>${s.id}</td><td>${esc(s.label)}</td>
      <td>${pill(s.state)}</td><td>${s.revision}</td>
      <td class="small">${esc(s.payload_fingerprint.slice(0, 22))}…</td>
      <td>${s.state === "open"
        ? `<button data-p="${s.id}" data-r="${s.revision}">发布</button>` : ""}
        <a href="/api/snapshots/${s.id}" target="_blank">查看</a></td></tr>`).join("")}
      </tbody></table>`;
    el.querySelectorAll("button[data-p]").forEach(b => b.onclick = async () => {
      try { await api(`/api/snapshots/${b.dataset.p}/publish`, { method: "POST",
        body: JSON.stringify({ base_rev: Number(b.dataset.r) }) });
        toast("已发布；此后再决议不会改变该快照"); list_();
      } catch (e) { toast(e.message, true); }
    });
  }
  $("#create", el).onclick = async () => {
    await api("/api/snapshots", { method: "POST", body: JSON.stringify({
      label: $("#label", el).value, date: $("#date", el).value,
      researcher: $("#who", el).value || "alice" }) });
    list_();
  };
  await list_();
};

VIEWS.merge = async (el) => {
  el.innerHTML = `<div class="card"><h2>两位研究者从同一版本分别修正后的合并</h2>
    <div class="row">
      <div><label>基准版本 ID</label><input id="base" value="2"></div>
      <div><label>研究者 A</label><input id="a" value="alice"></div>
      <div><label>研究者 B</label><input id="b" value="bob"></div>
      <div><label>标签</label><input id="label" value="合并 2023 版修正"></div>
    </div>
    <button class="act" id="go">创建合并会话</button>
    <div id="out" style="margin-top:12px"></div></div>`;
  $("#go", el).onclick = async () => {
    let session;
    try {
      session = await api("/api/merges", { method: "POST", body: JSON.stringify({
        label: $("#label", el).value, base_version_id: Number($("#base", el).value),
        researcher_a: $("#a", el).value, researcher_b: $("#b", el).value }) });
    } catch (e2) { return toast(e2.message, true); }
    const detail = await api(`/api/merges/${session.session_id}`);
    const rows = Object.entries(detail.items).map(([cid, sides]) => {
      const divergent = detail.divergences.map(Number).includes(Number(cid));
      return `<tr class="${divergent ? "diverge" : ""}"><td>${cid}</td>
        ${["A", "B"].map(side => {
          const s = sides.find(x => x.side === side);
          return `<td>${s ? `<b>${esc(s.researcher)}</b><br>
            版本#${esc(s.target_version_id)} 单元#${esc(s.target_unit_id)}<br>
            <span class="muted">${esc(s.valid_from || "")}~${esc(s.valid_to || "")}</span><br>
            来源页：${esc(s.source_page)}<br>
            <pre>${esc(s.context)}</pre>` : "（无决定）"}</td>`;
        }).join("")}
        <td>${divergent ? "目标不一致：双方上下文均保留" : "一致"}</td></tr>`;
    }).join("");
    $("#out", el).innerHTML = `
      <p>会话 #${detail.id}，rev=${detail.revision}；分歧
      <b>${detail.divergences.length}</b> 条（绝不以后保存者覆盖）。</p>
      <table><thead><tr><th>引用</th><th>A 方上下文/来源页</th>
      <th>B 方上下文/来源页</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="small muted">分歧在合并导出中以 merge_items 保留；服务端不会自动二选一。</p>`;
  };
};

VIEWS.export = async (el) => {
  el.innerHTML = `<div class="card"><h2>确定性导出</h2>
    <p class="small muted">ZIP 固定时间戳、JSON sort_keys；相同数据导出字节一致。
      包含 manifest（解析器版本）、versions 原文与双指纹、citations、
      candidates、resolutions、anchors。</p>
    <a class="act" style="text-decoration:none;display:inline-block"
      href="/api/export" download>下载 citeweave-bundle.zip</a>
    <button class="act ghost" id="verify">校验锚点（模拟换行变化）</button>
    <pre id="rep" hidden></pre></div>
    <div class="card"><h2>崩溃恢复</h2>
    <div id="rec"></div></div>`;
  const rec = await api("/api/recovery");
  $("#rec", el).innerHTML = rec.abandoned_stages.length
    ? `<p>检测到 ${rec.abandoned_stages.length} 个被进程异常退出遗留的暂存版本，
       已在启动时标记 abandoned，需重新导入：</p>
       <pre>${esc(JSON.stringify(rec.abandoned_stages, null, 2))}</pre>`
    : "<p class='muted'>没有遗留暂存版本。</p>";
  $("#verify", el).onclick = async () => {
    const r = await api("/api/anchors/verify");
    $("#rep", el).hidden = false;
    $("#rep", el).textContent = JSON.stringify(r.anchors, null, 2);
  };
};

render();
