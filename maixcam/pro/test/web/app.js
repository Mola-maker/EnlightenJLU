(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const EFFECT_LABELS = {
    solid: "常亮", breath: "呼吸", wave: "波浪", chase: "追逐",
    sparkle: "星光", theater: "交替", comet: "流星", pulse: "心跳",
  };
  const MODE_LABELS = {
    kws: "关键词待机", direct: "网页波形", off: "关闭",
    firmware: "固件特效",
  };
  const LINK_LABELS = {
    0: "BOOT", 1: "WAIT_HELLO", 2: "ACTIVE", 3: "LOST", 4: "FAULT",
  };
  const KWS_LABELS = {
    starting: "启动中", listening: "监听中", trigger: "已触发",
    paused: "已暂停", unavailable: "未接入",
  };
  const VIEWS = {
    lamp: { eyebrow: "MAIXCAM · STM32 · LAMP WAVEFORM", title: "灯牌波形" },
    heat: { eyebrow: "MAIXCAM · STM32 · HEATER WAVEFORM", title: "加热波形" },
    pid: { eyebrow: "PID · ANCHOR / MARGIN", title: "PID 锚点" },
    kws: { eyebrow: "KWS · KEYWORD RECOGNITION", title: "关键词识别" },
  };
  let currentView = "lamp";
  const state = {
    link: { state: -1, fault_flags: 0 },
    lamp: {
      mode: "kws", effect: "breath", brightness: 75, duties: Array(13).fill(0),
      target: 0x1fff, active_mask: 0, link_state: -1, last_event_seq: 0,
      direct_ready: false, message: "",
    },
    kws: {
      state: "starting", last_keyword: null, last_probability: null,
      last_trigger_at: null, trigger_count: 0,
      scores: { hello: 0, welcome: 0 },
    },
    thermal: {
      temp_c: null, temp_valid: false, setpoint_c: 43, margin_c: 3,
      ambient_c: -4, anchor_c: 21, determined_at: "—",
      kp: 60, ki: 1, kd: 0, max_duty: 300, enabled: false,
      duty: 0, p: 0, i: 0, d: 0, overtemp: false,
      slope_c_per_s: null, no_response: false,
    },
  };
  let history = { lamp: [], thermal: [] };
  let lampBars = [];
  let lampDragging = false;
  let brightnessTimer = null;
  let pidDirty = false;
  let stateTimer = null;
  let historyTimer = null;

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }
  function formatTime(epochSeconds) {
    if (!epochSeconds) return "—";
    return new Date(epochSeconds * 1000).toLocaleTimeString(
      "zh-CN", { hour12: false });
  }
  function toast(text, error = false) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("error", error);
    el.classList.add("show");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function showView(name) {
    if (!VIEWS[name]) return;
    currentView = name;
    document.querySelectorAll(".mode[data-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.view === name);
    });
    document.querySelectorAll(".view-card[data-view]").forEach((card) => {
      card.classList.toggle("is-active", card.dataset.view === name);
    });
    $("#viewEyebrow").textContent = VIEWS[name].eyebrow;
    $("#pageTitle").textContent = VIEWS[name].title;
    window.scrollTo({ top: 0, behavior: "smooth" });
    requestAnimationFrame(() => {
      drawLampChart();
      drawHeatChart();
    });
  }

  function buildLampBars() {
    const root = $("#lampBars");
    for (let i = 0; i < 13; i += 1) {
      const cell = document.createElement("div");
      cell.className = "lamp-unit";
      cell.innerHTML = `<i></i><b>${String(i + 1).padStart(2, "0")}</b>`;
      root.appendChild(cell);
      lampBars.push(cell.querySelector("i"));
    }
  }

  function setLinkUI(linkState) {
    const active = linkState === 2;
    ["#railDot", "#connectionDot"].forEach((sel) => {
      const el = $(sel);
      el.classList.toggle("online", active);
      el.classList.toggle("offline", !active);
    });
    $("#railLink").textContent = active ? "SPI 在线" : "SPI 未连接";
    $("#connectionText").textContent = active ? "已连接" : "连接中断";
    $("#footerState").textContent = active
      ? `SPI ${LINK_LABELS[linkState]} · 实时`
      : `SPI ${LINK_LABELS[linkState] || "等待"}`;
  }

  function renderLamp() {
    const lamp = state.lamp;
    const duties = lamp.duties || [];
    for (let i = 0; i < lampBars.length; i += 1) {
      const level = clamp(Number(duties[i] || 0) / 1000, 0, 1);
      lampBars[i].style.setProperty("--level", level.toFixed(3));
    }
    if (!lampDragging) {
      $("#brightness").value = String(lamp.brightness);
      $("#brightnessValue").textContent = `${lamp.brightness}%`;
    }
    const mode = lamp.mode;
    $("#effectSelect").value = EFFECT_LABELS[lamp.effect] ? lamp.effect : "breath";
    $("#effectLabel").textContent = mode === "kws" ? "关键词待机"
      : MODE_LABELS[mode] === "关闭" ? "关闭"
        : (EFFECT_LABELS[lamp.effect] || lamp.effect);
    const activeMask = Number(lamp.active_mask || 0);
    $("#lampState").textContent = mode === "kws"
      ? `待机呼吸 · ${lamp.brightness}%`
      : `${MODE_LABELS[mode] || mode} · 活动 0x${activeMask.toString(16)}`;
    $("#lampMeta").textContent = `PWM1–PWM13 · ${lamp.direct_ready ? "直控已启用" : "直控待升级"}`;
    $("#lampSeq").textContent = `event ${lamp.last_event_seq || 0}`;
  }

  function renderKws() {
    const kws = state.kws;
    const pill = $("#kwsPill");
    pill.className = `status-pill ${kws.state || "starting"}`;
    pill.textContent = KWS_LABELS[kws.state] || kws.state || "等待数据";
    if (kws.state === "unavailable") {
      $("#kwsKeyword").textContent = "KWS 未接入";
      $("#kwsDetail").textContent = "未检测到 KWS 日志，请确认 USB 麦克风和 KWS 进程。";
    } else if (kws.last_keyword) {
      $("#kwsKeyword").textContent = kws.last_keyword === "hello" ? "哈喽 · 已识别"
        : kws.last_keyword === "welcome" ? "威尔康 · 已识别"
          : `${kws.last_keyword} · 已识别`;
      const probability = kws.last_probability == null
        ? "" : ` · p=${Number(kws.last_probability).toFixed(3)}`;
      $("#kwsDetail").textContent = `最后一次触发${probability}`;
    } else {
      $("#kwsKeyword").textContent = state.lamp.mode === "kws" ? "等待关键词" : "关键词已暂停";
      $("#kwsDetail").textContent = state.lamp.mode === "kws"
        ? "请说「哈喽」或「威尔康」" : "网页灯效模式不会触发关键词";
    }
    $("#kwsCount").textContent = `触发 ${kws.trigger_count || 0} 次`;
    $("#kwsLast").textContent = kws.last_trigger_at
      ? `最近 ${formatTime(kws.last_trigger_at)}` : "—";
    const scores = kws.scores || {};
    const hello = clamp(Number(scores.hello || 0), 0, 1);
    const welcome = clamp(Number(scores.welcome || 0), 0, 1);
    $("#helloScore").style.width = `${hello * 100}%`;
    $("#welcomeScore").style.width = `${welcome * 100}%`;
    $("#helloScoreText").textContent = hello.toFixed(3);
    $("#welcomeScoreText").textContent = welcome.toFixed(3);
  }

  function renderThermal() {
    const t = state.thermal;
    const temp = t.temp_valid && t.temp_c != null ? Number(t.temp_c) : null;
    $("#tempNow").textContent = temp == null ? "—" : temp.toFixed(2);
    $("#targetNow").textContent = `${Number(t.setpoint_c).toFixed(1)} / ±${Number(t.margin_c).toFixed(1)}`;
    $("#dutyNow").textContent = String(Math.round(Number(t.duty) || 0));
    $("#ambientShown").textContent = `${Number(t.ambient_c).toFixed(1)} °C`;
    $("#anchorShown").textContent = `${Number(t.anchor_c).toFixed(1)} °C`;
    $("#setpointShown").textContent = `${Number(t.setpoint_c).toFixed(1)} °C`;
    $("#marginShown").textContent = `±${Number(t.margin_c).toFixed(1)} °C`;
    $("#pTerm").textContent = Number(t.p || 0).toFixed(1);
    $("#iTerm").textContent = Number(t.i || 0).toFixed(1);
    $("#dTerm").textContent = Number(t.d || 0).toFixed(1);
    const slope = t.slope_c_per_s;
    $("#slopeTerm").textContent = slope == null
      ? "—"
      : `${Number(slope) >= 0 ? "+" : ""}${Number(slope).toFixed(3)} °C/s`;
    $("#pidStatus").textContent = t.enabled
      ? (t.overtemp ? "过温保护" : "运行中") : "未使能";
    $("#pidStatus").className = `small-status ${t.enabled ? (t.overtemp ? "warn" : "on") : ""}`;
    $("#pidNote").textContent = t.no_response
      ? "加热已输出但结温无明显上升，请检查热耦合/加热电源。"
      : `PID 在 ${Number(t.ambient_c).toFixed(1)} °C 环境、锚定 ${Number(t.anchor_c).toFixed(1)} °C 下确定；应用参数后生效。`;
    $("#heatState").textContent = t.enabled
      ? (t.overtemp ? "过温保护 · HEAT=0" : "PID 运行中") : "加热未使能";
    $("#tempAge").textContent = t.temp_valid
      ? `温度 ${temp == null ? "—" : temp.toFixed(2)} °C · 采样 ${t.temp_age_ms == null ? "—" : Math.round(t.temp_age_ms)} ms`
      : "温度无效/过期";
    const enable = $("#heaterEnable");
    if (document.activeElement !== enable) enable.checked = Boolean(t.enabled);
    if (!pidDirty) {
      $("#setpointInput").value = t.setpoint_c;
      $("#marginInput").value = t.margin_c;
      $("#ambientInput").value = t.ambient_c;
      $("#anchorInput").value = t.anchor_c;
      $("#kpInput").value = t.kp;
      $("#kiInput").value = t.ki;
      $("#kdInput").value = t.kd;
      $("#maxInput").value = t.max_duty;
    }
  }

  function render() {
    setLinkUI(state.link.state);
    renderLamp();
    renderKws();
    renderThermal();
    drawLampChart();
    drawHeatChart();
    $("#updatedAt").innerHTML = `<i></i>${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  }

  function applyState(next) {
    if (!next || typeof next !== "object") return;
    if (next.link) state.link = next.link;
    if (next.lamp) state.lamp = { ...state.lamp, ...next.lamp };
    if (next.kws) state.kws = { ...state.kws, ...next.kws };
    if (next.thermal) state.thermal = { ...state.thermal, ...next.thermal };
    render();
  }

  function resizeCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, width: rect.width, height: rect.height };
  }

  function themeColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function drawLampChart() {
    const canvas = $("#lampCanvas");
    const size = resizeCanvas(canvas);
    if (!size) return;
    const { ctx, width, height } = size;
    const left = 31, right = 6, top = 7, bottom = 16;
    const rows = 13;
    const plotW = width - left - right;
    const plotH = height - top - bottom;
    ctx.clearRect(0, 0, width, height);
    const line = themeColor("--line");
    const muted = themeColor("--muted");
    const accent = themeColor("--accent");
    ctx.font = "9px 'Segoe UI', 'PingFang SC', sans-serif";
    ctx.textBaseline = "middle";
    for (let r = 0; r < rows; r += 1) {
      const y = top + r * plotH / rows;
      ctx.fillStyle = line;
      ctx.globalAlpha = 0.55;
      ctx.fillRect(left, y, plotW, 1);
      ctx.globalAlpha = 1;
      ctx.fillStyle = muted;
      ctx.fillText(`P${String(r + 1).padStart(2, "0")}`, 2, y + plotH / rows / 2);
    }
    const entries = history.lamp || [];
    if (entries.length < 2) {
      ctx.fillStyle = muted;
      ctx.textAlign = "center";
      ctx.fillText("等待灯牌波形数据…", left + plotW / 2, top + plotH / 2);
      ctx.textAlign = "start";
      return;
    }
    const lastT = entries[entries.length - 1].t;
    const firstT = lastT - 10;
    const visible = entries.filter((e) => e.t >= firstT);
    const startT = visible.length ? visible[0].t : firstT;
    const span = Math.max(0.1, lastT - startT);
    const rowH = plotH / rows;
    for (let n = 0; n < visible.length; n += 1) {
      const e = visible[n];
      const x0 = left + (e.t - startT) / span * plotW;
      const nextT = visible[n + 1] ? visible[n + 1].t : lastT;
      const x1 = left + (nextT - startT) / span * plotW;
      const w = Math.max(1, x1 - x0 + 0.6);
      for (let r = 0; r < rows; r += 1) {
        const level = clamp(Number(e.duties?.[r] || 0) / 1000, 0, 1);
        ctx.fillStyle = accent;
        ctx.globalAlpha = 0.08 + level * 0.82;
        ctx.fillRect(x0, top + r * rowH + 1, w, Math.max(1, rowH - 2));
      }
      if (e.gate) {
        ctx.globalAlpha = 0.7;
        ctx.fillStyle = themeColor("--green");
        ctx.fillRect(x0, top, 1, plotH);
      }
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = muted;
    ctx.fillText("−10s", left, height - 5);
    ctx.textAlign = "center";
    ctx.fillText("−5s", left + plotW / 2, height - 5);
    ctx.textAlign = "right";
    ctx.fillText("now", width - 2, height - 5);
    ctx.textAlign = "start";
  }

  function drawHeatChart() {
    const canvas = $("#heatCanvas");
    const size = resizeCanvas(canvas);
    if (!size) return;
    const { ctx, width, height } = size;
    const left = 35, right = 34, top = 8, bottom = 17;
    const plotW = width - left - right;
    const plotH = height - top - bottom;
    ctx.clearRect(0, 0, width, height);
    const line = themeColor("--line");
    const muted = themeColor("--muted");
    const accent = themeColor("--accent");
    const gold = themeColor("--gold");
    const green = themeColor("--green");
    const entries = (history.thermal || []).filter((e) => e.temp_c != null);
    const t = state.thermal;
    const upper = Number(t.setpoint_c) + Number(t.margin_c);
    const lower = Number(t.setpoint_c) - Number(t.margin_c);
    let minT = lower - 2;
    let maxT = upper + 2;
    for (const e of entries) {
      minT = Math.min(minT, e.temp_c);
      maxT = Math.max(maxT, e.temp_c);
    }
    if (maxT - minT < 8) maxT = minT + 8;
    const lastT = entries.length ? entries[entries.length - 1].t : 0;
    const startT = lastT - 60;
    const span = 60;
    const x = (time) => left + (time - startT) / span * plotW;
    const yT = (temp) => top + (maxT - temp) / (maxT - minT) * plotH;
    const yD = (duty) => top + (1 - duty / 1000) * plotH;

    ctx.font = "9px 'Segoe UI', 'PingFang SC', sans-serif";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i += 1) {
      const y = top + i * plotH / 4;
      ctx.fillStyle = line;
      ctx.globalAlpha = 0.6;
      ctx.fillRect(left, y, plotW, 1);
      ctx.globalAlpha = 1;
      ctx.fillStyle = muted;
      ctx.textAlign = "right";
      ctx.fillText(`${(maxT - i * (maxT - minT) / 4).toFixed(0)}°`, left - 4, y);
      ctx.textAlign = "left";
      ctx.fillText(`${100 - i * 25}%`, left + plotW + 4, y);
    }
    ctx.textAlign = "start";

    if (entries.length < 2) {
      ctx.fillStyle = muted;
      ctx.textAlign = "center";
      ctx.fillText("等待加热/温度数据…", left + plotW / 2, top + plotH / 2);
      ctx.textAlign = "start";
      return;
    }

    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.08;
    ctx.fillRect(left, yT(upper), plotW, Math.max(1, yT(lower) - yT(upper)));
    ctx.globalAlpha = 1;

    ctx.strokeStyle = green;
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(left, yT(upper));
    ctx.lineTo(left + plotW, yT(upper));
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(left, yT(lower));
    ctx.lineTo(left + plotW, yT(lower));
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.strokeStyle = gold;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    entries.forEach((e, i) => {
      const px = x(e.t);
      const py = yD(Number(e.duty || 0));
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();

    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    entries.forEach((e, i) => {
      const px = x(e.t);
      const py = yT(Number(e.temp_c));
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.stroke();

    ctx.fillStyle = muted;
    ctx.textAlign = "left";
    ctx.fillText("−60s", left, height - 5);
    ctx.textAlign = "center";
    ctx.fillText("−30s", left + plotW / 2, height - 5);
    ctx.textAlign = "right";
    ctx.fillText("now", width - 2, height - 5);
    ctx.textAlign = "start";
  }

  async function fetchState() {
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      applyState(await response.json());
    } catch (_) {
      setLinkUI(-1);
    }
  }

  async function fetchHistory() {
    try {
      const response = await fetch("/api/history", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      history = await response.json();
      drawLampChart();
      drawHeatChart();
    } catch (_) {
      /* state polling shows the connection error */
    }
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function applyEffect() {
    const effect = $("#effectSelect").value;
    const brightness = Number($("#brightness").value);
    postJson("/api/lamp", {
      command: "effect", effect, target: 0x1fff, brightness,
    }).then(applyState).catch((e) => toast(e.message, true));
  }

  function applyPid() {
    const payload = {
      setpoint_c: Number($("#setpointInput").value),
      margin_c: Number($("#marginInput").value),
      ambient_c: Number($("#ambientInput").value),
      anchor_c: Number($("#anchorInput").value),
      kp: Number($("#kpInput").value),
      ki: Number($("#kiInput").value),
      kd: Number($("#kdInput").value),
      max_duty: Number($("#maxInput").value),
      enabled: $("#heaterEnable").checked,
    };
    postJson("/api/thermal", payload)
      .then((data) => {
        pidDirty = false;
        applyState({ thermal: data });
        toast("PID 参数已应用");
      })
      .catch((e) => toast(e.message, true));
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("jlu-theme", theme);
    drawLampChart();
    drawHeatChart();
  }

  buildLampBars();
  const initialTheme = localStorage.getItem("jlu-theme")
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  setTheme(initialTheme);

  document.querySelectorAll(".mode[data-view]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  showView(currentView);

  $("#themeButton").addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  $("#refreshButton").addEventListener("click", () => {
    fetchState();
    fetchHistory();
    toast("已刷新");
  });

  $("#effectSelect").addEventListener("change", applyEffect);
  $("#kwsButton").addEventListener("click", () => {
    postJson("/api/lamp", { command: "kws" })
      .then(applyState)
      .then(() => toast("已恢复关键词待机"))
      .catch((e) => toast(e.message, true));
  });

  $("#brightness").addEventListener("pointerdown", () => { lampDragging = true; });
  $("#brightness").addEventListener("pointerup", () => { lampDragging = false; });
  $("#brightness").addEventListener("input", () => {
    lampDragging = true;
    $("#brightnessValue").textContent = `${$("#brightness").value}%`;
    clearTimeout(brightnessTimer);
    brightnessTimer = setTimeout(() => {
      lampDragging = false;
      applyEffect();
    }, 140);
  });

  $("#heaterEnable").addEventListener("change", () => {
    postJson("/api/thermal", { enabled: $("#heaterEnable").checked })
      .then((data) => {
        applyState({ thermal: data });
        toast($("#heaterEnable").checked ? "加热已使能" : "加热已关闭");
      })
      .catch((e) => {
        $("#heaterEnable").checked = !$("#heaterEnable").checked;
        toast(e.message, true);
      });
  });

  $("#pidForm").addEventListener("input", () => { pidDirty = true; });
  $("#applyPid").addEventListener("click", applyPid);

  window.addEventListener("resize", () => {
    drawLampChart();
    drawHeatChart();
  });

  fetchState().then(fetchHistory);
  stateTimer = setInterval(fetchState, 250);
  historyTimer = setInterval(fetchHistory, 1000);
})();
