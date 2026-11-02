// Capture a real short screen recording of the running canvas.
// Usage:
//   node capture_ui.cjs --url ... --frames-dir ... --main ... --duration 4 --fps 8
// Frames are JPEGs; the Python wrapper encodes them with ffmpeg.
const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

const CHROME = process.env.CANVAS_CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function arg(name, fallback) {
  const index = process.argv.indexOf("--" + name);
  return index === -1 ? fallback : process.argv[index + 1];
}

async function main() {
  const url = arg("url", "http://127.0.0.1:8792/");
  const framesDir = arg("frames-dir", path.join("scratch", "capture-frames"));
  const mainPath = arg("main", path.join("scratch", "canvas-main.png"));
  const duration = Number(arg("duration", "4"));
  const fps = Number(arg("fps", "8"));
  const actions = arg("actions", "");
  fs.mkdirSync(framesDir, { recursive: true });
  fs.mkdirSync(path.dirname(mainPath), { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars",
           "--no-first-run", "--disable-extensions", "--window-size=1600,1000"],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await new Promise((r) => setTimeout(r, 1200));
    // Visible synthetic cursor + click ripple: headless screenshots normally omit
    // the OS cursor, which makes real interactions look like static slides.
    await page.evaluate(() => {
      if (document.getElementById("__wc_cursor")) return;
      const dot = document.createElement("div");
      dot.id = "__wc_cursor";
      dot.style.cssText = "position:fixed;left:0;top:0;width:18px;height:18px;border-radius:50%;" +
        "background:rgba(240,180,41,.9);border:2px solid #fff;z-index:2147483647;pointer-events:none;" +
        "box-shadow:0 0 0 4px rgba(240,180,41,.25);transform:translate(-50%,-50%)";
      document.documentElement.appendChild(dot);
      window.addEventListener("mousemove", (e) => { dot.style.left = e.clientX + "px"; dot.style.top = e.clientY + "px"; });
    });
    async function clickText(text) {
      return page.evaluate((label) => {
        const candidates = Array.from(document.querySelectorAll("button,a,[role=button],div"));
        const target = candidates.find((el) => (el.innerText || "").trim() === label || (el.innerText || "").trim().startsWith(label));
        if (!target) return false;
        const rect = target.getBoundingClientRect();
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;
        const ripple = document.createElement("div");
        ripple.style.cssText = "position:fixed;left:" + x + "px;top:" + y + "px;width:10px;height:10px;border-radius:50%;" +
          "border:3px solid #fff;z-index:2147483646;pointer-events:none;transform:translate(-50%,-50%);animation:wcRipple .5s ease-out";
        document.documentElement.appendChild(ripple);
        setTimeout(() => ripple.remove(), 520);
        target.click();
        return { x, y };
      }, text);
    }
    await page.screenshot({ path: mainPath, type: "png" });
    const total = Math.max(8, Math.round(duration * fps));
    let previewActions = [];
    if (actions === "preview") {
      previewActions = [
        [0.05, "工作流"], [0.35, "关闭"], [0.48, "知识库"], [0.75, "关闭"], [0.84, "+ 新建模块"]
      ];
    }
    const totalMs = Math.max(1000, Math.round(1000 * duration));
    for (let i = 0; i < total; i++) {
      const fraction = i / total;
      for (const [at, label] of previewActions) {
        if (fraction >= at && fraction < at + 1 / total) {
          const point = await clickText(label).catch(() => null);
          if (point) await page.mouse.move(point.x, point.y, { steps: 3 }).catch(() => {});
          await new Promise((r) => setTimeout(r, 260));
        }
      }
      const x = 240 + (i % 12) * 92;
      const y = 280 + Math.floor(i / 12) * 48;
      await page.mouse.move(x, y, { steps: 2 }).catch(() => {});
      const file = path.join(framesDir, "frame-" + String(i).padStart(4, "0") + ".jpg");
      await page.screenshot({ path: file, type: "jpeg", quality: 84 });
      await new Promise((r) => setTimeout(r, Math.max(40, Math.round(totalMs / total) - 50)));
    }
    console.log(JSON.stringify({ ok: true, frames: total, fps, duration }));
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
