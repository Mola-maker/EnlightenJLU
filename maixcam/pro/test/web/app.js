(() => {
  "use strict";

  const LAMP_COUNT = 13;
  const ALL_LAMPS = 0x1fff;
  const EFFECT_LABELS = {
    solid: "常亮",
    breath: "呼吸",
    wave: "波浪",
    chase: "追逐",
    sparkle: "星光",
    theater: "交替",
    comet: "流星",
    pulse: "心跳",
    manual: "单灯",
  };
  const HERO = {
    kws: ["等待一句话。", "PWM1–PWM13 正在低声呼吸，关键词会点亮它们。"],
    solid: ["十三盏，一起亮起。", "稳定输出，亮度由你决定。"],
    breath: ["呼吸，正在发生。", "光线缓慢起伏，像一次安静的呼吸。"],
    wave: ["光，像水一样流动。", "每一路灯依次起伏，形成柔和的波浪。"],
    chase: ["一束光在奔跑。", "高光沿着十三个灯位持续游走。"],
    sparkle: ["细碎星光。", "灯光以不同节奏柔和闪烁。"],
    theater: ["一半亮，一半暗。", "奇偶灯位交替呼吸，像舞台的明暗换场。"],
    comet: ["一颗流星经过。", "高亮灯位拖着柔和尾焰，沿着十三路滑过。"],
    pulse: ["两次心跳。", "短促的双拍脉冲，随后回到安静的暗光。"],
    manual: ["每一盏，独立调光。", "拖动单灯滑杆，使用 SET_LAMPS 直接校准十三路亮度。"],
    off: ["光，暂时休息。", "所有灯位已关闭，HEAT 始终锁定关闭。"],
  };
  const LINK_LABELS = {
    0: "BOOT",
    1: "WAIT_HELLO",
    2: "ACTIVE",
    3: "LOST",
    4: "FAULT",
  };
  const FAULT_NAMES = [
    "CRC", "FORMAT", "SPI", "UNKNOWN_TLV", "NEED_HELLO",
    "TEMP_STALE", "TEMP_INVALID", "HEATER_UNSUPPORTED",
    "BAD_MASK", "PROTOCOL", "LINK_LOST", "OVERTEMP", "THERMAL_CONFIG",
  ];
  const SCENES = {
    full: { effect: "solid", brightness: 100, target: ALL_LAMPS, speed: 1.0 },
    soft: { effect: "solid", brightness: 35, target: ALL_LAMPS, speed: 1.0 },
    breath: { effect: "breath", brightness: 75, target: ALL_LAMPS, speed: 1.0 },
    comet: { effect: "comet", brightness: 85, target: ALL_LAMPS, speed: 1.2 },
    long: { effect: "solid", brightness: 100, target: 1, speed: 1.0 },
    spring: { effect: "solid", brightness: 100, target: 2, speed: 1.0 },
  };

  const $ = (selector) => document.querySelector(selector);
  const lampRow = $("#lampStage");
  const channelGrid = $("#channelGrid");
  const manualGrid = $("#manualGrid");
  const slider = $("#brightness");
  const speedSlider = $("#speed");
  const state = {
    mode: "kws",
    effect: "breath",
    target: ALL_LAMPS,
    brightness: 75,
    speed: 1.0,
    duties: Array(LAMP_COUNT).fill(0),
    direct_ready: false,
    link_state: -1,
    active_mask: 0,
    glyph_state: [],
    fault_flags: 0,
    last_control_seq: 0,
    last_event_seq: 0,
    temp_mc: 0,
    temp_valid: false,
    message: "",
  };
  let targetLevels = Array(LAMP_COUNT).fill(0);
  let displayLevels = Array(LAMP_COUNT).fill(0);
  let lampEls = [];
  let channelEls = [];
  let manualEls = [];
  let effectTimer = null;
  let manualTimer = null;
  let dragging = false;
  let speedDragging = false;
  let manualDragging = false;
  let toastTimer = null;
  let eventSource = null;

  function buildLamps() {
    for (let i = 0; i < LAMP_COUNT; i += 1) {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "lamp-cell";
      cell.dataset.index = String(i);
      cell.setAttribute("aria-label", `灯位 ${i + 1}`);
      cell.innerHTML = `<span class="lamp-core"></span><span class="lamp-label">${String(i + 1).padStart(2, "0")}</span>`;
      cell.addEventListener("click", () => toggleChannel(i));
      lampRow.appendChild(cell);
      lampEls.push(cell);

      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "channel-chip";
      chip.dataset.index = String(i);
      chip.innerHTML = `PWM ${String(i + 1).padStart(2, "0")}`;
      chip.addEventListener("click", () => toggleChannel(i));
      channelGrid.appendChild(chip);
      channelEls.push(chip);
    }
  }

  function buildManualControls() {
    for (let i = 0; i < LAMP_COUNT; i += 1) {
      const row = document.createElement("div");
      row.className = "manual-channel";

      const label = document.createElement("label");
      label.htmlFor = `manual-${i}`;
      label.textContent = `PWM ${String(i + 1).padStart(2, "0")}`;

      const input = document.createElement("input");
      input.id = `manual-${i}`;
      input.type = "range";
      input.min = "0";
      input.max = "100";
      input.step = "1";
      input.value = "0";
      input.setAttribute("aria-label", `PWM ${i + 1} 亮度`);

      const output = document.createElement("output");
      output.htmlFor = input.id;
      output.textContent = "0%";

      input.addEventListener("pointerdown", () => {
        manualDragging = true;
      });
      input.addEventListener("input", () => {
        manualDragging = true;
        input.style.setProperty("--v", input.value);
        output.textContent = `${input.value}%`;
        clearTimeout(manualTimer);
        manualTimer = setTimeout(() => {
          manualDragging = false;
          sendManual();
        }, 140);
      });
      input.addEventListener("change", () => {
        manualDragging = false;
        clearTimeout(manualTimer);
        sendManual();
      });

      row.append(label, input, output);
      manualGrid.appendChild(row);
      manualEls.push({ input, output });
    }
  }

  function setManualUIFromDuties(duties) {
    if (manualDragging) return;
    for (let i = 0; i < LAMP_COUNT; i += 1) {
      const duty = Math.max(0, Math.min(1000, Number(duties?.[i] || 0)));
      const percent = Math.round(duty / 10);
      const item = manualEls[i];
      if (!item) continue;
      item.input.value = String(percent);
      item.input.style.setProperty("--v", String(percent));
      item.output.textContent = `${percent}%`;
    }
  }

  function manualDuties() {
    return manualEls.map(({ input }) => Number(input.value) * 10);
  }

  function sendManual() {
    manualDragging = false;
    const duties = manualDuties();
    return command({ command: "manual", duties, speed: state.speed })
      .then(() => {
        $("#manualHint").textContent = "已下发 SET_LAMPS";
      })
      .catch(() => {});
  }

  function targetIsAll() { return state.target === ALL_LAMPS; }
  function selectedCount() {
    let count = 0;
    for (let i = 0; i < LAMP_COUNT; i += 1) {
      if (state.target & (1 << i)) count += 1;
    }
    return count;
  }

  function updateTargetUI() {
    lampEls.forEach((el, i) => {
      el.classList.toggle("is-selected", Boolean(state.target & (1 << i)));
    });
    channelEls.forEach((el, i) => {
      el.classList.toggle("is-on", Boolean(state.target & (1 << i)));
    });
    document.querySelectorAll("#targetPresets .seg").forEach((button) => {
      const raw = button.dataset.target;
      let active = false;
      if (raw === "8191") active = state.target === ALL_LAMPS;
      else if (raw === "custom") {
        active = !targetIsAll() && state.target !== 1 && state.target !== 2;
      } else active = state.target === Number(raw);
      button.classList.toggle("is-active", active);
    });
    const count = selectedCount();
    $("#targetHint").textContent = targetIsAll()
      ? "全部 13 路"
      : count === 1 && (state.target & 1) ? "长 / PWM1"
      : count === 1 && (state.target & 2) ? "春 / PWM2"
      : `自定义 ${count} 路`;
    $("#selectAll").textContent = targetIsAll() ? "清空" : "全选";
  }

  function updateSpeedUI() {
    const speed = Number(state.speed) || 1;
    if (!speedDragging) {
      speedSlider.value = String(Math.round(speed * 100));
    }
    $("#speedValue").textContent = `${speed.toFixed(1)}×`;
    const percent = Math.max(0, Math.min(100, ((speed * 100) - 25) / 275 * 100));
    speedSlider.style.setProperty("--pct", String(percent));
  }

  function updateModeUI() {
    const mode = state.mode;
    const effect = state.effect;
    const isManual = effect === "manual";
    document.querySelectorAll(".effect-card").forEach((card) => {
      card.classList.toggle(
        "is-active", mode === "direct" && !isManual && card.dataset.effect === effect,
      );
    });
    document.querySelectorAll(".scene-btn").forEach((card) => {
      const scene = SCENES[card.dataset.scene];
      const active = Boolean(scene) && mode === "direct" && !isManual &&
        effect === scene.effect &&
        state.brightness === scene.brightness &&
        state.target === scene.target &&
        Math.abs(Number(state.speed) - scene.speed) < 0.01;
      card.classList.toggle("is-active", active);
    });
    const modeLabel = mode === "kws" ? "KWS 待机"
      : mode === "direct" ? `网页 · ${EFFECT_LABELS[effect] || effect}`
      : mode === "firmware" ? "固件特效"
      : "关闭";
    $("#modePill").textContent = modeLabel;
    const hero = mode === "direct" ? (HERO[effect] || HERO.solid)
      : mode === "off" ? HERO.off
      : mode === "firmware" ? ["固件曲线运行中。", "这是 STM32 内置的固定表演曲线。"]
      : HERO.kws;
    $("#heroTitle").textContent = hero[0];
    $("#heroSub").textContent = hero[1];
  }

  function updateHealth() {
    const active = state.link_state === 2;
    const connection = $("#connection");
    connection.classList.toggle("online", active);
    connection.classList.toggle("offline", !active);
    $("#connectionText").textContent = active ? "SPI 在线" : "SPI 未连接";
    $("#linkText").textContent = LINK_LABELS[state.link_state] || "等待链路";
    $("#linkValue").textContent = LINK_LABELS[state.link_state] || "—";
    $("#maskValue").textContent = `0x${(state.active_mask >>> 0).toString(16).toUpperCase().padStart(2, "0")}`;
    $("#eventValue").textContent = String(state.last_event_seq);
    $("#seqValue").textContent = String(state.last_control_seq);
    const temp = state.temp_valid
      ? `${(Number(state.temp_mc) / 1000).toFixed(2)} °C`
      : "未读数";
    $("#tempValue").textContent = temp;
    $("#tempReadout").textContent = `结温 ${temp}`;
    $("#directValue").textContent = state.direct_ready ? "已支持" : "待升级";
    const faults = FAULT_NAMES.filter((_, i) => state.fault_flags & (1 << i));
    $("#faultValue").textContent = faults.length ? faults.join(" · ") : "正常";
    $("#faultValue").style.color = faults.length ? "#ffb07a" : "#9fe8d9";
    $("#directNote").classList.toggle("hidden", state.direct_ready);
  }

  function applyState(next) {
    Object.assign(state, next || {});
    if (!dragging) {
      slider.value = String(state.brightness);
      $("#brightnessValue").textContent = `${state.brightness}%`;
      slider.style.setProperty("--pct", state.brightness);
    }
    updateSpeedUI();
    $("#brightnessReadout").textContent = `${state.brightness}% 亮度`;
    updateTargetUI();
    updateModeUI();
    updateHealth();
    targetLevels = Array.from({ length: LAMP_COUNT }, (_, i) => {
      const value = Number(state.duties?.[i] || 0);
      return Math.max(0, Math.min(1000, value));
    });
    setManualUIFromDuties(state.duties);
  }

  function animate() {
    for (let i = 0; i < LAMP_COUNT; i += 1) {
      const target = targetLevels[i] / 1000;
      displayLevels[i] += (target - displayLevels[i]) * 0.24;
      if (Math.abs(target - displayLevels[i]) < 0.003) displayLevels[i] = target;
      lampEls[i].style.setProperty("--level", displayLevels[i].toFixed(3));
    }
    requestAnimationFrame(animate);
  }

  function toast(text, isError = false) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function command(payload) {
    return fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      applyState(data);
      return data;
    }).catch((error) => {
      toast(error.message || "控制失败", true);
      throw error;
    });
  }

  function scheduleEffect() {
    clearTimeout(effectTimer);
    effectTimer = setTimeout(() => {
      if (state.effect === "manual") {
        sendManual();
        return;
      }
      command({
        command: "effect",
        effect: state.effect,
        target: state.target,
        brightness: state.brightness,
        speed: state.speed,
      }).then(() => {
        if (!state.direct_ready) toast("预览已更新；烧录 web 固件后控制真实亮度");
      }).catch(() => {});
    }, 120);
  }

  function leaveManual() {
    if (state.effect === "manual") {
      state.effect = "solid";
      updateModeUI();
    }
  }

  function toggleChannel(index) {
    leaveManual();
    state.target ^= (1 << index);
    if (!state.target) {
      state.target = ALL_LAMPS;
      toast("至少保留一路，已恢复全灯");
    }
    updateTargetUI();
    scheduleEffect();
  }

  function applyScene(name) {
    const scene = SCENES[name];
    if (!scene) return;
    state.effect = scene.effect;
    state.brightness = scene.brightness;
    state.target = scene.target;
    state.speed = scene.speed;
    if (!dragging) {
      slider.value = String(state.brightness);
      $("#brightnessValue").textContent = `${state.brightness}%`;
      slider.style.setProperty("--pct", state.brightness);
    }
    updateTargetUI();
    updateModeUI();
    updateSpeedUI();
    scheduleEffect();
    toast(`场景：${$("#scenePresets [data-scene='" + name + "']")?.textContent || name}`);
  }

  buildLamps();
  buildManualControls();
  animate();

  document.querySelectorAll("#effectGrid .effect-card").forEach((button) => {
    button.addEventListener("click", () => {
      state.effect = button.dataset.effect;
      updateModeUI();
      scheduleEffect();
    });
  });

  document.querySelectorAll("#targetPresets .seg").forEach((button) => {
    button.addEventListener("click", () => {
      leaveManual();
      if (button.dataset.target === "custom") {
        if (targetIsAll()) state.target = 0x0003;
      } else {
        state.target = Number(button.dataset.target);
      }
      updateTargetUI();
      scheduleEffect();
    });
  });

  $("#selectAll").addEventListener("click", () => {
    leaveManual();
    state.target = targetIsAll() ? 0x0001 : ALL_LAMPS;
    updateTargetUI();
    scheduleEffect();
  });

  slider.addEventListener("pointerdown", () => { dragging = true; });
  slider.addEventListener("pointerup", () => { dragging = false; });
  slider.addEventListener("pointercancel", () => { dragging = false; });
  slider.addEventListener("input", () => {
    leaveManual();
    state.brightness = Number(slider.value);
    $("#brightnessValue").textContent = `${state.brightness}%`;
    $("#brightnessReadout").textContent = `${state.brightness}% 亮度`;
    slider.style.setProperty("--pct", state.brightness);
    targetLevels = targetLevels.map((_, i) => (
      state.target & (1 << i) ? state.brightness * 10 : 0
    ));
    scheduleEffect();
  });
  slider.addEventListener("change", () => { dragging = false; });

  speedSlider.addEventListener("pointerdown", () => { speedDragging = true; });
  speedSlider.addEventListener("pointerup", () => { speedDragging = false; });
  speedSlider.addEventListener("pointercancel", () => { speedDragging = false; });
  speedSlider.addEventListener("input", () => {
    state.speed = Number(speedSlider.value) / 100;
    updateSpeedUI();
    scheduleEffect();
  });
  speedSlider.addEventListener("change", () => { speedDragging = false; });

  document.querySelectorAll("#scenePresets .scene-btn").forEach((button) => {
    button.addEventListener("click", () => applyScene(button.dataset.scene));
  });

  $("#manualApply").addEventListener("click", () => {
    sendManual().then(() => toast("单灯亮度已下发"));
  });

  $("#kwsButton").addEventListener("click", () => {
    command({ command: "kws" }).then(() => toast("已恢复关键词待机"));
  });
  $("#offButton").addEventListener("click", () => {
    command({ command: "off" }).then(() => toast("全部输出已关闭"));
  });

  document.querySelectorAll("[data-firmware]").forEach((button) => {
    button.addEventListener("click", () => {
      command({ command: "firmware", action: button.dataset.firmware })
        .then(() => toast(`固件特效：${button.textContent.trim()}`));
    });
  });

  function connect() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource("/api/stream");
    eventSource.onopen = () => {
      $("#connection").classList.remove("offline");
      $("#connectionText").textContent = "已连接";
    };
    eventSource.onmessage = (event) => {
      try { applyState(JSON.parse(event.data)); }
      catch (_) { /* ignore a partial frame */ }
    };
    eventSource.onerror = () => {
      $("#connection").classList.add("offline");
      $("#connectionText").textContent = "重连中";
    };
  }

  fetch("/api/state")
    .then((response) => response.json())
    .then(applyState)
    .catch(() => toast("无法读取服务器状态", true));
  connect();
})();
