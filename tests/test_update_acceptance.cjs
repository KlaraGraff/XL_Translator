// node tests/test_update_acceptance.cjs —— 验收清单第 8 节「更新检查、忽略更新」的
// 端到端规则（docs/refactor/2026-07-16_route_b_tauri_implementation.md:173）。
//
// 这一项之前只有「后端抑制通知」的单测和 Mock 合同测试，没有人把前端这条链路
// 真的走一遍：后台检查 → 读到忽略版本 → 侧栏红点不点亮 → 不弹提示。
// 这里用 vm 把 update-controller.ts 真实跑起来（和 test_update_activity.cjs 同一套
// 做法），断言的是「用户说了不想再看到 9.9.9，就别再因为这个版本打扰他」。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { stripTypeScriptTypes } = require("module");

function buildApi(apiOverrides = {}) {
  const sourcePath = path.join(__dirname, "..", "ui", "src", "update-controller.ts");
  let source = fs.readFileSync(sourcePath, "utf8");
  source = source.replace(/import[\s\S]*?from "[^"\n]+";/g, "").replace(/export /g, "");
  const code =
    `const {ApiClient}=require('./api-client');` +
    `const {openModal,showToast}=require('./components');` +
    `const {t,tx,localized}=require('./i18n');` +
    `const {setSettingsAlert}=require('./shell');` +
    `const {restartApp,resolveUpdate}=require('./update-service');` +
    stripTypeScriptTypes(source) +
    `;exports.availableVersion=availableVersion;` +
    `exports.ignoredVersion=ignoredVersion;` +
    `exports.notificationsPaused=notificationsPaused;` +
    `exports.runBackgroundUpdateCheck=runBackgroundUpdateCheck;` +
    `exports.runUpdateCheck=runUpdateCheck;` +
    `exports.ignoreVersion=ignoreVersion;` +
    `exports.updateSnapshot=updateSnapshot;`;
  const harness = { alerts: [], toasts: [], requests: [] };
  const sandbox = {
    exports: {},
    console,
    require(name) {
      if (name === "./api-client") {
        return {
          ApiClient: class {
            async connect() {}
            async request(path, options) {
              harness.requests.push({ path, options });
              const handler = apiOverrides.request;
              if (handler) return handler(path, options);
              throw new Error(`unexpected request: ${path}`);
            }
            async listTasks() {
              return { active: [], active_work_count: 0 };
            }
          },
        };
      }
      if (name === "./components") {
        return {
          openModal() {},
          showToast(options) {
            harness.toasts.push(options.message);
          },
        };
      }
      if (name === "./i18n") {
        // 译文层不是这条验收的重点，原样透出中文并做最简单的 {0} 替换
        // （英文/法文真值由 check_i18n.py 保证）。
        const fill = (text, args) =>
          String(text).replace(/\{(\d+)\}/g, (whole, index) =>
            args[Number(index)] === undefined ? whole : String(args[Number(index)]),
          );
        return {
          t: (text, ...args) => fill(text, args),
          tx: (text) => fill(text, []),
          localized: (build) => build(),
        };
      }
      if (name === "./shell") {
        return {
          setSettingsAlert(value) {
            harness.alerts.push(value);
          },
        };
      }
      return { restartApp: async () => {}, resolveUpdate: async () => null };
    },
  };
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.exports, harness };
}

(async () => {
  // 1. 忽略的版本不再提示，也不点亮侧栏红点。
  {
    const { api, harness } = buildApi({
      request: (path) => {
        if (path === "/api/updates/state") {
          return { preferences: { notifications_paused: false, ignored_release_version: "9.9.9" } };
        }
        return { status: "available", latest_version: "9.9.9" };
      },
    });
    await api.runBackgroundUpdateCheck();
    assert.equal(api.ignoredVersion(), "9.9.9", "忽略版本从 /api/updates/state 读出来");
    assert.equal(api.availableVersion(), "", "被忽略的版本不算「有新版」");
    assert.deepEqual(harness.alerts, [false], "被忽略的版本不点亮侧栏红点");
    assert.deepEqual(harness.toasts, [], "后台检查本来就不弹 toast");
  }

  // 2. 忽略的是旧版本时，更新的版本照样提示 —— 这正是「忽略」而不是「关掉更新」。
  {
    const { api, harness } = buildApi({
      request: (path) => {
        if (path === "/api/updates/state") {
          return { preferences: { notifications_paused: false, ignored_release_version: "9.9.9" } };
        }
        return { status: "available", latest_version: "10.0.0" };
      },
    });
    await api.runBackgroundUpdateCheck();
    assert.equal(api.availableVersion(), "10.0.0", "更高版本仍然算「有新版」");
    assert.deepEqual(harness.alerts, [true], "更高版本要点亮侧栏红点");
  }

  // 3. 手动检查不受「暂停后台提醒」影响：用户自己问了就一定去问一次。
  {
    const { api, harness } = buildApi({
      request: (path) => {
        if (path === "/api/updates/state") {
          return { preferences: { notifications_paused: true, ignored_release_version: "" } };
        }
        return { status: "up_to_date", latest_version: "" };
      },
    });
    assert.equal(api.notificationsPaused(), false, "还没读过偏好时按「没暂停」处理");
    await api.runUpdateCheck();
    const check = harness.requests.find((entry) => entry.path.includes("/api/updates/check"));
    assert.ok(check, "手动检查必须真的发一次请求");
    assert.match(check.path, /mode=manual/, "手动检查走 mode=manual，后端不拦截");
    // 手动检查不看偏好：暂停提醒只拦后台那一次，用户自己点的不受影响。
    assert.ok(
      !harness.requests.some((entry) => entry.path === "/api/updates/state"),
      "手动检查不依赖「暂停提醒」偏好",
    );
    assert.equal(api.updateSnapshot().lastCheckOk, true, "查成了要记下来");
    assert.deepEqual(harness.toasts, [], "检查成功且没有新版时不打扰用户");
  }

  // 4. 「忽略这个版本」是从提示卡片上点出来的动作，写进偏好后立刻生效。
  {
    let stored = { preferences: { notifications_paused: false, ignored_release_version: "" } };
    const { api, harness } = buildApi({
      request: (path, options) => {
        if (path === "/api/updates/state") return stored;
        if (path === "/api/updates/preferences") {
          const body = JSON.parse(options.body);
          stored = { preferences: { ...stored.preferences, ...body } };
          return stored;
        }
        return { status: "available", latest_version: "9.9.9" };
      },
    });
    await api.runBackgroundUpdateCheck();
    assert.equal(api.availableVersion(), "9.9.9", "先能看到这个版本");
    await api.ignoreVersion("9.9.9");
    const put = harness.requests.find((entry) => entry.path === "/api/updates/preferences");
    assert.equal(put.options.method, "PUT", "写偏好用 PUT");
    assert.deepEqual(
      JSON.parse(put.options.body),
      { ignored_release_version: "9.9.9" },
      "只写忽略版本，不动其它偏好",
    );
    assert.equal(api.ignoredVersion(), "9.9.9", "点完立刻生效");
    assert.equal(api.availableVersion(), "", "点完之后这个版本不再提示");
    assert.equal(harness.alerts.at(-1), false, "侧栏红点跟着熄灭");
    assert.match(harness.toasts.at(-1), /9\.9\.9/, "告诉用户忽略了哪个版本");
  }

  // 5. 后台检查撞上尚未完成的快速开始（deferred）时不改动界面状态。
  {
    const { api, harness } = buildApi({
      request: (path) => {
        if (path === "/api/updates/state") {
          return { preferences: { notifications_paused: false, ignored_release_version: "" } };
        }
        return { status: "deferred", reason: "quick_start_incomplete" };
      },
    });
    await api.runBackgroundUpdateCheck();
    assert.equal(api.availableVersion(), "", "deferred 不算检查结论");
    assert.deepEqual(harness.alerts, [], "deferred 不点亮也不熄灭");
    assert.equal(api.updateSnapshot().result, null, "deferred 不覆盖已有结果");
  }

  // 6. 后台检查本身失败时静默：下次启动再说，不在启动那一刻弹错误。
  {
    const { api, harness } = buildApi({
      request: (path) => {
        if (path === "/api/updates/state") {
          return { preferences: { notifications_paused: false, ignored_release_version: "" } };
        }
        throw new Error("sidecar 未就绪");
      },
    });
    await api.runBackgroundUpdateCheck();
    assert.deepEqual(harness.toasts, [], "后台检查失败不弹窗");
    assert.equal(api.updateSnapshot().lastCheckOk, null, "后台检查不写手动检查的结论");
  }

  console.log("update acceptance: ignore version, later version, manual check, deferred and silent failure passed");
})();
