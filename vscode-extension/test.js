// Headless test for the adhd.focus extension. Stubs `vscode` and points HOME at
// a temp sandbox, then drives the focus-request file watcher and the legacy URI
// handler and asserts the right terminal tab is revealed.
// Run: node vscode-extension/test.js
const Module = require("module");
const fs = require("fs");
const os = require("os");
const path = require("path");

// Sandbox HOME so REQUEST/LOG land in a temp dir, not the real ~/.adhd.
const home = fs.mkdtempSync(path.join(os.tmpdir(), "adhd-ext-"));
process.env.USERPROFILE = home;
process.env.HOME = home;
fs.mkdirSync(path.join(home, ".adhd"), { recursive: true });
const REQUEST = path.join(home, ".adhd", "focus-request.json");

// Minimal `vscode` stub with a controllable terminal list.
let shown = [];
function term(name, pid) {
  return {
    name,
    processId: Promise.resolve(pid),
    show: (preserveFocus) => shown.push({ name, preserveFocus }),
  };
}
let uriHandler = null;
const vscode = {
  window: {
    terminals: [],
    registerUriHandler: (h) => {
      uriHandler = h;
      return { dispose() {} };
    },
  },
};
const origLoad = Module._load;
Module._load = function (request) {
  if (request === "vscode") return vscode;
  return origLoad.apply(this, arguments);
};

const ext = require(path.join(__dirname, "extension.js"));

let failures = 0;
function assert(cond, msg) {
  console.log((cond ? "ok:  " : "FAIL: ") + msg);
  if (!cond) failures++;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function writeReq(shellPid, ts) {
  const tmp = REQUEST + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify({ shellPid, ts }));
  fs.renameSync(tmp, REQUEST); // atomic, like the tray
}

(async () => {
  vscode.window.terminals = [term("A", 111), term("B", 222), term("C", 333)];
  const ctx = { subscriptions: [] };
  ext.activate(ctx);

  // 1) matching request reveals only the owner tab, taking focus
  shown = [];
  writeReq(222, 1000);
  await sleep(500);
  assert(
    shown.length === 1 && shown[0].name === "B" && shown[0].preserveFocus === false,
    "broadcast reveals the terminal whose shell pid matches (and focuses it)"
  );

  // 2) a request whose ts does not advance is ignored (no replay)
  shown = [];
  writeReq(111, 1000);
  await sleep(400);
  assert(shown.length === 0, "request with non-increasing ts is ignored");

  // 3) newer ts but a pid this window doesn't own -> reveal nothing
  shown = [];
  writeReq(999, 1001);
  await sleep(400);
  assert(shown.length === 0, "request for an unowned pid reveals nothing (other window owns it)");

  // 4) legacy URI handler still resolves a tab
  shown = [];
  await uriHandler.handleUri({ query: "shellPid=333" });
  await sleep(20);
  assert(shown.length === 1 && shown[0].name === "C", "legacy vscode:// URI handler reveals the matching tab");

  ctx.subscriptions.forEach((d) => d.dispose && d.dispose());
  try { fs.rmSync(home, { recursive: true, force: true }); } catch (e) {}
  console.log(failures ? `\n${failures} FAILURE(S)` : "\nALL PASS");
  process.exit(failures ? 1 : 0);
})();
