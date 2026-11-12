/*
 * Screenshots and pixel diffs for the club-site template packages.
 *
 * Driven by parity.py, which renders each package with its sample.json and
 * hands this script a JSON job file:
 *
 *   {"jobs": [{"key": "classic", "original": "/abs/01-classic.html",
 *              "rendered": "/abs/out/classic.html", "out": "/abs/site-parity/classic",
 *              "package": "/abs/backend/app/api/sites/templates/classic",
 *              "thumbs": true}],
 *    "result": "/abs/out/result.json"}
 *
 * For every job: the designer's page and ours at 1440 px, full page, compared
 * pixel by pixel (exact RGBA equality; pixelmatch draws the diff image); our
 * page at 390 and 768 px with a horizontal-overflow check (scrollWidth and
 * every element poking past the viewport); and the two dashboard thumbnails.
 *
 * Needs playwright-core, pngjs and pixelmatch from the tools folder
 * (SITE_PARITY_TOOLS, default ~/development/projects/feature-register/site-parity-tools)
 * and a Chromium: SITE_PARITY_BROWSER, else Brave, else Playwright's
 * cached chrome-headless-shell. One browser per run, closed at the end.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const TOOLS = process.env.SITE_PARITY_TOOLS || path.join(os.homedir(), "development/projects/feature-register/site-parity-tools");
const need = (m) => require(path.join(TOOLS, "node_modules", m));
const { chromium } = need("playwright-core");
const { PNG } = need("pngjs");
const pixelmatchMod = need("pixelmatch");
const pixelmatch = pixelmatchMod.default || pixelmatchMod;

function browserPath() {
  if (process.env.SITE_PARITY_BROWSER) return process.env.SITE_PARITY_BROWSER;
  const brave = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser";
  if (fs.existsSync(brave)) return brave;
  const cache = path.join(os.homedir(), "Library/Caches/ms-playwright");
  if (fs.existsSync(cache)) {
    for (const d of fs.readdirSync(cache).filter((d) => d.startsWith("chromium_headless_shell")).sort().reverse()) {
      for (const sub of ["chrome-mac/headless_shell", "chrome-headless-shell-mac-arm64/chrome-headless-shell", "chrome-headless-shell-mac-x64/chrome-headless-shell"]) {
        const p = path.join(cache, d, sub);
        if (fs.existsSync(p)) return p;
      }
    }
  }
  return undefined; // playwright's own default
}

async function settle(page) {
  // Fonts come from Google Fonts on both pages. Lazy pictures below the fold
  // would stay blank in a full-page shot, and a picture that has loaded may
  // not be decoded yet: load everything eagerly, then wait for the fonts and
  // for every <img> to decode (each capped, so a broken picture can't hang).
  await page.evaluate(() => document.querySelectorAll('img[loading="lazy"]').forEach((i) => i.setAttribute("loading", "eager")));
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.evaluate(async () => {
    await document.fonts.ready;
    const capped = (p) => Promise.race([p.catch(() => {}), new Promise((r) => setTimeout(r, 10000))]);
    await Promise.all(Array.from(document.images).map((i) => capped(i.decode())));
    await document.fonts.ready;
  });
  await page.waitForTimeout(150);
}

async function shot(context, file, opts = {}) {
  const page = await context.newPage();
  await page.goto("file://" + file, { waitUntil: "load" });
  await settle(page);
  const metrics = opts.metrics ? await page.evaluate(overflowReport) : null;
  const buf = await page.screenshot({ fullPage: opts.fullPage !== false, animations: "disabled", caret: "hide" });
  await page.close();
  return { buf, metrics };
}

// Runs in the page: anything wider than the viewport, and why.
function overflowReport() {
  const vw = document.documentElement.clientWidth;
  const out = { viewport: vw, scrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth, offenders: [] };
  const clipped = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (/(hidden|clip|auto|scroll)/.test(s.overflowX)) {
        const r = p.getBoundingClientRect();
        if (r.right <= vw + 0.5 && r.left >= -0.5) return true;
      }
    }
    return false;
  };
  for (const el of document.body.querySelectorAll("*")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if ((r.right > vw + 0.5 || r.left < -0.5) && !clipped(el)) {
      const desc = el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : "");
      out.offenders.push({ el: desc, left: Math.round(r.left), right: Math.round(r.right), text: (el.textContent || "").trim().slice(0, 60) });
    }
  }
  // Only the outermost offenders are interesting.
  out.offenders = out.offenders.slice(0, 25);
  return out;
}

function diff(aBuf, bBuf, diffFile) {
  const a = PNG.sync.read(aBuf);
  const b = PNG.sync.read(bBuf);
  const w = Math.max(a.width, b.width);
  const h = Math.max(a.height, b.height);
  const pad = (img) => {
    if (img.width === w && img.height === h) return img;
    const out = new PNG({ width: w, height: h });
    out.data.fill(0);
    PNG.bitblt(img, out, 0, 0, img.width, img.height, 0, 0);
    return out;
  };
  const A = pad(a);
  const B = pad(b);
  let same = 0;
  for (let i = 0; i < A.data.length; i += 4) {
    if (A.data[i] === B.data[i] && A.data[i + 1] === B.data[i + 1] && A.data[i + 2] === B.data[i + 2] && A.data[i + 3] === B.data[i + 3]) same++;
  }
  const total = w * h;
  const out = new PNG({ width: w, height: h });
  const mismatched = pixelmatch(A.data, B.data, out.data, w, h, { threshold: 0.1, alpha: 0.3 });
  if (diffFile) fs.writeFileSync(diffFile, PNG.sync.write(out));
  return {
    width: w,
    height: h,
    original_height: a.height,
    rendered_height: b.height,
    identical_pct: +(100 * same / total).toFixed(3),
    pixelmatch_pct: +(100 * (total - mismatched) / total).toFixed(3),
  };
}

async function main() {
  const spec = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const browser = await chromium.launch({ executablePath: browserPath(), headless: true, args: ["--hide-scrollbars", "--font-render-hinting=none", "--disable-lcd-text"] });
  const results = {};
  try {
    for (const job of spec.jobs) {
      fs.mkdirSync(job.out, { recursive: true });
      const r = { key: job.key };
      const desk = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
      if (job.original && !job.skip_parity) {
        const o = await shot(desk, job.original);
        const m = await shot(desk, job.rendered);
        fs.writeFileSync(path.join(job.out, "ours-1440.png"), m.buf);
        r.parity = diff(o.buf, m.buf, path.join(job.out, "diff-1440.png"));
      }
      for (const extra of job.extra || []) {
        // Other renders of the same package (real images, sparse club, reordered).
        const e = await shot(desk, extra.file);
        fs.writeFileSync(path.join(job.out, extra.name + "-1440.png"), e.buf);
      }
      await desk.close();
      r.mobile = {};
      for (const width of [390, 768]) {
        const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1024 }, deviceScaleFactor: 1, isMobile: false });
        const s = await shot(ctx, job.rendered, { metrics: true });
        fs.writeFileSync(path.join(job.out, `m${width}.png`), s.buf);
        r.mobile[width] = s.metrics;
        for (const extra of job.extra || []) {
          if (!extra.mobile) continue;
          const e = await shot(ctx, extra.file, { metrics: true });
          fs.writeFileSync(path.join(job.out, `${extra.name}-m${width}.png`), e.buf);
          r.mobile[`${extra.name}-${width}`] = e.metrics;
        }
        await ctx.close();
      }
      if (job.thumbs && job.package) {
        const t = await browser.newContext({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 600 / 1440 });
        fs.writeFileSync(path.join(job.package, "thumb.png"), (await shot(t, job.rendered, { fullPage: false })).buf);
        await t.close();
        const tm = await browser.newContext({ viewport: { width: 390, height: 780 }, deviceScaleFactor: 300 / 390 });
        fs.writeFileSync(path.join(job.package, "thumb-mobile.png"), (await shot(tm, job.rendered, { fullPage: false })).buf);
        await tm.close();
      }
      results[job.key] = r;
      process.stderr.write(`${job.key}: ${JSON.stringify(r.parity || {})}\n`);
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(spec.result, JSON.stringify(results, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
