import time
from playwright.sync_api import sync_playwright

URL = "http://localhost:4193/"
SHOTS = [8.0, 20.7, 24.0, 34.0]
errors, warns = [], []
with sync_playwright() as p:
    b = p.chromium.launch(args=[
        "--autoplay-policy=no-user-gesture-required",
        "--use-gl=swiftshader", "--enable-webgl", "--ignore-gpu-blocklist"])
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.on("console", lambda m: (errors if m.type == "error" else warns).append(f"{m.type}: {m.text}")
          if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="load")
    time.sleep(0.5)
    pg.mouse.click(640, 400)
    t0 = time.time()
    for ts in SHOTS:
        dt = ts - (time.time() - t0)
        if dt > 0:
            time.sleep(dt)
        pg.screenshot(path=f"tools/boot_build/final_{ts:04.1f}.png")
        print(f"shot @ {ts:.1f}s")
    # confirm the cue was fetched + applied, and music is routed/looping
    st = pg.evaluate("""async () => {
        let cue = null; try { cue = await (await fetch('/boot_cue.json')).json(); } catch(e){}
        const m = document.getElementById('boot-music');
        return { cue, mPaused: m && m.paused, mVol: m && +m.volume.toFixed(3) };
    }""")
    print("STATE:", st)
    b.close()
print("CONSOLE ERRORS:", len(errors))
for e in errors[:10]:
    print("  E", e)
print("WARNINGS:", len(warns))
for w in warns[:10]:
    print("  W", w)
