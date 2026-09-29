'use strict';

/* ---------------- 状态与示例 ---------------- */

const SAMPLE = {
  channels: [
    { id: 'R', r: 1000, g: 120, b: 40,  power_mw: 3200 },
    { id: 'G', r: 160,  g: 1000, b: 90, power_mw: 2800 },
    { id: 'B', r: 60,   g: 80,  b: 1000, power_mw: 3400 },
    { id: 'W', r: 820,  g: 800, b: 760, power_mw: 6500 },
  ],
  target: { r: 600, g: 420, b: 180 },
  tolerance: 12,
};

// 标定示例样本（针对 SAMPLE 通道，按顺序给出驱动级；实测值由页面公式算得）
const SAMPLE_CALIB_DRIVES = [
  [255, 0,   0,   0  ],
  [0,   255, 0,   0  ],
  [0,   0,   255, 0  ],
  [0,   0,   0,   255],
  [128, 200, 64,  30 ],
];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const clone = (v) => JSON.parse(JSON.stringify(v));

let channels = clone(SAMPLE.channels);
// 最近一次求解结果（通道满档数据以服务端返回为准）
let solution = null;
// 最优驱动级（id -> drive），供“恢复最优”使用
let optimalDrives = null;
let manualMode = false;
// 标定样本：每条 { drives: {id:int}, measured:[r,g,b] }，按 id 存以适应通道增删
let calibSamples = [];
// 最近一次唯一标定结果（{id:{r,g,b}} + 提交时的驱动级，供实测校验/写回）
let calibUnique = null;

/* ---------------- 工具 ---------------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (ch) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function intVal(input, fallback = 0) {
  const v = input.value.trim();
  if (v === '' || !/^[+-]?\d+$/.test(v)) return fallback;
  return parseInt(v, 10);
}

// 最多 3 位小数，去掉多余的 0
function fmt(x) {
  const r = Math.round(x * 1000) / 1000;
  return Number.isInteger(r) ? String(r) : r.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}
function fmtSigned(x) { return (x > 0 ? '+' : '') + fmt(x); }

// 四舍五入（小数部分 ≥ 0.5 一律向上，不用 Math.round 的负数/偶舍分歧；此处值非负）
function roundHalfUp(x) { return Math.floor(x + 0.5); }

/** 线性 RGB（0～1000 标度）→ sRGB 显示色。仅显示时裁剪到 0～1。 */
function linearToSrgbHex(rgb) {
  const enc = (c) => {
    let v = c / 1000;
    v = Math.min(1, Math.max(0, v)); // 仅显示裁剪
    v = v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
    return Math.round(v * 255);
  };
  const h = (n) => n.toString(16).padStart(2, '0');
  return '#' + rgb.map(enc).map(h).join('');
}

function swatchHtml(rgb, title) {
  return `<span class="mini-swatch" style="background:${linearToSrgbHex(rgb)}" title="${esc(title)}"></span>`;
}

function showBanner(kind, html) {
  const el = $('#error-banner');
  el.className = `banner ${kind}`;
  el.innerHTML = html;
}
function clearBanner() { $('#error-banner').className = 'banner error hidden'; }

/* ---------------- 通道录入表 ---------------- */

function renderChannels() {
  const tb = $('#channel-tbody');
  tb.innerHTML = channels.map((ch, i) => `
    <tr data-i="${i}">
      <td class="id-cell"><input type="text" class="f-id" value="${esc(ch.id)}" maxlength="40"></td>
      <td><input type="number" class="f-r" min="0" max="1000" step="1" value="${ch.r}"></td>
      <td><input type="number" class="f-g" min="0" max="1000" step="1" value="${ch.g}"></td>
      <td><input type="number" class="f-b" min="0" max="1000" step="1" value="${ch.b}"></td>
      <td><input type="number" class="f-p" min="1" max="10000" step="1" value="${ch.power_mw}"></td>
      <td class="full-swatch"></td>
      <td><button type="button" class="btn small danger del-btn" ${channels.length <= 3 ? 'disabled' : ''}>删除</button></td>
    </tr>`).join('');
  refreshChannelSwatches();
  $('#add-channel').disabled = channels.length >= 6;
}

function refreshChannelSwatches() {
  $$('#channel-tbody tr').forEach((tr) => {
    const i = +tr.dataset.i;
    const ch = channels[i];
    const cell = tr.querySelector('.full-swatch');
    cell.innerHTML = swatchHtml([ch.r, ch.g, ch.b],
      `满档线性 RGB(${ch.r}, ${ch.g}, ${ch.b})`);
  });
}

function syncRowState(tr) {
  const i = +tr.dataset.i;
  channels[i] = {
    id: tr.querySelector('.f-id').value.trim(),
    r: intVal(tr.querySelector('.f-r')),
    g: intVal(tr.querySelector('.f-g')),
    b: intVal(tr.querySelector('.f-b')),
    power_mw: intVal(tr.querySelector('.f-p')),
  };
  refreshChannelSwatches();
}

function readForm() {
  $$('#channel-tbody tr').forEach((tr) => syncRowState(tr));
  const target = {
    r: intVal($('#target-r')),
    g: intVal($('#target-g')),
    b: intVal($('#target-b')),
  };
  const tolerance = intVal($('#tolerance'));
  return { target, tolerance };
}

function validateForm({ target, tolerance }) {
  const errs = [];
  if (!channels.every((c) => c.id)) errs.push('每路通道都必须填写 ID。');
  const ids = channels.map((c) => c.id);
  const dup = ids.filter((x, i) => x && ids.indexOf(x) !== i);
  if (dup.length) errs.push(`通道 ID 必须唯一，重复：${[...new Set(dup)].join('、')}。`);
  channels.forEach((c) => {
    const tag = c.id ? `“${c.id}”` : '某通道';
    for (const k of ['r', 'g', 'b']) {
      if (!Number.isInteger(c[k]) || c[k] < 0 || c[k] > 1000)
        errs.push(`通道 ${tag} 的满档 ${k.toUpperCase()} 必须是 0～1000 的整数。`);
    }
    if (c.r === 0 && c.g === 0 && c.b === 0)
      errs.push(`通道 ${tag} 的满档 RGB 不能全为零。`);
    if (!Number.isInteger(c.power_mw) || c.power_mw < 1 || c.power_mw > 10000)
      errs.push(`通道 ${tag} 的满档功率必须是 1～10000 mW 的整数。`);
  });
  for (const k of ['r', 'g', 'b']) {
    if (target[k] < 0 || target[k] > 1000)
      errs.push(`目标 ${k.toUpperCase()} 必须是 0～1000 的整数。`);
  }
  if (tolerance < 0 || tolerance > 20) errs.push('共用容差必须是 0～20 的整数。');
  return [...new Set(errs)];
}

/* ---------------- 目标色块（实时） ---------------- */

function refreshTargetSwatch() {
  const { target } = readForm();
  const rgb = [target.r, target.g, target.b];
  const hex = linearToSrgbHex(rgb);
  $('#target-swatch').style.background = hex;
  $('#target-swatch').title = `目标线性 RGB(${target.r}, ${target.g}, ${target.b})`;
  $('#target-hex').textContent = hex.toUpperCase();
  if (solution) {
    $('#target-swatch2').style.background = hex;
  }
}

/* ---------------- 求解 ---------------- */

async function onSolve() {
  clearBanner();
  const form = readForm();
  const errs = validateForm(form);
  if (errs.length) {
    showBanner('error', '<strong>录入有误：</strong><br>' + errs.map(esc).join('<br>'));
    return;
  }
  const btn = $('#solve-btn');
  btn.disabled = true;
  btn.textContent = '求解中…';
  try {
    const resp = await fetch('/api/solve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channels: channels.map(({ id, r, g, b, power_mw }) => ({ id, r, g, b, power_mw })),
        target_r: form.target.r,
        target_g: form.target.g,
        target_b: form.target.b,
        tolerance: form.tolerance,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      const detail = data.detail;
      let msg;
      if (Array.isArray(detail)) {
        msg = detail.map((d) => {
          const loc = (d.loc || []).filter((x) => x !== 'body').join('.');
          return `${loc ? loc + '：' : ''}${d.msg}`;
        }).join('<br>');
      } else {
        msg = esc(String(detail ?? '求解失败'));
      }
      showBanner('error', '<strong>请求被拒绝：</strong><br>' + msg);
      return;
    }
    renderSolution(data);
  } catch (e) {
    showBanner('error', `请求失败：${esc(e.message)}。请确认后端服务已启动。`);
  } finally {
    btn.disabled = false;
    btn.textContent = '求解最省功率配方';
  }
}

/* ---------------- 结果渲染与手动预览 ---------------- */

function currentDrives() {
  const drives = {};
  $$('#drive-tbody tr').forEach((tr) => {
    drives[tr.dataset.id] = intVal(tr.querySelector('.f-drive'), 0);
  });
  return drives;
}

function computePreview(drives) {
  const s = [0, 0, 0];
  let powerNum = 0;
  // 与 #drive-tbody 中的行顺序一一对应（渲染顺序与 solution.channels 相同）
  const rows = solution.channels.map((ch) => {
    const d = Math.max(0, Math.min(255, drives[ch.id] ?? 0));
    const full = [ch.r_full, ch.g_full, ch.b_full];
    const contrib = full.map((v) => (v * d) / 255);
    for (let c = 0; c < 3; c++) s[c] += full[c] * d; // 整数分子
    powerNum += ch.power_full_mw * d;
    return { ch, d, contrib };
  });
  const actual = s.map((v) => v / 255);
  return { rows, s, actual, powerNum };
}
function renderSolution(data) {
  solution = data;
  if (!data.feasible) {
    $('#result-panel').hidden = true;
    optimalDrives = null;
    showBanner('info',
      `<strong>⛔ 无可行配方</strong><br>${esc(data.message)}<br>` +
      `<span style="color:var(--muted)">未返回任何近似配方。</span>`);
    return;
  }
  clearBanner();
  $('#result-panel').hidden = false;
  optimalDrives = {};
  data.channels.forEach((c) => { optimalDrives[c.id] = c.drive; });
  manualMode = false;

  $('#drive-tbody').innerHTML = data.channels.map((c) => `
    <tr data-id="${esc(c.id)}">
      <td><strong>${esc(c.id)}</strong></td>
      <td><input type="number" class="f-drive" min="0" max="255" step="1" value="${c.drive}"></td>
      <td class="full-cell"></td>
      <td class="actual-cell"></td>
      <td class="power-cell"></td>
    </tr>`).join('');

  $('#tiebreak-hint').textContent =
    `字典序 tie-break（按 ID 字符串升序）：${data.sorted_ids.map(esc).join(' ＜ ')}`;
  refreshTargetSwatch();
  refreshPreview();
}

function refreshPreview() {
  if (!solution || !solution.feasible) return;
  const drives = currentDrives();
  const { rows, s, actual, powerNum } = computePreview(drives);
  const target = solution.target;
  const tol = solution.tolerance;

  // 驱动表：满档色 / 实际贡献色 / 功率贡献
  const trs = $$('#drive-tbody tr');
  rows.forEach(({ ch, d, contrib }, i) => {
    const tr = trs[i];
    const full = [ch.r_full, ch.g_full, ch.b_full];
    tr.querySelector('.full-cell').innerHTML =
      swatchHtml(full, `${ch.id} 满档 RGB(${full.join(', ')})`);
    tr.querySelector('.actual-cell').innerHTML =
      swatchHtml(contrib, `${ch.id} 驱动 ${d}/255 的线性贡献 RGB(${contrib.map(fmt).join(', ')})`);
    tr.querySelector('.power-cell').textContent =
      fmt((ch.power_full_mw * d) / 255);
  });

  // 实际色块
  const hex = linearToSrgbHex(actual);
  $('#actual-swatch').style.background = hex;
  $('#actual-hex').textContent = hex.toUpperCase();
  const clipped = actual.some((v) => v < 0 || v > 1000);
  $('#actual-swatch').title =
    `实际线性 RGB(${actual.map(fmt).join(', ')})` + (clipped ? '（超出 0～1000，显示已裁剪，模型不裁剪）' : '');

  // 分量对照表
  let allOk = true;
  const names = ['R', 'G', 'B'];
  $$('#component-table tbody tr').forEach((tr) => {
    const c = +tr.dataset.c;
    const lo = target[c] - tol, hi = target[c] + tol;
    const err = actual[c] - target[c];
    const ok = lo <= actual[c] && actual[c] <= hi && 0 <= actual[c] && actual[c] <= 1000;
    if (!ok) allOk = false;
    tr.querySelector('.lo').textContent = Math.max(0, lo);
    tr.querySelector('.tgt').textContent = target[c];
    tr.querySelector('.hi').textContent = Math.min(1000, hi);
    tr.querySelector('.act').innerHTML =
      `<span title="精确值 ${s[c]}/255">${fmt(actual[c])}</span>`;
    tr.querySelector('.err').textContent = fmtSigned(err);
    const j = tr.querySelector('.judge');
    j.textContent = ok ? '✔ 在区间内' : '✘ 超差';
    j.className = 'judge ' + (ok ? 'ok' : 'bad');
  });

  // 功率
  $('#total-power').textContent = fmt(powerNum / 255);
  $('#power-frac').textContent = `精确值 ${powerNum} / 255 mW`;

  // 状态标记
  const tag = $('#power-opt-tag');
  const isOptimal =
    !manualMode &&
    Object.entries(drives).every(([id, d]) => d === optimalDrives[id]);
  if (isOptimal) {
    tag.className = 'tag ok';
    tag.textContent = '最优（最小功率 + 字典序）';
  } else {
    tag.className = 'tag warn';
    tag.textContent = allOk ? '手动预览（仍满足约束）' : '手动预览（已超差，仅供参考）';
  }
  $('#restore-opt').hidden = isOptimal;
  $('#manual-hint').hidden = isOptimal;
}

function restoreOptimal() {
  if (!optimalDrives) return;
  $$('#drive-tbody tr').forEach((tr) => {
    tr.querySelector('.f-drive').value = optimalDrives[tr.dataset.id];
  });
  manualMode = false;
  refreshPreview();
}

/* ---------------- 满档读数标定 ---------------- */

function defaultCalibSamples() {
  // 依据当前 SAMPLE 通道的满档值生成无噪声示例样本
  return SAMPLE_CALIB_DRIVES.map((ds) => {
    const drives = {};
    SAMPLE.channels.forEach((ch, i) => { drives[ch.id] = ds[i] ?? 0; });
    return { drives, measured: predictedFromChannels(SAMPLE.channels, ds) };
  });
}

// 按 Σ(满档读数×驱动级)/255 四舍五入计算实测线性 RGB
function predictedFromChannels(chs, driveList) {
  const s = [0, 0, 0];
  chs.forEach((ch, i) => {
    const d = driveList[i] ?? 0;
    s[0] += ch.r * d; s[1] += ch.g * d; s[2] += ch.b * d;
  });
  return s.map((v) => roundHalfUp(v / 255));
}

// 通道增删/重命名后，把样本对齐到当前通道 id 集合
function reconcileCalibSamples() {
  const ids = new Set(channels.map((c) => c.id));
  calibSamples.forEach((smp) => {
    Object.keys(smp.drives).forEach((id) => {
      if (!ids.has(id)) delete smp.drives[id];
    });
    channels.forEach((c) => {
      if (smp.drives[c.id] === undefined) smp.drives[c.id] = 0;
    });
  });
}

function emptySample() {
  const drives = {};
  channels.forEach((c) => { drives[c.id] = 0; });
  return { drives, measured: [0, 0, 0] };
}

function renderCalibTable() {
  reconcileCalibSamples();
  const head = $('#sample-head-row');
  head.innerHTML =
    '<th>样本</th>' +
    channels.map((c) => `<th class="calib-drive-col">驱动 ${esc(c.id)}</th>`).join('') +
    '<th>实测 R</th><th>实测 G</th><th>实测 B</th><th></th>';

  const tb = $('#sample-tbody');
  tb.innerHTML = calibSamples.map((smp, si) => `
    <tr data-s="${si}">
      <td class="calib-idx">#${si + 1}</td>
      ${channels.map((c) => `
        <td><input type="number" class="cd-drive" data-id="${esc(c.id)}"
             min="0" max="255" step="1" value="${smp.drives[c.id] ?? 0}"></td>`).join('')}
      <td><input type="number" class="cd-m" data-c="0" min="0" max="6000" step="1" value="${smp.measured[0]}"></td>
      <td><input type="number" class="cd-m" data-c="1" min="0" max="6000" step="1" value="${smp.measured[1]}"></td>
      <td><input type="number" class="cd-m" data-c="2" min="0" max="6000" step="1" value="${smp.measured[2]}"></td>
      <td><button type="button" class="btn small danger sample-del" ${calibSamples.length <= 3 ? 'disabled' : ''}>删除</button></td>
    </tr>`).join('');
  $('#add-sample').disabled = calibSamples.length >= 12;
}

function syncCalibRow(tr) {
  const si = +tr.dataset.s;
  const smp = calibSamples[si];
  tr.querySelectorAll('.cd-drive').forEach((inp) => {
    smp.drives[inp.dataset.id] = intVal(inp, 0);
  });
  tr.querySelectorAll('.cd-m').forEach((inp) => {
    smp.measured[+inp.dataset.c] = intVal(inp, 0);
  });
}

function readCalibForm() {
  $$('#sample-tbody tr').forEach((tr) => syncCalibRow(tr));
  return calibSamples;
}

function validateCalibForm() {
  const errs = [];
  if (!channels.every((c) => c.id)) errs.push('每路通道都必须填写 ID，才能标定。');
  const ids = channels.map((c) => c.id);
  if (new Set(ids).size !== ids.length) errs.push('通道 ID 必须唯一，才能标定。');
  if (![3, 4, 5, 6].includes(channels.length))
    errs.push(`当前通道数为 ${channels.length}，标定仅支持 3～6 路。`);
  if (![3, 4, 5, 6, 7, 8, 9, 10, 11, 12].includes(calibSamples.length))
    errs.push(`标定样本数为 ${calibSamples.length}，必须录入 3～12 组。`);
  calibSamples.forEach((smp, si) => {
    channels.forEach((c) => {
      const d = smp.drives[c.id];
      if (!Number.isInteger(d) || d < 0 || d > 255)
        errs.push(`样本 #${si + 1} 通道 “${c.id}” 的驱动级必须是 0～255 的整数。`);
    });
    smp.measured.forEach((m, c) => {
      if (!Number.isInteger(m) || m < 0 || m > 6000)
        errs.push(`样本 #${si + 1} 的实测 ${'RGB'[c]} 必须是 0～6000 的整数。`);
    });
  });
  return [...new Set(errs)];
}

function clearCalibResult() {
  calibUnique = null;
  $('#calib-result').hidden = true;
  $('#calib-status').className = 'calib-status';
  $('#calib-status').textContent = '';
  $('#calib-unique-block').hidden = true;
}

async function onCalibrate() {
  clearBanner();
  const samples = readCalibForm();
  const errs = validateCalibForm();
  if (errs.length) {
    clearCalibResult();
    showBanner('error', '<strong>样本录入有误，已拒绝提交：</strong><br>' + errs.map(esc).join('<br>'));
    return;
  }
  const btn = $('#calib-btn');
  btn.disabled = true;
  btn.textContent = '反求中…';
  try {
    const orderedIds = channels.map((c) => c.id);
    const resp = await fetch('/api/calibrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channels: orderedIds.map((id) => ({ id })),
        samples: samples.map((smp) => ({
          drives: orderedIds.map((id) => smp.drives[id]),
          measured: smp.measured.slice(),
        })),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      const detail = data.detail;
      let msg;
      if (Array.isArray(detail)) {
        msg = detail.map((d) => {
          const loc = (d.loc || []).filter((x) => x !== 'body').join('.');
          return `${loc ? loc + '：' : ''}${d.msg}`;
        }).join('<br>');
      } else {
        msg = esc(String(detail ?? '标定失败'));
      }
      clearCalibResult();
      showBanner('error', '<strong>请求被拒绝：</strong><br>' + msg);
      return;
    }
    renderCalibResult(data, samples);
  } catch (e) {
    clearCalibResult();
    showBanner('error', `请求失败：${esc(e.message)}。请确认后端服务已启动。`);
  } finally {
    btn.disabled = false;
    btn.textContent = '反求满档读数';
  }
}

function renderCalibResult(data, submittedSamples) {
  const box = $('#calib-result');
  const status = $('#calib-status');
  box.hidden = false;
  const ICON = {
    unique: '✔',
    contradictory: '⛔',
    non_unique: '⚠',
    unknown: '⏳',
  };
  status.className = `calib-status s-${data.status}`;
  status.innerHTML =
    `<strong>${ICON[data.status] || ''} ${
      { unique: '标定唯一', contradictory: '样本矛盾',
        non_unique: '标定不唯一', unknown: '搜索未完成' }[data.status]
    }</strong> ${esc(data.message)}`;

  if (data.status !== 'unique') {
    // 非唯一结论：不显示任何参数，也不允许写回
    $('#calib-unique-block').hidden = true;
    calibUnique = null;
    return;
  }

  calibUnique = {
    readings: {},
    samples: clone(submittedSamples),
  };
  data.channels.forEach((c) => { calibUnique.readings[c.id] = { r: c.r, g: c.g, b: c.b }; });

  const curPower = {};
  channels.forEach((c) => { curPower[c.id] = c.power_mw; });
  $('#calib-tbody').innerHTML = data.channels.map((c) => {
    const rgb = [c.r, c.g, c.b];
    return `
      <tr data-id="${esc(c.id)}">
        <td><strong>${esc(c.id)}</strong></td>
        <td>${c.r}</td>
        <td>${c.g}</td>
        <td>${c.b}</td>
        <td>${swatchHtml(rgb, `${c.id} 反求满档 RGB(${c.r}, ${c.g}, ${c.b})`)}</td>
        <td>${curPower[c.id] ?? '—'}</td>
      </tr>`;
  }).join('');

  // 逐样本残差总表：用反求读数重算预测实测，与录入实测对照
  const ids = channels.map((c) => c.id);
  const rowsHtml = submittedSamples.map((smp, si) => {
    const s = [0, 0, 0];
    ids.forEach((id) => {
      const rd = calibUnique.readings[id];
      const d = smp.drives[id] ?? 0;
      s[0] += rd.r * d; s[1] += rd.g * d; s[2] += rd.b * d;
    });
    const pred = s.map((v) => roundHalfUp(v / 255));
    const diffs = pred.map((p, c) => p - smp.measured[c]);
    const ok = diffs.every((x) => x === 0);
    return `<tr>
      <td>#${si + 1}</td>
      <td>${pred.join(', ')}</td>
      <td>${smp.measured.join(', ')}</td>
      <td class="${ok ? 'judge ok' : 'judge bad'}">${ok ? '✔ 一致' : '✘ ' + diffs.join(', ')}</td>
    </tr>`;
  }).join('');
  $('#calib-residual-table tbody').innerHTML = rowsHtml;

  const missing = ids.filter((id) => !calibUnique.readings[id]);
  const meta = $('#writeback-meta');
  const wb = $('#writeback-btn');
  if (missing.length) {
    wb.disabled = true;
    meta.textContent = `当前通道与标定通道不一致（缺少：${missing.join('、')}），请恢复标定所用通道后再写回。`;
  } else {
    wb.disabled = false;
    meta.textContent = '写回只更新各路满档 R/G/B，满档功率保持当前值不变。';
  }
  $('#calib-unique-block').hidden = false;
}

function onWriteback() {
  if (!calibUnique) return;
  $$('#channel-tbody tr').forEach((tr) => syncRowState(tr));
  let applied = 0;
  channels.forEach((c) => {
    const rd = calibUnique.readings[c.id];
    if (rd) {
      c.r = rd.r; c.g = rd.g; c.b = rd.b;
      applied += 1;
    }
  });
  renderChannels();
  // 通道满档值已变，旧配方结果失效
  solution = null;
  optimalDrives = null;
  $('#result-panel').hidden = true;
  showBanner('info',
    `<strong>✔ 已写回 ${applied} 路满档 RGB</strong>，各路满档功率保持原值。` +
    `可直接在 ② 设置目标色后点击“求解最省功率配方”。`);
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  $('#add-channel').addEventListener('click', () => {
    if (channels.length >= 6) return;
    let n = channels.length + 1;
    let id = `LED${n}`;
    const used = new Set(channels.map((c) => c.id));
    while (used.has(id)) { n += 1; id = `LED${n}`; }
    channels.push({ id, r: 500, g: 500, b: 500, power_mw: 3000 });
    reconcileCalibSamples();
    renderChannels();
    renderCalibTable();
    clearCalibResult();
  });

  $('#reset-sample').addEventListener('click', () => {
    channels = clone(SAMPLE.channels);
    $('#target-r').value = SAMPLE.target.r;
    $('#target-g').value = SAMPLE.target.g;
    $('#target-b').value = SAMPLE.target.b;
    $('#tolerance').value = SAMPLE.tolerance;
    calibSamples = defaultCalibSamples();
    renderChannels();
    renderCalibTable();
    clearCalibResult();
    refreshTargetSwatch();
    clearBanner();
  });

  $('#channel-tbody').addEventListener('input', (e) => {
    const tr = e.target.closest('tr');
    if (tr) syncRowState(tr);
  });
  $('#channel-tbody').addEventListener('click', (e) => {
    if (!e.target.classList.contains('del-btn')) return;
    const tr = e.target.closest('tr');
    channels.splice(+tr.dataset.i, 1);
    reconcileCalibSamples();
    renderChannels();
    renderCalibTable();
    clearCalibResult();
  });

  ['#target-r', '#target-g', '#target-b', '#tolerance'].forEach((sel) =>
    $(sel).addEventListener('input', refreshTargetSwatch));

  $('#solve-btn').addEventListener('click', onSolve);

  $('#drive-tbody').addEventListener('input', (e) => {
    if (e.target.classList.contains('f-drive')) {
      manualMode = true;
      refreshPreview();
    }
  });
  $('#restore-opt').addEventListener('click', restoreOptimal);

  // ---- 标定 ----
  $('#add-sample').addEventListener('click', () => {
    if (calibSamples.length >= 12) return;
    calibSamples.push(emptySample());
    renderCalibTable();
    clearCalibResult();
  });

  $('#gen-samples').addEventListener('click', () => {
    // 用当前通道表中的满档 RGB 合成“每路单独满档点亮”的示例样本（功率不参与）
    const errs = [];
    channels.forEach((c) => {
      for (const k of ['r', 'g', 'b']) {
        if (!Number.isInteger(c[k]) || c[k] < 0 || c[k] > 1000)
          errs.push(`通道 “${c.id}” 的满档 ${k.toUpperCase()} 非法（须 0～1000 整数）。`);
      }
      if (c.r === 0 && c.g === 0 && c.b === 0)
        errs.push(`通道 “${c.id}” 满档 RGB 全为零，无法生成示例样本。`);
    });
    if (errs.length) {
      showBanner('error', '<strong>当前通道表不能生成示例样本：</strong><br>' +
        [...new Set(errs)].map(esc).join('<br>'));
      return;
    }
    clearBanner();
    calibSamples = channels.map((_, i) => {
      const drives = channels.map(() => 0);
      drives[i] = 255;
      const drivesById = {};
      channels.forEach((c, j) => { drivesById[c.id] = drives[j]; });
      return { drives: drivesById, measured: predictedFromChannels(channels, drives) };
    });
    renderCalibTable();
    clearCalibResult();
  });

  $('#sample-tbody').addEventListener('input', (e) => {
    const tr = e.target.closest('tr');
    if (tr) syncCalibRow(tr);
    clearCalibResult();
  });
  $('#sample-tbody').addEventListener('click', (e) => {
    if (!e.target.classList.contains('sample-del')) return;
    const tr = e.target.closest('tr');
    calibSamples.splice(+tr.dataset.s, 1);
    renderCalibTable();
    clearCalibResult();
  });

  $('#calib-btn').addEventListener('click', onCalibrate);
  $('#writeback-btn').addEventListener('click', onWriteback);
}

/* ---------------- 启动 ---------------- */

calibSamples = defaultCalibSamples();
renderChannels();
renderCalibTable();
refreshTargetSwatch();
bindEvents();
